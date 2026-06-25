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
import io
import zipfile
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
        dataset: str,
        variables: list[str] | None = None,
        temporal_resolution: str = "auto",
        path: str | Path = "",
        fmt: str = "%Y-%m-%d",
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
            path: Output directory for the raster transports. Defaults to
                the current directory.
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.

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

        super().__init__(
            start=start,
            end=end,
            variables=[dataset],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=str(path),
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
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        if end_dt < start_dt:
            raise ValueError(
                f"end ({end!r}) is before start ({start!r}); flip the order "
                "or widen the window."
            )

        raw = pd.date_range(start=start_dt, end=end_dt, freq="D")
        snapped = snap_to_cadence(
            [pd.Timestamp(ts).date() for ts in raw],
            self._dataset.cadence,
        )
        if not snapped:
            snapped = [start_dt.date()]
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
            NotImplementedError: For the `edo-wcs` transport — the
                pyramids temporal `read_wcs` extension (`PY-A`) is not
                yet released. Tracked at the planning document under
                `PY-A`.
        """
        transport = self._dataset.transport
        if transport == "usdm-geojson":
            return self._fetch_usdm(products)
        if transport == "netcdf-url":
            return self._fetch_speibase(products)
        if transport == "edo-wcs":
            raise NotImplementedError(
                "Drought.edo-wcs transport waits on the pyramids temporal "
                "`read_wcs` extension (PY-A). When pyramids releases the "
                "`time=` parameter on `pyramids.wcs.read_wcs`, this branch "
                "calls `read_wcs(endpoint=row.endpoint, coverage=row.coverage, "
                "bbox=bbox_from_extent(self.space), crs='EPSG:4326', "
                "time=<period>, output=<tif>)` and returns the written "
                "paths."
            )
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
            products: One product per snapped Thursday release date.

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
        gdf = gpd.GeoDataFrame(merged, geometry="geometry", crs="EPSG:4326")
        if gdf.crs is None or str(gdf.crs).upper() not in {"EPSG:4326"}:
            gdf = gdf.to_crs("EPSG:4326")
        bbox = bbox_from_extent(self.space)
        within = gdf.cx[bbox[0] : bbox[2], bbox[1] : bbox[3]]
        if not len(within):
            return self._empty_vector()
        return FeatureCollection(
            gpd.GeoDataFrame(within, geometry="geometry", crs="EPSG:4326")
        )

    @staticmethod
    def _render_usdm_url(template: str, period: dt.date) -> str:
        """Substitute `{ymd}` in a USDM endpoint with the release date.

        Args:
            template: The catalog row's endpoint (carries `{ymd}`).
            period: The snapped Thursday release date.

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

        Args:
            payload: The decoded GeoJSON `FeatureCollection` dict.
            period: The snapped release date for this fetch.

        Returns:
            gpd.GeoDataFrame: Polygons in EPSG:4326 with a `release_date`
                column.
        """
        import geopandas as gpd

        gdf = gpd.GeoDataFrame.from_features(
            payload.get("features") or [], crs="EPSG:4326"
        )
        if not len(gdf):
            return gdf
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
            written: list[Path] = []
            for product in products:
                period: dt.date = product.metadata["period"]
                idx = (period.year - SPEIBASE_EPOCH_YEAR) * 12 + (period.month - 1)
                if idx < 0:
                    raise ValueError(
                        f"SPEIbase period {period} is before the dataset "
                        f"epoch ({SPEIBASE_EPOCH_YEAR}-01)."
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


def _extract_zipped_shp(blob: bytes, dest_dir: Path) -> Path:
    """Unzip a USDM `.zip` shapefile bundle into `dest_dir` and return its `.shp`.

    Kept module-scope (per no-nested-defs) so a future code path that
    prefers shapefile over GeoJSON can reuse it; the JSON path
    (`_fetch_usdm`) does not call it today.

    Args:
        blob: The zip bundle bytes.
        dest_dir: The directory to extract into.

    Returns:
        Path: The path of the extracted `.shp` file inside `dest_dir`.

    Raises:
        ValueError: When the zip contains no `.shp` member.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest_dir)
        for name in zf.namelist():
            if name.lower().endswith(".shp"):
                return dest_dir / name
    raise ValueError("USDM zip bundle contains no .shp member.")
