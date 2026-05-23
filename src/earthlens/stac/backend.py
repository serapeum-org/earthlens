"""STAC-API + COG backend (gridded raster output) over multiple endpoints.

`STAC(AbstractDataSource)` is one unified backend over the STAC-API + COG
providers — Microsoft Planetary Computer, Copernicus Data Space (CDSE), and
Earth Search (Element 84 / AWS) — which all speak STAC API v1 and differ only
in **asset signing**. A request is `variables={collection_key: [asset, ...]}`
plus a bbox + date window; the backend searches the endpoint, mosaics the
matched items to the bbox per acquisition date, and writes one Cloud-Optimized
GeoTIFF per `(collection, date)` to `path`.

The GIS heavy lifting lives in pyramids (per the pyramids/earthlens split):
`pyramids.stac.open_client` / `load_asset`, `pyramids.dataset.merge`'s
`merge_rasters` / `stack_bands`, `Dataset.crop` / `Dataset.to_crs`, and
`pyramids.dataset.cog.write_cog`. earthlens owns only the provider signers
(`earthlens.stac.signers`), the endpoint × collection × asset catalog, and the
search→load→write orchestration. There is no `odc-stac` / `stackstac`
dependency.

`OUTPUT_KIND = "raster"`, so the `EarthLens` facade forwards
`aggregate=AggregationConfig(...)` to `download()` rather than rejecting it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.stac.catalog import Catalog, Collection, Endpoint


class STAC(AbstractDataSource):
    """Unified STAC-API + COG backend (Planetary Computer / CDSE / Earth Search).

    Attributes:
        OUTPUT_KIND: `"raster"` — writes COGs; the facade forwards
            `aggregate=` (a multi-date pull composes with the aggregator).
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        endpoint: str | None = None,
        resolution: float | None = None,
        epsg: int | None = None,
        chunks: int | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        max_items: int | None = None,
    ):
        """Initialise a STAC backend instance.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            variables: `{collection_key: [asset_or_band, ...]}` — the logical
                collection key resolves to the endpoint's actual collection id
                via the catalog aliases; an empty asset list falls back to the
                collection's `default_assets`.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label; the search window is
                `start/end` and items are grouped by acquisition date.
            path: Output directory (created by the parent class).
            fmt: `strptime` format for `start` / `end`.
            endpoint: Endpoint key (`"planetary-computer"`, `"cdse"`,
                `"earth-search"`). Defaults to the home endpoint of the first
                requested collection.
            resolution: Optional output ground sample distance in metres.
            epsg: Optional output EPSG code.
            chunks: Optional COG tiling hint (reserved).
            region: Optional AWS region for requester-pays / S3 endpoints.
            access_key: CDSE S3 access key (else `CDSE_S3_ACCESS_KEY`).
            secret_key: CDSE S3 secret key (else `CDSE_S3_SECRET_KEY`).
            max_items: Optional cap on the number of items per collection
                search (mainly for tests / smoke pulls).
        """
        self._endpoint = endpoint
        self._resolution = resolution
        self._epsg = epsg
        self._chunks = chunks
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._max_items = max_items
        # Stored before super().__init__ because the base constructor calls
        # _initialize() (which needs the request) before it sets self.vars.
        self._variables = variables
        # Non-crossing AOI sub-bboxes; populated by _create_grid (one box, or
        # two when the request crosses the antimeridian).
        self._aoi_bboxes: list[tuple[float, float, float, float]] = []
        # Filled by _fetch: (collection_key, date, half index, path) per COG.
        self._written: list[tuple[str, str, int, Path]] = []
        self._catalog: Catalog | None = None
        self._endpoint_obj: Endpoint | None = None
        self._signer: Any = None
        self._client: Any = None
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

    def _initialize(self) -> None:
        """Load the catalog, resolve the endpoint + signer, open the STAC client.

        Returns `None` so the parent does not bind `self.client`; the opened
        client is stored on `self._client`.

        Raises:
            ValueError: When `variables` is empty or names an endpoint /
                collection the catalog does not know.
        """
        from earthlens.stac.catalog import Catalog
        from earthlens.stac.signers import build_signer

        if not self._variables:
            raise ValueError(
                "STAC requires variables={collection_key: [asset, ...]} with at "
                "least one collection."
            )
        self._catalog = Catalog()
        first_collection_key = next(iter(self._variables))
        if self._endpoint is None:
            self._endpoint = self._catalog.get_collection(first_collection_key).endpoint
        self._endpoint_obj = self._catalog.get_endpoint(self._endpoint)
        self._signer = build_signer(
            self._endpoint_obj.signer,
            region=self._region or self._endpoint_obj.region,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )

        from pyramids.stac import open_client

        self._client = open_client(self._endpoint_obj.url, signer=self._signer)
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Build the search AOI bbox(es) and the WGS84 envelope.

        An antimeridian-crossing AOI is expressed the OGC way — `lon_lim`
        west-to-east with `west > east` (e.g. `[170, -170]` for a box straddling
        180 deg). `pyramids.feature.bbox.split_antimeridian` severs it into an
        eastern `(west, south, 180, north)` and a western `(-180, south, east,
        north)` half; the one or two resulting non-crossing sub-bboxes are
        stored on `self._aoi_bboxes` and are what `_search` queries and `_fetch`
        crops to. `self.space` is the gross WGS84 envelope — the raw box when it
        does not cross, or the full -180..180 longitude span when it does, since
        a `SpatialExtent` enforces `longitude_min <= longitude_max` and so cannot
        itself represent a crossing box.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[west, east]` in degrees; `west > east` signals a
                crossing of the antimeridian.

        Returns:
            SpatialExtent: The gross WGS84 envelope of the request.
        """
        from pyramids.feature.bbox import split_antimeridian

        west, east = float(lon_lim[0]), float(lon_lim[1])
        south, north = float(lat_lim[0]), float(lat_lim[1])
        self._aoi_bboxes = split_antimeridian((west, south, east, north))
        envelope_lon = list(lon_lim) if west <= east else [-180.0, 180.0]
        return SpatialExtent.from_pairs(
            lat_lim=lat_lim, lon_lim=envelope_lon, resolution=self._resolution
        )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date window into a :class:`TemporalExtent`."""
        import datetime as dt

        import pandas as pd

        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h", "yearly": "YS"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt, end_date=end_dt, resolution=resolution, dates=dates
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def _bboxes(self) -> list[tuple[float, float, float, float]]:
        """Return the search AOI bbox(es) (two when the AOI crosses 180 deg)."""
        return self._aoi_bboxes

    def _search(self) -> list[RemoteProduct]:
        """Search every requested collection for the bbox + window.

        Returns:
            One :class:`RemoteProduct` per matched item; the pystac item, its
            acquisition date, the logical collection key, and the requested
            asset list ride on `metadata`.
        """
        start = self.time.start_date.strftime("%Y-%m-%d")
        end = self.time.end_date.strftime("%Y-%m-%d")
        products: list[RemoteProduct] = []
        for collection_key, requested in self.vars.items():
            collection = self._catalog.get_collection(collection_key)
            assets = list(requested) or list(collection.default_assets)
            resolved_id = self._catalog.resolve(self._endpoint, collection_key)
            for bbox in self._bboxes():
                search = self._client.search(
                    collections=[resolved_id],
                    bbox=list(bbox),
                    datetime=f"{start}/{end}",
                    max_items=self._max_items,
                )
                for item in search.items():
                    products.append(
                        RemoteProduct(
                            id=getattr(item, "id", str(item)),
                            metadata={
                                "item": item,
                                "date": _acq_date(item),
                                "collection_key": collection_key,
                                "assets": assets,
                                "bbox": tuple(bbox),
                            },
                        )
                    )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Mosaic the matched items per `(collection, date)` and write COGs.

        For each group: per requested band, collect the **signed** hrefs of the
        tiles covering the bbox, reproject mismatched-CRS tiles to a common CRS
        (`merge_rasters` does not reproject), mosaic, stack the bands, crop to
        the AOI, and write one COG. The merge / stack calls run inside
        `CloudConfig(extra=signer.gdal_env())` because those helpers read URLs
        directly and have no signer hook.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written COG paths, one per `(collection, date)` group.
        """
        from pyramids.base.remote import CloudConfig
        from pyramids.dataset.cog import write_cog
        from pyramids.dataset.merge import merge_rasters, stack_bands

        out: list[Path] = []
        # (collection_key, date, half index, path) for each written COG, so
        # download(aggregate=) can group + window them without re-parsing names.
        self._written = []
        multi = len(self._aoi_bboxes) > 1
        with CloudConfig(extra=self._signer.gdal_env()):
            for (collection_key, date, bbox_key), group in _group_products(products):
                idx = self._aoi_bboxes.index(bbox_key) if multi else 0
                assets = group[0].metadata["assets"]
                band_paths: list[Path] = []
                for band in assets:
                    hrefs = [
                        self._signer.sign_href(_asset_href(p.metadata["item"], band))
                        for p in group
                    ]
                    hrefs = self._to_common_crs(hrefs)
                    tmp = Path(self.root_dir) / f".{collection_key}_{band}_{date}_{idx}.tif"
                    if len(hrefs) > 1:
                        merge_rasters([str(h) for h in hrefs], str(tmp), method="last")
                    else:
                        _copy_single(hrefs[0], tmp)
                    band_paths.append(tmp)
                stacked = stack_bands(
                    [str(p) for p in band_paths], band_names=list(assets), align=True
                )
                # One COG per (collection, date); a crossing AOI yields one per
                # half, suffixed _part0 (eastern) / _part1 (western).
                part = f"_part{idx}" if multi else ""
                target = Path(self.root_dir) / f"{collection_key}_{date}{part}.tif"
                write_cog(stacked.crop(list(bbox_key)), str(target))
                out.append(target)
                self._written.append((collection_key, date, idx, target))
                _cleanup(band_paths)
        logger.info(f"STAC download: {len(out)} COG(s) written to {self.root_dir}")
        return out

    def _to_common_crs(self, hrefs: list[str]) -> list[str]:
        """Reproject mismatched-CRS tiles onto a shared CRS before mosaicking.

        `merge_rasters` aligns onto a union grid assuming a shared CRS, so a
        multi-UTM-zone bbox (common for Sentinel-2) needs each tile reprojected
        first. When all tiles already share a CRS (the common single-tile or
        single-zone case), the hrefs are returned unchanged.

        Args:
            hrefs: Signed tile hrefs for one band of one date group.

        Returns:
            Hrefs/paths ready for `merge_rasters` (reprojected copies where the
            CRS differed; the originals otherwise).
        """
        if len(hrefs) <= 1:
            return hrefs
        from pyramids.dataset import Dataset

        datasets = [Dataset.read_file(h) for h in hrefs]
        epsgs = {getattr(ds, "epsg", None) for ds in datasets}
        if len(epsgs) <= 1:
            return hrefs
        target_epsg = self._epsg or next(iter(sorted(e for e in epsgs if e)))
        reprojected: list[str] = []
        for href, ds in zip(hrefs, datasets):
            if getattr(ds, "epsg", None) == target_epsg:
                reprojected.append(href)
                continue
            tmp = Path(self.root_dir) / f".reproj_{abs(hash(href))}.tif"
            ds.to_crs(target_epsg).to_file(str(tmp))
            reprojected.append(str(tmp))
        return reprojected

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Search, mosaic, and write one COG per `(collection, date)`.

        Args:
            progress_bar: Reserved for parity with the other backends.
            aggregate: Optional aggregation request. Accepted (not rejected)
                because `OUTPUT_KIND` is `"raster"`, so the facade forwards it.

        Returns:
            The written COG paths.

        """
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate_cogs(aggregate)
        return paths

    def _aggregate_cogs(self, config: AggregationConfig) -> list[Path]:
        """Reduce the per-date COGs into per-`(window)` COGs via pyramids.

        Groups the COGs `_fetch` wrote by `(collection, antimeridian half)` —
        each group shares a grid — builds a `pyramids.dataset.DatasetCollection`
        over the group (time axis = acquisition date), labels each timestep with
        its `config.freq` window, and reduces with `config.op` via
        `DatasetCollection.groupby(labels).<op>()` (the COG analog of the
        NetCDF reducer CMEMS uses). One COG is written per `(group, window)`.

        Args:
            config: The aggregation request (`freq` window, `op` reducer,
                `out_dir`, `skipna`).

        Returns:
            The per-window COG paths.
        """
        from pyramids.dataset import Dataset, DatasetCollection
        from pyramids.dataset.cog import write_cog

        op = "mean" if config.op == "auto" else config.op
        out_dir = Path(config.out_dir) if config.out_dir is not None else Path(self.root_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        groups: dict[tuple[str, int], list[tuple[str, Path]]] = {}
        for collection_key, date, idx, path in self._written:
            groups.setdefault((collection_key, idx), []).append((date, path))

        written: list[Path] = []
        multi = len(self._aoi_bboxes) > 1
        for (collection_key, idx), dated in groups.items():
            dated.sort()
            dates = [d for d, _ in dated]
            files = [str(p) for _, p in dated]
            labels = _window_labels(dates, config.freq)
            collection = DatasetCollection.from_files(files)
            reduced = getattr(collection.groupby(labels), op)(skipna=config.skipna)
            geo, epsg = _geo_of(Dataset, files[0])
            part = f"_part{idx}" if multi else ""
            for label, array in reduced.items():
                target = out_dir / f"{collection_key}_{op}_{config.freq}_{label}{part}.tif"
                write_cog(Dataset.create_from_array(arr=array, geo=geo, epsg=epsg), str(target))
                written.append(target)
        logger.info(
            f"STAC aggregate: {len(self._written)} COG(s) -> {len(written)} "
            f"window COG(s) (time/{config.freq} {op}) in {out_dir}"
        )
        return written


def _window_labels(dates: list[str], freq: str) -> list[str]:
    """Return one window-start label (`YYYYMMDD`) per date, bucketed by `freq`.

    Dates sharing a `config.freq` window get the same label, so
    `DatasetCollection.groupby` coarsens the time axis to one slice per window.

    Args:
        dates: Acquisition dates as `YYYY-MM-DD` strings, in file order.
        freq: A pandas offset alias (`"1MS"`, `"7D"`, `"YS"`, …).

    Returns:
        One label per input date (length == `len(dates)`).
    """
    import pandas as pd

    index = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    positions = pd.Series(range(len(index)), index=index)
    label_for: dict[int, str] = {}
    for window_start, group in positions.groupby(pd.Grouper(freq=freq)):
        if group.empty:
            continue
        label = window_start.strftime("%Y%m%d")
        for pos in group.tolist():
            label_for[int(pos)] = label
    return [label_for[i] for i in range(len(index))]


def _geo_of(dataset_cls: Any, path: str) -> tuple[Any, Any]:
    """Return `(geotransform, epsg)` read from a written COG.

    Args:
        dataset_cls: The pyramids `Dataset` class.
        path: A COG path in the group (all share a grid).

    Returns:
        The geotransform and EPSG to stamp on the per-window outputs.
    """
    ds = dataset_cls.read_file(path)
    return ds.geotransform, ds.epsg


def _acq_date(item: Any) -> str:
    """Return a STAC item's acquisition date as `YYYY-MM-DD`.

    Args:
        item: A pystac `Item` (or a duck-typed stand-in with `datetime` /
            `properties["datetime"]`).

    Returns:
        The date portion of the item's datetime, or `"unknown"` when absent.
    """
    dttm = getattr(item, "datetime", None)
    if dttm is not None and hasattr(dttm, "strftime"):
        return dttm.strftime("%Y-%m-%d")
    props = getattr(item, "properties", None)
    if props is None and isinstance(item, dict):
        props = item.get("properties")
    props = props or {}
    value = props.get("datetime") if isinstance(props, dict) else None
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return "unknown"


def _asset_href(item: Any, asset_key: str) -> str:
    """Return the href of `asset_key` on a STAC item (pystac or raw dict).

    Args:
        item: A pystac `Item` or a raw STAC dict with an `assets` mapping.
        asset_key: The asset name to resolve.

    Returns:
        The asset href.

    Raises:
        KeyError: If the asset is missing or has no href.
    """
    assets = getattr(item, "assets", None)
    if assets is None and isinstance(item, dict):
        assets = item.get("assets")
    if not assets or asset_key not in assets:
        raise KeyError(
            f"asset {asset_key!r} not found on STAC item; "
            f"available: {sorted(assets or [])}"
        )
    asset = assets[asset_key]
    href = getattr(asset, "href", None)
    if href is None and isinstance(asset, dict):
        href = asset.get("href")
    if href is None:
        raise KeyError(f"STAC asset {asset_key!r} has no 'href'")
    return str(href)


def _group_products(
    products: list[RemoteProduct],
) -> list[tuple[tuple[str, str, tuple], list[RemoteProduct]]]:
    """Group products by `(collection_key, date, source bbox)`, first-seen order.

    The source bbox is part of the key so an antimeridian-crossing request —
    whose two halves are searched separately — produces one mosaic per half
    rather than collapsing both sides into a single (un-croppable) group.

    Args:
        products: The list returned by `_search`.

    Returns:
        `[((collection_key, date, bbox), [products...]), ...]`.
    """
    groups: dict[tuple[str, str, tuple], list[RemoteProduct]] = {}
    for product in products:
        key = (
            product.metadata["collection_key"],
            product.metadata["date"],
            product.metadata["bbox"],
        )
        groups.setdefault(key, []).append(product)
    return list(groups.items())


def _copy_single(href: str, target: Path) -> None:
    """Materialise a single signed tile href to `target` as a GeoTIFF.

    Used when a date group has exactly one tile for a band, so no mosaic is
    needed. Reads via pyramids (honouring the active `CloudConfig`) and writes
    a local copy.

    Args:
        href: The single signed tile href.
        target: Local output path.
    """
    from pyramids.dataset import Dataset

    Dataset.read_file(href).to_file(str(target))


def _cleanup(paths: list[Path]) -> None:
    """Best-effort removal of the per-band temporary mosaics.

    Args:
        paths: Temporary band-mosaic paths to delete.
    """
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
