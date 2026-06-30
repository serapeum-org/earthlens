"""Backend that fetches drought indicators from three live sources.

`Drought(AbstractDataSource)` reaches the US Drought Monitor (USDM, vector),
the Copernicus European / Global Drought Observatory (EDO/GDO, raster OGC
WCS), and the CSIC SPEIbase (raster NetCDF). The shape of `download()`
tracks the per-instance `OUTPUT_KIND` (`G1`): USDM returns a
`pyramids.feature.collection.FeatureCollection` in EPSG:4326; EDO/GDO and
SPEIbase return a `list[Path]` of written GeoTIFFs / NetCDFs.

The constructor's `dataset=` selects one curated row (`"usdm"`,
`"edo-spaST"`, `"speibase-12"`, …); the row's `transport` field drives the
`_fetch` route (`usdm-geojson` / `netcdf-url` / `edo-wcs`). The EDO/GDO
route is wired but raises `NotImplementedError` until the pyramids
temporal `read_wcs` extension (the cross-repo `PY-A` task) ships.

Authentication: none — all three sources are open. Each successful
`download()` logs the per-source attribution once (`G6`); no
`LicenseWarning` because none of the three carry a non-commercial /
restricted-redistribution clause.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.base._dates import to_datetime
from earthlens.drought._helpers import (
    attribution_for,
    bbox_from_extent,
    snap_to_cadence,
)
from earthlens.drought.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

#: Pinned epoch month for SPEIbase v2.x — every shipped version starts at
#: January 1901 (CRU TS lineage). The per-month positional index into the
#: NetCDF's `time` axis is `(year - 1901) * 12 + (month - 1)`. When a future
#: SPEIbase release changes the epoch, this constant lives in the catalog
#: row instead (a YAML edit).
SPEIBASE_EPOCH_YEAR: int = 1901

_HTTP_TIMEOUT: int = 90
_USER_AGENT: str = "earthlens-drought/0.x (https://github.com/serapeum-org/earthlens)"


class Drought(AbstractDataSource):
    """Drought-indicator backend (per-instance output kind).

    Wraps three live public services so a user can request a single drought
    indicator by id and get the right shape back. Each instance is bound to
    one catalog row at construction; the row's `output_kind` is copied onto
    the instance, so `download()` returns a `FeatureCollection` (vector
    USDM) or a `list[Path]` (raster EDO/GDO/SPEIbase) without per-call
    branching beyond `_fetch`.

    Attributes:
        OUTPUT_KIND: Class default `"raster"`, **overridden per instance**
            in `__init__` from the resolved row's `output_kind` (`G1`).
            The facade reads the instance value to gate `aggregate=`.

    Examples:
        - Resolve a vector USDM request (no network call at construction):
            ```python
            >>> from earthlens.drought import Drought
            >>> backend = Drought(
            ...     start="2026-06-23", end="2026-06-23",
            ...     dataset="usdm",
            ...     lat_lim=[30.0, 40.0], lon_lim=[-95.0, -85.0],
            ... )
            >>> backend.OUTPUT_KIND
            'vector'
            >>> backend._dataset.transport
            'usdm-geojson'

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        lat_lim: list[float],
        lon_lim: list[float],
        dataset: str | None = None,
        variables: list[str] | None = None,
        temporal_resolution: str = "auto",
        path: str | Path | None = None,
        fmt: str = "%Y-%m-%d",
        today: dt.date | None = None,
    ):
        """Configure a drought-indicator request.

        Args:
            start: Inclusive start date as a string (parsed with `fmt`).
            end: Inclusive end date as a string (`fmt`).
            lat_lim: `[lat_min, lat_max]` in degrees. Required.
            lon_lim: `[lon_min, lon_max]` in degrees. Required.
            dataset: One curated dataset id — `"usdm"`, `"edo-spaST"`,
                `"speibase-12"`, … See `Catalog().datasets` for the full
                list.
            variables: Accepted for facade-signature parity. Defaults to
                `[dataset]`; passing a non-empty value is rejected to
                keep the one-instance-one-dataset contract clear.
            temporal_resolution: Advisory label. Defaults to `"auto"` —
                the backend snaps dates to the source's release cadence
                (`weekly` / `10day` / `monthly`).
            path: Output directory for the raster transports. **Required**
                when the resolved dataset is raster — the SPEIbase NetCDFs
                and per-period GeoTIFFs land here, and silently writing
                hundreds of MB into the user's CWD is hostile. Defaults to
                `None`; raster requests without an explicit `path=` raise.
                Optional for the USDM vector transport (which returns an
                in-memory FeatureCollection without writing to disk).
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.
            today: Reference "now" date used by the USDM weekly snap to
                decide whether the same-week Tuesday's composite has been
                released yet. Defaults to `dt.date.today()` for live
                queries; tests and historical reruns pin this to a fixed
                date so the snap result is deterministic.

        Raises:
            ValueError: When `dataset` is missing or unknown, when
                `lat_lim` / `lon_lim` are missing, or when a non-trivial
                `variables=` argument is passed.
        """
        if not dataset:
            raise ValueError(
                "Drought needs dataset=; one of Catalog().datasets keys "
                "(e.g. 'usdm', 'edo-spaST', 'speibase-12')."
            )
        if variables not in (None, [], [dataset]):
            raise ValueError(
                "Drought is one-dataset-per-instance: pass dataset=... "
                "(the variables= kwarg only exists for facade parity)."
            )
        if not lat_lim or not lon_lim:
            raise ValueError(
                "Drought needs lat_lim=[lat_min, lat_max] and "
                "lon_lim=[lon_min, lon_max] (bbox is required)."
            )
        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(dataset)
        self.OUTPUT_KIND = self._dataset.output_kind
        self._today = today if today is not None else dt.date.today()

        # `bool(Path("")) is True` and `str(Path(""))` is `"."` — both would
        # bypass a `not path` check and silently route the parent class to
        # `Path(".").absolute()` (the user's CWD). Normalise to a string
        # first so the empty-path detection covers `None`, `""`, `Path("")`,
        # `Path(".")`, and `Path()` uniformly.
        path_str = "" if path is None else str(path)
        is_empty_path = path_str in ("", ".")
        if self.OUTPUT_KIND == "raster" and is_empty_path:
            raise ValueError(
                f"Drought needs path= for raster dataset {dataset!r} — the "
                "per-period rasters are written to disk; silently writing "
                "to the current working directory is not safe. Pass an "
                "explicit output directory (e.g. path='drought_out')."
            )

        super().__init__(
            start=start,
            end=end,
            variables=[dataset],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path_str,
        )

    def _initialize(self) -> None:
        """Open no client — every transport is per-call HTTP / pyramids I/O.

        Returns:
            None: Drought sources are open / unauthenticated; HTTP requests
                go out per fetch (`requests.get`), and the pyramids NetCDF
                / FeatureCollection / WCS readers manage their own state.
        """
        return None

    def _create_grid(
        self, lat_lim: list[float], lon_lim: list[float]
    ) -> SpatialExtent:
        """Wrap the WGS84 bbox into a frozen `SpatialExtent` (no snapping).

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
        """Parse the date window and snap it to the source's cadence.

        The snapped dates land on `self.time.dates`. `_search` then emits
        one `RemoteProduct` per distinct snapped period (one fetch per
        Thursday release / per 10-day dekad / per month).

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Advisory label (overridden by the
                resolved cadence).
            fmt: `strptime` format for both ends.

        Returns:
            TemporalExtent: The window plus the per-period snapped dates.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        if end_dt < start_dt:
            raise ValueError(
                f"end ({end!r}) is before start ({start!r}); flip the order "
                "or widen the window."
            )

        # Drop sub-day precision before building the range: `pd.date_range`
        # steps by 24h FROM the start's wall time, so a non-midnight start
        # like `datetime(2026, 6, 30, 23, 59)` paired with end
        # `datetime(2026, 7, 1, 0, 1)` would step past the end on the
        # second tick and yield only [Jun 30 23:59], silently dropping
        # July 1 (and therefore the July monthly snap) from the request.
        # Always range over calendar days.
        raw = pd.date_range(
            start=start_dt.date(), end=end_dt.date(), freq="D"
        )
        # `pd.date_range(start, end)` with `end_dt >= start_dt` always yields
        # at least one element, and `snap_to_cadence` is total over non-empty
        # input, so `snapped` is guaranteed non-empty here. Resist adding a
        # fallback: a future filter that empties `snapped` (e.g. a SPEIbase
        # pre-epoch drop, a USDM pre-release walk-back) is a real signal the
        # window is unreachable; silently pushing the un-snapped start_dt
        # into the URL would 404 with no hint at the cause.
        snapped = snap_to_cadence(
            [pd.Timestamp(ts).date() for ts in raw],
            self._dataset.cadence,
            today=self._today,
        )
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=self._dataset.cadence,
            dates=pd.DatetimeIndex([pd.Timestamp(d) for d in snapped]),
        )

    def _api(self) -> Any:
        """Run the canonical search/fetch composition.

        Returns:
            list[Path] | FeatureCollection: For raster transports the list
                of written paths; for the vector transport the merged
                `FeatureCollection`. `download()` wraps this with the
                attribution log and the `aggregate=` gate.
        """
        products = self._search()
        if not products:
            return [] if self.OUTPUT_KIND == "raster" else self._empty_vector()
        return self._fetch(products)

    def _search(self) -> list[RemoteProduct]:
        """List one product per snapped period of the bound dataset.

        Returns:
            list[RemoteProduct]: One item per distinct snapped date; the
                period date is stored in `metadata["period"]` so `_fetch`
                can stamp it into the URL placeholder or the file name.
        """
        return [
            RemoteProduct(
                id=f"{self._dataset.id}@{ts.date().isoformat()}",
                href=self._dataset.endpoint,
                metadata={"period": ts.date(), "dataset": self._dataset.id},
            )
            for ts in self.time.dates
        ]

    def _fetch(self, products: list[RemoteProduct]) -> Any:
        """Dispatch one period's fetch to the bound dataset's transport.

        Args:
            products: The `_search` result — one `RemoteProduct` per
                snapped period.

        Returns:
            list[Path] | FeatureCollection: Raster transports return the
                list of written file paths in `products` order; the USDM
                vector transport returns the merged `FeatureCollection`.

        Raises:
            ValueError: When the resolved row's `transport` is not one of
                the three known values (catalog out of sync with backend).
        """
        transport = self._dataset.transport
        if transport == "usdm-geojson":
            return self._fetch_usdm(products)
        if transport == "netcdf-url":
            return self._fetch_speibase(products)
        if transport == "edo-wcs":
            return self._fetch_wcs(products)
        raise ValueError(
            f"unknown drought transport {transport!r} on dataset "
            f"{self._dataset.id!r} (catalog out of sync with backend?)"
        )

    def _fetch_usdm(self, products: list[RemoteProduct]) -> FeatureCollection:
        """Fetch the USDM weekly GeoJSON per period and merge into one FC.

        Each period's GeoJSON is downloaded, parsed by geopandas, and
        appended into a single `FeatureCollection`. The result is
        reprojected to EPSG:4326 as a defensive no-op (the current files
        already ship EPSG:4326; the reproject keeps the contract stable if
        a future release switches CRS) and clipped to the requested bbox.

        Args:
            products: One product per snapped Tuesday valid date.

        Returns:
            FeatureCollection: The drought-class polygons, CRS `EPSG:4326`,
                clipped to the requested bbox.
        """
        import geopandas as gpd
        from pyramids.feature.collection import FeatureCollection

        frames: list[gpd.GeoDataFrame] = []
        for product in products:
            period: dt.date = product.metadata["period"]
            url = self._render_usdm_url(product.href, period)
            payload = _http_get_json(url)
            frame = self._geojson_to_gdf(payload, period)
            if len(frame):
                frames.append(frame)
        if not frames:
            return self._empty_vector()
        merged = pd.concat(frames, ignore_index=True)
        # Honour the input frame's CRS: `pd.concat` of GeoDataFrames yields
        # one whose CRS is the first frame's; do NOT pass `crs=...` to the
        # constructor (that re-labels coords without transforming and
        # silently strands a non-4326 payload at the wrong epsg).
        gdf = gpd.GeoDataFrame(merged, geometry="geometry")
        if gdf.crs is None:
            # _geojson_to_gdf always stamps the source CRS (RFC 7946 default
            # 4326 when the payload omits it), so reaching here means an
            # upstream contract was broken. Raise loudly rather than
            # silently re-labelling unknown coordinates.
            raise RuntimeError(
                "USDM frame reached _fetch_usdm with no CRS; "
                "_geojson_to_gdf is expected to stamp one."
            )
        if gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        bbox = bbox_from_extent(self.space)
        within = gdf.cx[bbox[0] : bbox[2], bbox[1] : bbox[3]]
        if not len(within):
            return self._empty_vector()
        # `within` inherits `gdf`'s CRS (already 4326 after the to_crs
        # above); do NOT pass `crs="EPSG:4326"` here — that is the
        # relabel-without-transform footgun the comment above warns
        # against. A future maintainer who edits the block between line
        # `gdf = gdf.to_crs(...)` and this wrap must keep `within.crs`
        # consistent rather than relying on a re-stamp.
        return FeatureCollection(
            gpd.GeoDataFrame(within, geometry="geometry")
        )

    @staticmethod
    def _render_usdm_url(template: str, period: dt.date) -> str:
        """Substitute `{ymd}` in a USDM endpoint with the Tuesday valid date.

        Args:
            template: The catalog row's endpoint (carries `{ymd}`).
            period: The snapped Tuesday valid date.

        Returns:
            str: The fully-rendered USDM JSON URL.
        """
        return template.replace("{ymd}", period.strftime("%Y%m%d"))

    @staticmethod
    def _geojson_to_gdf(payload: dict[str, Any], period: dt.date):
        """Materialise a USDM GeoJSON payload as a GeoDataFrame.

        Adds a `release_date` column so a multi-period merge keeps the
        per-period attribution; preserves USDM's `OBJECTID` / `DM` /
        `Shape_Length` / `Shape_Area` schema verbatim.

        The source CRS is read from the payload's top-level `crs` member
        (RFC 7946 deprecated but still emitted by USDM and most ArcGIS
        endpoints); when absent the GeoJSON spec mandates EPSG:4326. The
        downstream `_fetch_usdm` step is the one that drives a real
        `to_crs` transform — this helper only labels the frame
        accurately.

        Args:
            payload: The decoded GeoJSON `FeatureCollection` dict.
            period: The snapped Tuesday valid date for this fetch.

        Returns:
            gpd.GeoDataFrame: Polygons labelled with whatever CRS the
                payload declared (RFC 7946 default `EPSG:4326` when
                absent) and a `release_date` column.
        """
        import geopandas as gpd

        source_crs = _crs_from_geojson(payload)
        features = payload.get("features") or []
        if not features:
            return gpd.GeoDataFrame(
                {"geometry": gpd.GeoSeries([], crs=source_crs)},
                crs=source_crs,
            )
        gdf = gpd.GeoDataFrame.from_features(features, crs=source_crs)
        gdf["release_date"] = period.isoformat()
        return gdf

    def _fetch_speibase(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch the SPEIbase NetCDF once per scale, write one TIFF per period.

        SPEIbase ships **one NetCDF per timescale** (`spei01.nc` …
        `spei48.nc`), each carrying a monthly `time` axis from 1901-01.
        Every period requested by `_search` lives in the **same file**, so
        the NetCDF is downloaded once and a per-month TIFF is sliced out
        via `pyramids.netcdf.NetCDF.subset`.

        Args:
            products: One product per snapped month.

        Returns:
            list[Path]: The written GeoTIFFs in `products` order.
        """
        from pyramids.netcdf import NetCDF

        endpoint = self._dataset.endpoint
        nc_path = self.root_dir / f"{self._dataset.id}.nc"
        if not nc_path.exists():
            _http_download(endpoint, nc_path)
        bbox = bbox_from_extent(self.space)

        nc = NetCDF.read_file(str(nc_path))
        try:
            # Case-insensitive lookup — pyramids reports dimension names in
            # whatever case the source NetCDF uses; SPEIbase v2.x ships
            # `time` lowercase today, but a future release that switches to
            # `Time` / `t` should not silently disable the upper-bound
            # guard. An empty `dimension_sizes` (variable-subset open with
            # no root group) is treated as an unknown axis and rejected
            # rather than letting an over-range idx propagate.
            dim_sizes = nc.dimension_sizes or {}
            time_axis = next(
                (size for name, size in dim_sizes.items() if name.lower() == "time"),
                None,
            )
            if time_axis is None:
                raise ValueError(
                    f"SPEIbase NetCDF {nc_path.name} has no discoverable "
                    "time axis (dimension_sizes empty or missing 'time' "
                    "key). Cannot safely slice — bump the pyramids floor "
                    "or pin the catalog row to a NetCDF with a labelled "
                    "time dimension."
                )
            n_time = time_axis
            written: list[Path] = []
            for product in products:
                period: dt.date = product.metadata["period"]
                idx = (period.year - SPEIBASE_EPOCH_YEAR) * 12 + (period.month - 1)
                if idx < 0:
                    raise ValueError(
                        f"SPEIbase period {period} is before the dataset "
                        f"epoch ({SPEIBASE_EPOCH_YEAR}-01)."
                    )
                if idx >= n_time:
                    last_year = SPEIBASE_EPOCH_YEAR + (n_time - 1) // 12
                    last_month = ((n_time - 1) % 12) + 1
                    raise ValueError(
                        f"SPEIbase period {period} is past the bundled "
                        f"time axis (length {n_time}, last month "
                        f"{last_year}-{last_month:02d}). Bump the catalog "
                        "row's `endpoint` to a newer SPEIbase release."
                    )
                subset = nc.subset(
                    "spei",
                    time=idx,
                    bbox=bbox,
                    crs=4326,
                )
                out_path = (
                    self.root_dir
                    / f"{self._dataset.id}_{period.strftime('%Y%m')}.tif"
                )
                subset.to_file(str(out_path))
                written.append(out_path)
        finally:
            nc.close()
        return written

    def _fetch_wcs(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch one Copernicus EDO/GDO GeoTIFF per period via WCS GetCoverage.

        EDO/GDO is a Copernicus REST shim: only its `GetCoverage` operation
        is reliable (the standard `GetCapabilities` / `DescribeCoverage`
        discovery handshake is 502 / 400), so we build the documented
        GetCoverage URL by hand — `coverageID` + `CRS=EPSG:4326` +
        `format=GEOTIFF` + the custom `TIME=<date>` /
        `SELECTED_TIMESCALE=<NN>` params + the `SUBSET=Long/Lat` bbox — fetch
        it with core `requests`, and open the bytes through pyramids
        `Dataset.read_file`. No `owslib`, no GDAL WCS driver, no `xarray`
        (`G7`).

        Args:
            products: One product per snapped period (10-day dekad or
                month, per the row's cadence).

        Returns:
            list[Path]: The written GeoTIFFs in `products` order.

        Raises:
            ValueError: When the server rejects a period (e.g. a date
                outside the indicator's available coverage range); the
                Copernicus error message is surfaced verbatim.
        """
        from pyramids.dataset import Dataset

        bbox = bbox_from_extent(self.space)
        written: list[Path] = []
        for product in products:
            period: dt.date = product.metadata["period"]
            url = self._render_wcs_url(
                self._dataset.endpoint,
                coverage=self._dataset.coverage,
                timescale=self._dataset.timescale,
                period=period,
                bbox=bbox,
            )
            out_path = (
                self.root_dir
                / f"{self._dataset.id}_{period.strftime('%Y%m%d')}.tif"
            )
            _http_download_raster(url, out_path, label=self._dataset.id)
            # Open to validate it is a real raster (guards against a 200
            # response carrying a non-raster body), then leave it on disk.
            ds = Dataset.read_file(str(out_path))
            ds.close()
            written.append(out_path)
        return written

    @staticmethod
    def _render_wcs_url(
        endpoint: str,
        *,
        coverage: str | None,
        timescale: str | None,
        period: dt.date,
        bbox: tuple[float, float, float, float],
    ) -> str:
        """Build a Copernicus EDO/GDO `GetCoverage` URL for one period.

        Args:
            endpoint: The row's WCS map endpoint (carries `?map=DO_WCS`
                or `?map=GDO_WCS`).
            coverage: The WCS `coverageID` (e.g. `"spaST"`).
            timescale: The `SELECTED_TIMESCALE` value (`"01"`, `"03"`,
                …); omitted from the URL when `None`.
            period: The snapped date for this fetch (`TIME=`).
            bbox: `(west, south, east, north)` in EPSG:4326 degrees.

        Returns:
            str: The fully-rendered GetCoverage URL.

        Raises:
            ValueError: When `coverage` is `None` (an `edo-wcs` row must
                carry a coverage id).
        """
        if not coverage:
            raise ValueError(
                "an edo-wcs drought row must carry a `coverage` id; "
                "got None."
            )
        west, south, east, north = bbox
        params = [
            "SERVICE=WCS",
            "VERSION=2.0.0",
            "REQUEST=GetCoverage",
            f"coverageID={coverage}",
            "CRS=EPSG:4326",
            "format=GEOTIFF",
            f"TIME={period.isoformat()}",
            f"SUBSET=Long({west},{east})",
            f"SUBSET=Lat({south},{north})",
        ]
        if timescale:
            params.append(f"SELECTED_TIMESCALE={timescale}")
        sep = "&" if "?" in endpoint else "?"
        return endpoint + sep + "&".join(params)

    @staticmethod
    def _empty_vector() -> FeatureCollection:
        """Return an empty `FeatureCollection` with the USDM schema.

        Used when every requested period produced zero polygons or fell
        entirely outside the requested bbox.

        Returns:
            FeatureCollection: Zero rows, the USDM columns, CRS
                `EPSG:4326`.
        """
        import geopandas as gpd
        from pyramids.feature.collection import FeatureCollection

        frame = pd.DataFrame(
            {
                "OBJECTID": pd.Series([], dtype="int64"),
                "DM": pd.Series([], dtype="int64"),
                "Shape_Length": pd.Series([], dtype="float64"),
                "Shape_Area": pd.Series([], dtype="float64"),
                "release_date": pd.Series([], dtype="object"),
            }
        )
        gdf = gpd.GeoDataFrame(
            frame, geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326"
        )
        return FeatureCollection(gdf)

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path] | FeatureCollection:
        """Fetch the requested drought indicator and return its artefacts.

        Args:
            progress_bar: Accepted for facade-signature parity. The three
                drought transports run one fetch per period in serial
                today, so a `tqdm` bar would mostly idle; the argument is
                a no-op until a transport gains a per-byte progress hook.
            aggregate: A temporal reducer over the requested date range.
                Accepted for `OUTPUT_KIND == "raster"` (EDO/GDO/SPEIbase)
                and forwarded to the standard `earthlens.aggregate`
                pyramids reducer. **Rejected** for `OUTPUT_KIND ==
                "vector"` (USDM) — drought-class polygons have no
                gridded reduction.

        Returns:
            FeatureCollection | list[Path]: For the vector USDM transport,
                the merged drought-class `FeatureCollection` in EPSG:4326.
                For the raster transports, the list of written file paths
                in period order.

        Raises:
            NotImplementedError: When `aggregate is not None` on the
                vector USDM transport, or when the bound dataset uses
                the `edo-wcs` transport (waits on `PY-A`).
        """
        if self.OUTPUT_KIND == "vector" and aggregate is not None:
            raise NotImplementedError(
                "Drought.download(aggregate=...) is not supported for the "
                "USDM (vector) transport: drought-class polygons have no "
                "gridded reduction. Call download() without aggregate= and "
                "post-process the returned FeatureCollection directly."
            )
        # Force `progress_bar` into the local scope so a future per-period
        # tqdm hook does not break the public signature when wired up.
        _ = progress_bar
        if self.OUTPUT_KIND == "raster" and aggregate is not None:
            raise NotImplementedError(
                "Drought.download(aggregate=...) for raster transports is "
                "not wired in this build. The SPEIbase / EDO / GDO outputs "
                "are per-period GeoTIFFs; pass them through "
                "`earthlens.aggregate.aggregate_netcdf` (or pyramids' "
                "`DatasetCollection.groupby`) directly until the drought "
                "stack reducer ships."
            )
        result = self._api()
        logger.info(attribution_for(self._dataset.transport))
        return result


def _crs_from_geojson(payload: dict[str, Any]) -> str:
    """Read the CRS a GeoJSON payload declares, defaulting to EPSG:4326.

    The RFC 7946 `crs` member is deprecated, but every USDM JSON we
    sample carries an ArcGIS-flavoured one
    (`{"crs": {"type": "name", "properties": {"name": "EPSG:4326"}}}`
    or `{"type": "EPSG", "properties": {"code": 4326}}`). Accept both
    shapes plus the older `urn:ogc:def:crs:EPSG::4326` URN. When the
    payload omits the `crs` member entirely, RFC 7946 §4 mandates
    EPSG:4326.

    Args:
        payload: A decoded GeoJSON `FeatureCollection` dict.

    Returns:
        str: A `"EPSG:NNNN"` CRS string ready to feed to geopandas.
    """
    crs = payload.get("crs")
    if not isinstance(crs, dict):
        return "EPSG:4326"
    props = crs.get("properties")
    # `or {}` only catches None/empty; a truthy non-dict (a `str`, `list`,
    # or `int` from a malformed upstream) would slip past and the next
    # `.get(...)` would AttributeError. Default to the RFC 7946 value
    # instead, which is the only safe interpretation when we cannot
    # parse the properties block.
    if not isinstance(props, dict):
        return "EPSG:4326"
    name = props.get("name") or props.get("code")
    if name is None:
        return "EPSG:4326"
    text = str(name)
    if text.upper().startswith("EPSG:"):
        return text
    if "EPSG::" in text:
        return f"EPSG:{text.rsplit('::', 1)[-1]}"
    if text.isdigit():
        return f"EPSG:{text}"
    return text


def _http_get_json(url: str) -> dict[str, Any]:
    """Download a JSON payload over HTTP.

    Args:
        url: The fully-rendered URL.

    Returns:
        dict[str, Any]: The decoded JSON body.

    Raises:
        requests.HTTPError: For non-2xx responses.
    """
    response = requests.get(
        url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}
    )
    response.raise_for_status()
    return response.json()


def _http_download(url: str, target: Path) -> None:
    """Stream a binary payload to `target` over HTTP.

    Args:
        url: The source URL.
        target: The destination path. Parent directory must exist.

    Raises:
        requests.HTTPError: For non-2xx responses.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    with requests.get(
        url,
        timeout=_HTTP_TIMEOUT,
        stream=True,
        headers={"User-Agent": _USER_AGENT},
    ) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
    tmp.replace(target)


def _http_download_raster(url: str, target: Path, *, label: str) -> None:
    """Stream a raster GeoTIFF to `target`, surfacing Copernicus JSON errors.

    Like `_http_download`, but on a non-2xx response it reads the body and
    re-raises a `ValueError` carrying the Copernicus error message (EDO/GDO
    answer an out-of-range date or a bad coverage with an informative JSON
    `{"message": ...}` body — far more useful than a bare HTTP status). A
    2xx response is streamed to disk unchanged.

    Args:
        url: The fully-rendered GetCoverage URL.
        target: The destination `.tif` path.
        label: The dataset id, for the error message.

    Raises:
        ValueError: On a non-2xx response; the Copernicus message (or the
            raw body) is included.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    with requests.get(
        url,
        timeout=_HTTP_TIMEOUT,
        stream=True,
        headers={"User-Agent": _USER_AGENT},
    ) as response:
        if response.status_code >= 400:
            body = response.text
            try:
                message = response.json().get("message", body)
            except ValueError:
                message = body
            raise ValueError(
                f"Copernicus EDO/GDO rejected {label!r} "
                f"(HTTP {response.status_code}): {message.strip()[:300]}"
            )
        chunks = response.iter_content(chunk_size=1 << 16)
        first = next(chunks, b"")
        # A 2xx is NOT a guarantee of a raster: this Copernicus MapServer
        # answers an invalid `map=`/coverage with a plain-text or HTML body
        # under HTTP 200 (e.g. `ERROR: invalid map parameter`). Reject any
        # body that does not start with the GeoTIFF magic so a non-raster
        # error never reaches `Dataset.read_file` as an opaque GDAL failure.
        if first[:4] not in (b"MM\x00*", b"II*\x00"):
            detail = first.decode("utf-8", errors="replace").strip()[:300]
            raise ValueError(
                f"Copernicus EDO/GDO returned a non-raster body for "
                f"{label!r} (HTTP {response.status_code}): {detail}"
            )
        with tmp.open("wb") as fh:
            fh.write(first)
            for chunk in chunks:
                if chunk:
                    fh.write(chunk)
    tmp.replace(target)


