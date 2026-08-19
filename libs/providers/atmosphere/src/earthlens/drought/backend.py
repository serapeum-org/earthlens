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
route fetches each period through `pyramids.dataset.Dataset.from_wcs` in
**direct** mode, as the soilgrids backend does — earthlens shapes the
provider-specific request (`TIME` + `SELECTED_TIMESCALE` + the bbox) and
translates the failure, pyramids speaks the protocol and returns the raster.
`direct=True` is required because Copernicus EDO/GDO is a REST shim whose
WCS discovery handshake is 502/400-flaky, so the GetCapabilities path does
not apply; the shim also wants a lowercase `coverageID` and a WCS-1.x `CRS=`
rather than the spec's `COVERAGEID` / `SUBSETTINGCRS=`, which direct-mode
`extra_params` supplies. See `_fetch_wcs_coverage`.

Authentication: none — all three sources are open. Each successful
`download()` logs the per-source attribution once (`G6`); no
`LicenseWarning` because none of the three carry a non-commercial /
restricted-redistribution clause.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.http import HttpClient
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

    # No `SUPPORTS_AGGREGATE = True`: neither transport reduces today, so the
    # central gate refuses `aggregate=` before the body runs. The reason covers
    # both, because `OUTPUT_KIND` is resolved per instance from the catalog row.
    AGGREGATE_REFUSAL_REASON = (
        "no drought transport reduces across dates today. The USDM "
        "(vector) route returns drought-class polygons, which have no gridded "
        "reduction; the SPEIbase / EDO / GDO (raster) routes emit per-period "
        "GeoTIFFs whose stack reducer is not wired yet — pass those through "
        "earthlens.aggregate.aggregate_netcdf (or pyramids' "
        "DatasetCollection.groupby) directly"
    )

    def __init__(
        self,
        start: str | dt.date | dt.datetime,
        end: str | dt.date | dt.datetime,
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
            start: Inclusive start date — a `date` / `datetime`, or a
                string parsed with `fmt`.
            end: Inclusive end date — a `date` / `datetime`, or a string
                parsed with `fmt`.
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
            path: Output directory for the raster transports — the SPEIbase
                NetCDFs and per-period GeoTIFFs land here. When omitted it
                falls back to the configured earthlens output directory
                (`set_output_dir()` / `EARTHLENS_DATA_DIR`); see
                `earthlens.config`. Asking for the working directory
                explicitly (`path=""` or `path="."`) is refused for raster
                datasets, because writing hundreds of MB there is hostile.
                Optional for the USDM vector transport (which returns an
                in-memory FeatureCollection without writing to disk).
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.
            today: Reference "now" date used by the USDM weekly snap to
                decide whether the same-week Tuesday's composite has been
                released yet. Defaults to `dt.date.today()` for live
                queries; pin it to a fixed date for a deterministic snap
                against historical data.

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

        # An omitted `path` is fine: it resolves to the configured output
        # directory, which is a deliberate location rather than wherever the
        # process happens to be running. Asking for the cwd *explicitly* is
        # still refused for raster, because that is the case this guard was
        # written for. `bool(Path("")) is True` and `str(Path(""))` is `"."`,
        # so normalise to a string first to cover `""`, `Path("")`, `Path(".")`
        # and `Path()` uniformly.
        if path is not None and self.OUTPUT_KIND == "raster":
            if str(path).strip() in ("", "."):
                raise ValueError(
                    f"Drought needs a real path= for raster dataset "
                    f"{dataset!r} — the per-period rasters are written to "
                    "disk, and writing hundreds of MB into the current "
                    "working directory is not safe. Pass an explicit output "
                    "directory (e.g. path='drought_out'), or omit path= to "
                    "use the configured earthlens output directory."
                )

        super().__init__(
            # Drought accepts str/date/datetime; the overridden
            # `_check_input_dates` normalises all three via `to_datetime`,
            # so bridge the base template's narrower `str` here.
            start=cast("str", start),
            end=cast("str", end),
            variables=[dataset],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

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
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

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
        raw = pd.date_range(start=start_dt.date(), end=end_dt.date(), freq="D")
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

        # Lazy so a `limit=` stops the work: each period is a separate GeoJSON
        # download, so a week past the cap is never requested. Empty periods
        # are dropped before the cap counts them, so the cap bounds returned
        # polygons rather than weeks attempted.
        bbox = bbox_from_extent(self.space)
        frames = self._take_limited(
            (
                frame
                for frame in (
                    self._fetch_usdm_period(product, bbox) for product in products
                )
                if len(frame)
            ),
            limit=self._limit,
        )
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
        # Already clipped per period (before the cap counted the rows), so
        # this is a no-op for the bbox and only guards the empty case.
        within = gdf.cx[bbox[0] : bbox[2], bbox[1] : bbox[3]]
        if not len(within):
            return self._empty_vector()
        # `within` inherits `gdf`'s CRS (already 4326 after the to_crs
        # above); do NOT pass `crs="EPSG:4326"` here — that is the
        # relabel-without-transform footgun the comment above warns
        # against. A future maintainer who edits the block between line
        # `gdf = gdf.to_crs(...)` and this wrap must keep `within.crs`
        # consistent rather than relying on a re-stamp.
        return FeatureCollection(gpd.GeoDataFrame(within, geometry="geometry"))

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

    def _fetch_usdm_period(
        self, product: RemoteProduct, bbox: tuple[float, float, float, float]
    ) -> Any:
        """Download one USDM week's GeoJSON and clip it to the request bbox.

        The clip happens here, not after the concat, because a `limit=` counts
        these frames as they arrive. USDM publishes one **national** polygon
        set per week, so counting before the clip would let a cap fill up on
        rows outside the requested area and return nothing — with the weeks
        that did intersect it never fetched.

        Args:
            product: One period product from `_search`; its `href` is the URL
                template and its `period` metadata the week to render.
            bbox: The request's `(west, south, east, north)` filter.

        Returns:
            gpd.GeoDataFrame: That week's drought polygons intersecting the
                bbox, stamped with the period (empty when nothing matched).
        """
        period: dt.date = product.metadata["period"]
        assert product.href is not None  # USDM products always carry a URL template
        url = self._render_usdm_url(product.href, period)
        payload = _http_get_json(url)
        frame = self._geojson_to_gdf(payload, period)
        if not len(frame):
            return frame
        if frame.crs is None:
            # _geojson_to_gdf always stamps the source CRS (RFC 7946 default
            # 4326 when the payload omits it), so reaching here means an
            # upstream contract was broken. Raise loudly rather than silently
            # re-labelling unknown coordinates.
            raise RuntimeError(
                "USDM frame reached _fetch_usdm_period with no CRS; "
                "_geojson_to_gdf is expected to stamp one."
            )
        # Reproject before clipping: `bbox` is in EPSG:4326, so clipping a
        # payload delivered in another CRS with those numbers discards
        # everything.
        if frame.crs.to_epsg() != 4326:
            frame = frame.to_crs("EPSG:4326")
        return frame.cx[bbox[0] : bbox[2], bbox[1] : bbox[3]]

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
        # Key the on-disk cache on the endpoint URL, not just the dataset id:
        # a catalog version bump (e.g. SPEIbase v2.11 -> v2.12) changes the
        # `endpoint`, so a leftover file from the old release is ignored and
        # the new bytes are fetched — the version bump stays a pure YAML edit.
        endpoint_tag = hashlib.sha1(
            endpoint.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:8]
        nc_path = self.root_dir / f"{self._dataset.id}-{endpoint_tag}.nc"
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
                    self.root_dir / f"{self._dataset.id}_{period.strftime('%Y%m')}.tif"
                )
                try:
                    subset.to_file(str(out_path))
                finally:
                    subset.close()
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
            list[Path]: The written GeoTIFFs in `products` order. Each is
                window-cropped to `bbox` via `_clip_wcs_raster`; on the rare
                period where the requested bbox falls entirely outside the
                downloaded raster's coverage, the crop is skipped, a warning
                is logged, and the original uncropped file is kept instead.

        Raises:
            ValueError: When the server rejects a period (e.g. a date
                outside the indicator's available coverage range); the
                Copernicus error message is surfaced verbatim.
        """
        bbox = bbox_from_extent(self.space)
        written: list[Path] = []
        for product in products:
            period: dt.date = product.metadata["period"]
            out_path = (
                self.root_dir / f"{self._dataset.id}_{period.strftime('%Y%m%d')}.tif"
            )
            # Write to a sibling temp first and rename only on success, so a
            # failed write (full disk, GDAL error mid-write) never leaves a
            # truncated GeoTIFF that a later run would read as valid. `.part`
            # goes *before* the suffix — GDAL picks its driver from the
            # extension, so a trailing `.part` is an unknown format.
            tmp_path = out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")
            dataset = self._fetch_wcs_coverage(period, bbox)
            clipped = None
            try:
                # The Copernicus EDO/GDO MapServer honours the `Lat` subset but
                # silently ignores `Long`, returning a full -180..180 strip
                # (re-verified live 2026-07-17: a Long(-10,5) request still
                # comes back spanning -25..51), and tags the GeoTIFF with no
                # embedded SRS — so `Dataset.crop`'s cutline path is a no-op.
                # Window-crop to the requested bbox by hand so the output
                # honours the documented extent, mirroring the USDM (`gdf.cx`)
                # and SPEIbase (`NetCDF.subset`) transports.
                clipped = self._clip_wcs_raster(dataset, bbox)
                if clipped is None:
                    logger.warning(
                        f"{self._dataset.id}: requested bbox {bbox} fell "
                        f"entirely outside the downloaded raster's coverage; "
                        f"writing {out_path} unclipped (full server-returned "
                        f"extent)."
                    )
                (dataset if clipped is None else clipped).to_file(str(tmp_path))
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            finally:
                # Close on every path: `crop` / `to_file` can raise, and
                # without this the GDAL handles leak for the rest of the batch.
                if clipped is not None:
                    clipped.close()
                dataset.close()
            # Only now: GDAL keeps the written file open until its source
            # Dataset is closed, and Windows refuses to rename a file that is
            # still held open — so the promotion must follow the `finally`.
            tmp_path.replace(out_path)
            written.append(out_path)
        return written

    def _fetch_wcs_coverage(
        self, period: dt.date, bbox: tuple[float, float, float, float]
    ) -> Any:
        """Fetch one EDO/GDO period as an in-memory `Dataset` over WCS.

        The WCS transport is pyramids' (`Dataset.from_wcs`), matching the
        soilgrids backend: earthlens shapes the provider-specific request and
        translates the failure, pyramids speaks the protocol.

        `direct=True` skips the GetCapabilities handshake (the EDO/GDO
        discovery endpoint is 502/400-flaky) and builds the documented KVP
        GetCoverage call. Two of those keys go through `extra_params` because
        this MapServer is not spec-compliant: it rejects pyramids' uppercased
        `COVERAGEID` and the WCS-2.0 `SUBSETTINGCRS=`, wanting lowercase
        `coverageID` and the WCS-1.x `CRS=` instead — direct-mode
        `extra_params` overrides a built-in KVP case-insensitively
        (serapeum-org/pyramids#725). `wcs_format="GEOTIFF"` is mandatory:
        `from_wcs` defaults it to `None`, and without a `format=` the shim
        answers HTTP 500.

        Args:
            period: The snapped date for this fetch (`TIME=`).
            bbox: `(west, south, east, north)` in EPSG:4326 degrees.

        Returns:
            Dataset: The server's coverage, un-cropped (see `_clip_wcs_raster`).

        Raises:
            ValueError: When `coverage` is `None` (an `edo-wcs` row must carry
                one), or when the server rejects the request — an out-of-range
                date, an unknown coverage, or a non-raster body under HTTP 200.
                The Copernicus message is surfaced verbatim.
        """
        from pyramids.dataset import Dataset
        from pyramids.errors import WCSError

        if not self._dataset.coverage:
            raise ValueError(
                "an edo-wcs drought row must carry a `coverage` id; got None."
            )
        params = {
            "coverageID": self._dataset.coverage,
            "CRS": "EPSG:4326",
            "TIME": period.isoformat(),
        }
        if self._dataset.timescale:
            params["SELECTED_TIMESCALE"] = self._dataset.timescale
        try:
            return Dataset.from_wcs(
                self._dataset.endpoint,
                coverage=self._dataset.coverage,
                crs="EPSG:4326",
                wcs_format="GEOTIFF",
                direct=True,
                bbox=bbox,
                extra_params=params,
            )
        except WCSError as exc:
            # `WCSError` is not a `ValueError`, and this backend's documented
            # contract is a `ValueError` carrying the Copernicus text. Since
            # pyramids 0.46.0 the message embeds the response body
            # (serapeum-org/pyramids#744), so the informative
            # `{"message": "...outside the available coverage range..."}` EDO
            # answers a bad date with survives the translation.
            raise ValueError(
                f"Copernicus EDO/GDO rejected {self._dataset.id!r}: {exc}"
            ) from exc

    @staticmethod
    def _clip_wcs_raster(dataset: Any, bbox: tuple[float, float, float, float]) -> Any:
        """Window-crop a WCS raster to `bbox` when the server didn't clip it.

        The Copernicus EDO/GDO `GetCoverage` endpoint honours the latitude
        subset but ignores longitude, returning a full -180..180 strip, and
        the GeoTIFF carries no embedded SRS so `pyramids.Dataset.crop`'s
        cutline path is a no-op. We therefore compute the pixel window from
        the raster's own bounding box and rebuild the trimmed raster with
        `Dataset.create_from_array` — a purely local operation that honours
        the requested bbox regardless of what the server returned. Latitude,
        already clipped server-side, is windowed again harmlessly; if the
        server ever starts honouring `Long`, the window simply re-selects the
        same region.

        Args:
            dataset: The freshly-downloaded `pyramids.Dataset` (typically a
                full-width -180..180 strip).
            bbox: The requested `(west, south, east, north)` in EPSG:4326
                degrees.

        Returns:
            A new cropped `pyramids.Dataset`, or `None` when the requested
            bbox falls entirely outside the raster (`_fetch_wcs` keeps the
            original file untouched and logs a warning in that case).
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        west, south, east, north = bbox
        arr = dataset.read_array()
        if arr.ndim == 3:
            arr = arr[0]
        x_min, y_min, x_max, y_max = dataset.bbox
        rows, cols = arr.shape
        cell_x = (x_max - x_min) / cols
        cell_y = (y_max - y_min) / rows
        col_start = max(int(round((west - x_min) / cell_x)), 0)
        col_stop = min(int(round((east - x_min) / cell_x)), cols)
        row_start = max(int(round((y_max - north) / cell_y)), 0)
        row_stop = min(int(round((y_max - south) / cell_y)), rows)
        if col_stop <= col_start or row_stop <= row_start:
            return None
        window = arr[row_start:row_stop, col_start:col_stop]
        geo = (
            x_min + col_start * cell_x,
            cell_x,
            0.0,
            y_max - row_start * cell_y,
            0.0,
            -cell_y,
        )
        nodata = dataset.no_data_value[0] if dataset.no_data_value else None
        return PyramidsDataset.create_from_array(
            arr=window,
            geo=geo,
            epsg=4326,
            no_data_value=nodata,
        )

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
        limit: int | None = None,
    ) -> list[Path] | FeatureCollection:
        """Fetch the requested drought indicator and return its artefacts.

        Args:
            progress_bar: Accepted for facade-signature parity. The three
                drought transports run one fetch per period in serial
                today, so a `tqdm` bar would mostly idle; the argument is
                a no-op until a transport gains a per-byte progress hook.
            aggregate: Refused on every transport today, by the shared gate
                rather than here. The USDM (vector) route returns polygons,
                which have no gridded reduction; the raster routes emit
                per-period GeoTIFFs whose stack reducer is not wired yet.
            limit: Cap on the total drought polygons returned, across every
                requested week. Applied as each week's GeoJSON arrives — after
                the bbox clip, so it counts rows the caller will actually
                receive — meaning a week past the cap is never downloaded.
                `None` (the default) fetches everything. **Vector (USDM)
                only** — the raster transports return written files, which a
                row cap cannot describe, so passing one there is refused
                rather than silently ignored.

        Returns:
            FeatureCollection | list[Path]: For the vector USDM transport,
                the merged drought-class `FeatureCollection` in EPSG:4326.
                For the raster transports, the list of written file paths
                in period order.

        Raises:
            NotImplementedError: When `aggregate is not None`. Raised by the
                shared gate before this body runs — the USDM (vector) route
                has no gridded reduction, and the raster routes' cross-period
                stack reducer is not wired yet.
            ValueError: When `limit` is not `None` on a raster transport,
                where there are no rows to cap; or when it is zero or
                negative.
        """
        self._limit = self.check_limit(limit)
        if self._limit is not None and self.OUTPUT_KIND != "vector":
            raise ValueError(
                f"Drought.download(limit=...) applies to the USDM (vector) "
                f"transport only; dataset {self._dataset.id!r} writes raster "
                f"files, which a row cap cannot describe. Drop limit= and "
                f"narrow the date range instead."
            )
        # Force `progress_bar` into the local scope so a future per-period
        # tqdm hook does not break the public signature when wired up.
        _ = progress_bar
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
    """Download a JSON payload over HTTP via the shared `HttpClient`.

    Args:
        url: The fully-rendered URL.

    Returns:
        dict[str, Any]: The decoded JSON body.

    Raises:
        requests.HTTPError: For non-2xx responses.
    """
    return cast("dict[str, Any]", _http_client().get_json(url, timeout=_HTTP_TIMEOUT))


def _http_client() -> HttpClient:
    """Build the drought `HttpClient`: fresh-connection GETs, no status retry.

    Routes through the module-level `requests.get` (via `_RequestsGet`) so
    tests that monkeypatch `requests.get` still drive the transport, and
    keeps the previous single-shot-on-status behaviour (`status_forcelist=()`)
    while reusing `HttpClient`'s streamed atomic `download`, transport-error
    retry, and consistent User-Agent.
    """
    return HttpClient(
        user_agent=_USER_AGENT,
        status_forcelist=(),
        max_backoff=None,
    )


def _http_download(url: str, target: Path) -> None:
    """Stream a binary payload to `target` over HTTP.

    Delegates the transfer to `HttpClient.download`, which streams to a
    sibling `<target>.part` file and atomically renames it on success,
    removing the temp on any failure — so an interrupted download never
    leaves a truncated file (nor a stale temp) behind. Error statuses are
    raised immediately, not retried (`status_forcelist=()`), matching the
    previous single-shot behaviour.

    Args:
        url: The source URL.
        target: The destination path. Parent directory is created.

    Raises:
        requests.HTTPError: For non-2xx responses.
    """
    _http_client().download(
        url, target, chunk=1 << 16, progress=False, timeout=_HTTP_TIMEOUT
    )
