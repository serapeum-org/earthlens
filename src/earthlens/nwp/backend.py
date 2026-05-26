"""Backend that fetches open NWP forecasts as bbox-cropped COGs.

`NWP(AbstractDataSource)` is one backend over the open
numerical-weather-prediction buckets — NOAA NODD (GFS / GEFS / HRRR /
…), ECMWF Open Data (IFS), DWD Open Data (ICON), with Météo-France /
ECCC as follow-ons. It differs from the observation-time backends in
its **forecast time axis**: data is indexed by
`(cycle_datetime_utc, forecast_step_hours)`, not a single valid time.
`start` / `end` select the **cycle date range**; a `steps=` /
`horizon=` kwarg picks the forecast lead times; one COG is produced
per `(cycle, step)`.

The request shape is `variables = {model_key: [param, ...]}` (mirrors
the GEE / STAC backends). Each `param` resolves through the catalog to
the centre's selector — a Herbie `search` regex or a DWD variable
token. The download path per model is the catalog `backend:` value,
dispatched to a sibling :mod:`earthlens.nwp.centres` module.

`OUTPUT_KIND` is fixed `"raster"`: every centre yields a GRIB2 file
that the shared pipeline reads with `pyramids.grib.open_grib`, crops
to the request bbox, and writes as a COG (`C3`); `aggregate=` reduces
the `(cycle, step)` stack (`C6`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.nwp._helpers import (
    cog_name,
    enumerate_cycles,
    parse_cog_valid_time,
    window_labels,
)
from earthlens.nwp.catalog import KNOWN_BACKENDS, Catalog, NWPModel
from earthlens.nwp.centres import resolve_centre

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.nwp.centres.base import _NWPCentre


class NWP(AbstractDataSource):
    """Open numerical-weather-prediction backend (forecast time axis).

    Resolves each requested model key against the bundled catalog,
    dispatches its download to the matching centre module, and yields
    one bbox-cropped COG per `(cycle, step)`. Open buckets only — no
    authentication.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every model yields gridded
            output, so the facade always forwards `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "6hourly",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        centre: str | None = None,
        mirror: str = "auto",
        steps: list[int] | None = None,
        horizon: int | None = None,
        catalog: Catalog | None = None,
    ):
        """Initialise an NWP backend instance.

        Resolves every requested model key against the catalog
        **before** the parent constructor runs, because the parent
        calls :meth:`_initialize` first and `self.vars` is not yet set
        there.

        Args:
            start: Inclusive start of the cycle-date range (parsed with
                `fmt`).
            end: Inclusive end of the cycle-date range.
            variables: Mapping from model key to a list of parameter
                names, e.g. `{"gfs": ["temperature_2m"]}`.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label. Defaults to
                `"6hourly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            centre: Optional explicit centre override (reserved; the
                catalog `backend:` normally selects the centre).
            mirror: Cloud-mirror key (`"auto"` lets the centre choose).
            steps: Explicit forecast lead times in hours. Defaults to
                `[0]` (the analysis step) when neither `steps` nor
                `horizon` is given.
            horizon: Maximum forecast lead time in hours; expands to a
                step list per model cadence (resolved in `C3`).
            catalog: Optional pre-built :class:`Catalog` (tests inject
                a faked one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a model key is
                unknown, or a model declares an unknown `backend:`.
        """
        if not variables:
            raise ValueError(
                "NWP requires a non-empty `variables` mapping of "
                "{model_key: [param, ...]}."
            )
        self._centre = centre
        self._mirror = mirror
        self._steps_arg = steps
        self._horizon_arg = horizon
        self._catalog = catalog if catalog is not None else Catalog()
        self._requests: list[tuple[str, NWPModel, list[str]]] = self._resolve_models(
            variables
        )
        # Centre instances are cached per backend so a multi-cycle fetch
        # reuses one Herbie / ecmwf-opendata adapter rather than rebuilding it.
        self._centres: dict[str, _NWPCentre] = {}

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _resolve_models(
        self, variables: dict[str, list[str]]
    ) -> list[tuple[str, NWPModel, list[str]]]:
        """Resolve every requested model key to a catalog row + params.

        Args:
            variables: The `{model_key: [param, ...]}` request.

        Returns:
            list[tuple[str, NWPModel, list[str]]]: One `(key, model,
                params)` triple per request key, in request order.

        Raises:
            ValueError: When a key is unknown (the catalog's
                did-you-mean is surfaced), a model declares an unknown
                `backend:`, or a requested param is not in the model's
                band map.
        """
        resolved: list[tuple[str, NWPModel, list[str]]] = []
        for model_key, params in variables.items():
            model = self._catalog.get_model(model_key)
            if model.backend not in KNOWN_BACKENDS:
                raise ValueError(
                    f"model {model_key!r} declares unknown backend "
                    f"{model.backend!r}; known: {sorted(KNOWN_BACKENDS)}."
                )
            unknown = [p for p in params if p not in model.bands]
            if unknown:
                raise ValueError(
                    f"model {model_key!r} has no band(s) {unknown}; "
                    f"known params: {sorted(model.bands)}."
                )
            resolved.append((model_key, model, list(params)))
        return resolved

    def _initialize(self):
        """No-op auth hook — every MVP centre is an open bucket.

        Returns `None`; the parent binds no `self.client`.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a :class:`SpatialExtent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the cycle-date range into a :class:`TemporalExtent`.

        For NWP the `dates` index is the requested **cycle date**
        range; the per-cycle / per-step expansion happens in
        :meth:`_search` (`C3`).

        Args:
            start: Inclusive start of the cycle-date range.
            end: Inclusive end of the cycle-date range.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        dates = pd.date_range(start_dt, end_dt, freq="D")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="D",
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def _steps_for(self, model: NWPModel) -> list[int]:
        """Resolve the forecast lead times to fetch for one model (`G1`).

        Precedence: an explicit `steps=` list wins; otherwise `horizon=`
        expands to every integer hour `0..horizon`; otherwise the
        default is `[0]` (the analysis step), keeping the MVP bounded.
        `steps=` is the recommended way to request a coarse set of lead
        times, since not every model publishes every hourly step.

        Args:
            model: The resolved catalog row (bounds the request via
                `horizon_h`).

        Returns:
            list[int]: Sorted, de-duplicated lead times in hours.

        Raises:
            ValueError: When a requested step exceeds the model's
                `horizon_h`.
        """
        if self._steps_arg is not None:
            steps = sorted({int(s) for s in self._steps_arg})
        elif self._horizon_arg is not None:
            steps = list(range(0, int(self._horizon_arg) + 1))
        else:
            steps = [0]
        too_far = [s for s in steps if s > model.horizon_h]
        if too_far:
            raise ValueError(
                f"step(s) {too_far} exceed the {model.horizon_h} h horizon "
                f"of the requested model."
            )
        return steps

    def _centre_for(self, backend: str) -> _NWPCentre:
        """Return the cached :class:`_NWPCentre` for a catalog `backend:`.

        Args:
            backend: The model's `backend:` value (e.g. `"herbie"`).

        Returns:
            _NWPCentre: A centre bound to the output directory; one
                instance per backend, reused across cycles.
        """
        if backend not in self._centres:
            self._centres[backend] = resolve_centre(backend, self.root_dir)
        return self._centres[backend]

    def _search(self) -> list[RemoteProduct]:
        """Expand the request into one product per `(model, cycle, step)`.

        Walks the cycle grid (`G1`): for each requested model, every
        cycle in the `start`/`end` date range (per the model's
        `cycles_utc`) crossed with every requested forecast step.

        Returns:
            list[RemoteProduct]: One product per `(model, cycle, step)`,
                each carrying the model row, cycle, step, and requested
                params in `metadata` so `_fetch` needs no re-query.
        """
        products: list[RemoteProduct] = []
        for model_key, model, params in self._requests:
            cycles = enumerate_cycles(
                self.time.start_date, self.time.end_date, model.cycles_utc
            )
            for cycle in cycles:
                for step in self._steps_for(model):
                    products.append(
                        RemoteProduct(
                            id=f"{model_key}.{cycle:%Y%m%d%H}.f{step:03d}",
                            metadata={
                                "model_key": model_key,
                                "model": model,
                                "cycle": cycle,
                                "step": step,
                                "params": params,
                            },
                        )
                    )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch each product's GRIB2, crop to the bbox, write a COG (`G4`).

        Per product: the matching centre downloads the variable-subset
        GRIB2 (the >99 % bandwidth win — Herbie `.idx` or DWD's
        per-variable files), then `pyramids.grib.open_grib` reads it,
        the result is cropped to the request bbox, and written as a COG.
        Global models on a 0–360° longitude grid are normalised to
        −180..180 first when the bbox reaches into negative longitudes,
        so an Americas crop lands correctly.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One cropped COG path per product, in order.
        """
        from pyramids.dataset.cog import write_cog
        from pyramids.grib import open_grib

        bbox = [
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        ]
        out: list[Path] = []
        for product in products:
            meta = product.metadata
            centre = self._centre_for(meta["model"].backend)
            grib_path = centre.fetch_one(
                meta["model"],
                meta["cycle"],
                meta["step"],
                meta["params"],
                self._mirror,
            )
            dataset = open_grib(str(grib_path))
            dataset = self._normalise_longitude(dataset)
            # touch=False avoids pyramids' wrap-cutline correction, which calls
            # the GDAL/PROJ database for the GRIB driver's reported CRS
            # (EPSG:9122, WGS84 lon/lat) — a code many bundled PROJ databases
            # cannot resolve. The plain (non-cutline) crop path needs no such
            # lookup and subsets a regular NWP grid correctly.
            cropped = dataset.crop(bbox=bbox, epsg=4326, touch=False)
            target = self.root_dir / cog_name(
                meta["model_key"], meta["cycle"], meta["step"]
            )
            write_cog(cropped, str(target))
            out.append(target)
        return out

    def _normalise_longitude(self, dataset):
        """Shift a 0–360° global grid to −180..180 when the bbox needs it.

        `pyramids` `convert_longitude` only applies to a whole-globe
        0–360 raster (it raises otherwise). A regional model (HRRR) or a
        bbox entirely in the eastern hemisphere needs no shift, so this
        is a no-op unless the request bbox reaches a negative longitude.

        Args:
            dataset: The freshly opened GRIB2 `Dataset`.

        Returns:
            The same `Dataset`, or a longitude-shifted copy.
        """
        if self.space.west >= 0:
            return dataset
        try:
            return dataset.convert_longitude()
        except ValueError:
            # Not a 0–360 global raster (e.g. a regional model already in
            # −180..180); the bbox is already in the dataset's CRS.
            return dataset

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the requested forecasts as bbox-cropped COGs.

        Args:
            progress_bar: Whether to show per-product progress.
            aggregate: Optional
                :class:`earthlens.aggregate.AggregationConfig`; reduces
                the `(cycle, step)` COG stack (`C6`).

        Returns:
            list[Path]: One cropped COG per `(cycle, step)`, or — when
                `aggregate` is set — the per-window reduced rasters.
        """
        self._show_progress = progress_bar
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(
        self, paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce the `(cycle, step)` COG stack into per-window COGs (`C6`).

        Labels each COG by the window its **valid time** (`cycle + step`)
        falls in, then reduces the co-registered stack with `config.op`
        via `DatasetCollection.groupby(labels).<op>()` — the COG analog
        of the NetCDF reducer the observation-time backends use. One COG
        is written per window.

        Aggregation requires a **single model**: different models have
        different native grids and cannot be co-registered into one
        stack, so a multi-model request is rejected here rather than
        silently mixing grids.

        Args:
            paths: The per-`(cycle, step)` COGs from :meth:`_fetch`.
            config: The aggregation request (`freq` window, `op`
                reducer, `out_dir`, `skipna`).

        Returns:
            list[Path]: The per-window reduced COG paths.

        Raises:
            ValueError: When the request names more than one model.
        """
        if not paths:
            return []
        model_keys = {key for key, _, _ in self._requests}
        if len(model_keys) > 1:
            raise ValueError(
                "aggregate= over an NWP request needs a single model; "
                f"got {sorted(model_keys)}. Different models have different "
                "native grids and cannot be co-registered into one stack — "
                "issue one request per model."
            )
        from pyramids.dataset import Dataset, DatasetCollection
        from pyramids.dataset.cog import write_cog

        op = "mean" if config.op == "auto" else config.op
        out_dir = (
            Path(config.out_dir) if config.out_dir is not None else Path(self.root_dir)
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        dated = sorted((parse_cog_valid_time(p), str(p)) for p in paths)
        times = [t for t, _ in dated]
        files = [f for _, f in dated]
        labels = window_labels(times, config.freq)
        collection = DatasetCollection.from_files(files)
        reduced = getattr(collection.groupby(labels), op)(skipna=config.skipna)
        reference = Dataset.read_file(files[0])
        geo, epsg = reference.geotransform, reference.epsg
        model_key = model_keys.pop()
        written: list[Path] = []
        for label, array in reduced.items():
            target = out_dir / f"{model_key}_{op}_{config.freq}_{label}.tif"
            write_cog(
                Dataset.create_from_array(arr=array, geo=geo, epsg=epsg), str(target)
            )
            written.append(target)
        return written
