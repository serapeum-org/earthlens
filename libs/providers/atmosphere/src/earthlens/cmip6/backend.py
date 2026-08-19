"""Backend that fetches raw CMIP6 climate projections from the Pangeo ARCO mirror.

`CMIP6(AbstractDataSource)` exposes the **full raw CMIP6 archive** — every
ScenarioMIP / CMIP experiment, every ESM, on its native grid — as analysis-ready
Zarr on the open `gs://cmip6` Google Cloud bucket (Pangeo), indexed by a plain
consolidated-stores CSV with **no auth**. This is the whole `model x scenario x
variable x member` matrix, unlike the single pre-downscaled `NASA/GDDP-CMIP6`
product the `gee` backend exposes or the CHC-CMIP6 precipitation deltas the `chc`
backend exposes.

A request is a CMIP6 **facet tuple** — `source_id` (model), `experiment_id`
(scenario), `variable_id`, `table_id` (+ optional `member_id` / `grid_label` /
`version`). :meth:`_search` resolves it against the CSV
(:class:`~earthlens.cmip6.resolver.StoreResolver`) to the matching `zstore`
URI(s) — a tuple that pins fewer facets **fans out**, one output per store.
:meth:`_fetch` then has pyramids open each store and write a **bbox/time NetCDF
subset** (:mod:`earthlens.cmip6.accessor`): the `[start, end]` window maps to an
integer time-index range, the `lat_lim`/`lon_lim` box crops the grid, and only
the requested cells are fetched. earthlens never imports `xarray` / `zarr` /
`gcsfs` — pyramids owns the read (GDAL `/vsigs/`, anonymous).

The archive is on each model's native grid and stores are large, so a subset is
the default; a whole-grid download (`lat_lim`/`lon_lim` left at whole-Earth) is
allowed but warned. Aggregation (`aggregate=`) is not supported — the written
NetCDFs can be aggregated separately.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.cmip6 import accessor
from earthlens.cmip6.catalog import Catalog
from earthlens.cmip6.resolver import ResolvedStore, StoreResolver


class CMIP6(AbstractDataSource):
    """CMIP6 climate-projections backend (raw archive on `gs://cmip6`).

    Wraps the open Pangeo CMIP6 ARCO mirror so a user pulls a
    model / scenario / variable / member subset of the raw CMIP6 archive through
    the same `download()` shape every other earthlens backend uses. The output is
    one gridded NetCDF per resolved store.

    Attributes:
        OUTPUT_KIND: `"raster"` — the written artefacts are gridded NetCDFs.
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "the backend writes gridded NetCDF subsets; reduce them separately with earthlens.aggregate.aggregate_netcdf"

    def __init__(
        self,
        start: str,
        end: str,
        *,
        source_id: str | None = None,
        experiment_id: str | None = None,
        variable_id: str | None = None,
        table_id: str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        member_id: str | None = None,
        grid_label: str | None = None,
        version: str = "latest",
        activity_id: str | None = None,
        whole_time: bool = False,
        temporal_resolution: str = "monthly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        catalog: Catalog | None = None,
        resolver: StoreResolver | None = None,
    ):
        """Initialise a CMIP6 backend instance.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`).
            end: Inclusive end of the date window.
            source_id: Model key (`"CanESM5"`, `"GFDL-ESM4"`).
            experiment_id: Scenario / experiment (`"ssp585"`, `"historical"`).
            variable_id: The CMIP6 variable to fetch (`"tas"`, `"pr"`).
            table_id: The MIP table (`"Amon"`, `"day"`, `"Omon"`).
            lat_lim: `[lat_min, lat_max]` in degrees. A whole-Earth box
                (`[-90, 90]`, the default) is "no spatial subset" — a whole-grid
                download, warned; a narrower box crops the native grid.
            lon_lim: `[lon_min, lon_max]` in degrees. Defaults to `[-180, 180]`.
            member_id: Variant label; `None` uses the catalog default
                (`r1i1p1f1`).
            grid_label: Grid label (`"gn"`, `"gr"`); `None` fans out over the
                grids present for the other facets.
            version: `"latest"` (newest publication per store) or an explicit
                version string.
            activity_id: MIP the experiment belongs to; `None` leaves it
                unconstrained (inferred from the experiment).
            whole_time: Skip the date-window time subset and write the whole
                series (warned). Defaults to `False`.
            temporal_resolution: Advisory cadence label (fixed by `table_id`).
            path: Output directory for the written NetCDFs.
            fmt: `strptime` format for `start` / `end`.
            catalog: Optional pre-built :class:`Catalog`; defaults to the
                bundled catalog.
            resolver: Optional pre-built
                :class:`~earthlens.cmip6.resolver.StoreResolver`; defaults to one
                built from the catalog's CSV URL and facet columns.

        Raises:
            ValueError: If a required facet (`source_id` / `experiment_id` /
                `variable_id` / `table_id`) or a date bound is omitted or empty.
        """
        for name, value in (
            ("source_id", source_id),
            ("experiment_id", experiment_id),
            ("variable_id", variable_id),
            ("table_id", table_id),
        ):
            if not value:
                raise ValueError(f"CMIP6 requires a non-empty {name}.")
        if not start or not end:
            raise ValueError(
                "CMIP6 requires a start and end date, e.g. "
                "start='2050-01-01', end='2050-12-31'."
            )

        # The loop above raised on any empty required id; narrow for the type
        # checker so the downstream str-typed uses see non-optional values.
        assert source_id is not None
        assert experiment_id is not None
        assert variable_id is not None
        assert table_id is not None

        self._catalog = catalog if catalog is not None else Catalog()
        self._resolver = (
            resolver
            if resolver is not None
            else StoreResolver(self._catalog.csv_url, self._catalog.facet_columns)
        )
        self._source_id = source_id
        self._experiment_id = experiment_id
        self._variable_id = variable_id
        self._table_id = table_id
        self._member_id = member_id or self._catalog.default_member_id
        self._grid_label = grid_label
        self._version = version
        self._activity_id = activity_id
        self._whole_time = whole_time
        self._show_progress = True

        super().__init__(
            start=start,
            end=end,
            variables=[variable_id],
            temporal_resolution=temporal_resolution,
            lat_lim=[-90.0, 90.0] if lat_lim is None else lat_lim,
            lon_lim=[-180.0, 180.0] if lon_lim is None else lon_lim,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="raw")

    def _wants_spatial_subset(self) -> bool:
        """Return whether the request narrows the grid (a bbox crop).

        Returns:
            bool: `True` when the bbox is narrower than whole-Earth.
        """
        return not (
            self.space.latitude_min <= -90.0
            and self.space.latitude_max >= 90.0
            and self.space.longitude_min <= -180.0
            and self.space.longitude_max >= 180.0
        )

    def _bbox(self) -> tuple[float, float, float, float] | None:
        """Return the request bbox as `(west, south, east, north)`, or `None`.

        Returns:
            tuple | None: The crop window, or `None` for a whole-grid request.
        """
        if not self._wants_spatial_subset():
            return None
        return (self.space.west, self.space.south, self.space.east, self.space.north)

    def _search(self) -> list[RemoteProduct]:
        """Resolve the facet tuple to one product per matching `zstore`.

        Returns:
            list[RemoteProduct]: One product per resolved store; each carries the
                `zstore` as `href` and the store's facets as `metadata`.

        Raises:
            ValueError: If no store matches (the resolver names the offending
                facet and lists the available values).
        """
        stores = self._resolver.resolve(
            source_id=self._source_id,
            experiment_id=self._experiment_id,
            variable_id=self._variable_id,
            table_id=self._table_id,
            member_id=self._member_id,
            grid_label=self._grid_label,
            version=self._version,
            activity_id=self._activity_id,
        )
        return [
            RemoteProduct(
                id=store.slug,
                href=store.zstore,
                metadata={"store": store},
            )
            for store in stores
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Write a bbox/time NetCDF subset for each resolved store.

        For each store: map the `[start, end]` window to an integer time-index
        range (unless `whole_time`), then read the gridded `(variable, time,
        bbox)` window through pyramids and write it to NetCDF.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One written NetCDF path per store, in order.
        """
        bbox = self._bbox()
        if bbox is None:
            logger.warning(
                f"cmip6: no bbox — writing the whole native grid for "
                f"{self._variable_id}/{self._experiment_id}; pass lat_lim/lon_lim "
                "to subset (CMIP6 stores can be large)."
            )
        out: list[Path] = []
        for product in tqdm(
            products, disable=not self._show_progress, desc="cmip6", unit="store"
        ):
            store: ResolvedStore = product.metadata["store"]
            time_sel = self._time_selector(store)
            stem = accessor.store_output_stem(
                store, self.time.start_date, self.time.end_date
            )
            out_path = self.root_dir / f"{stem}.nc"
            out.append(
                accessor.write_subset(
                    store.zstore,
                    self._variable_id,
                    bbox=bbox,
                    time=time_sel,
                    out_path=out_path,
                )
            )
        return out

    def _time_selector(
        self, store: ResolvedStore
    ) -> int | tuple[int, int] | slice | None:
        """Resolve the time selector for one store from the request window.

        Args:
            store: The resolved store to read.

        Returns:
            The integer time selector for :func:`accessor.write_subset` — a
            `(i0, i1)` index range for the date window, or `slice(None)` for a
            `whole_time` request.
        """
        if self._whole_time:
            return slice(None)
        return accessor.resolve_time_window(
            store.zstore,
            self._variable_id,
            self.time.start_date,
            self.time.end_date,
        )

    def terms_note(self) -> str:
        """Return the attribution note for the requested source model.

        Returns:
            str: The per-model `terms_note`, else the catalog default.
        """
        return self._catalog.terms_note(self._source_id)

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the requested CMIP6 subset(s) and return the written paths.

        Runs the cheap :meth:`_search` (facet -> `zstore` resolution) then
        :meth:`_fetch`, which writes one bbox/time NetCDF subset per resolved
        store.

        Args:
            progress_bar: Show a per-store progress bar. Defaults to `True`.

        Returns:
            list[Path]: The written NetCDF paths, one per resolved store (never
                empty — a facet tuple that matches no store raises rather than
                returning an empty list).

        Raises:
            ValueError: If the facet tuple matches no store.
        """
        self._show_progress = progress_bar
        return self._api_via_search_fetch()
