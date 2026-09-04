"""STAC-API + COG backend (gridded raster output) over multiple endpoints.

`STAC(AbstractDataSource)` is one unified backend over the STAC-API + COG
providers — Microsoft Planetary Computer, Copernicus Data Space (CDSE), and
Earth Search (Element 84 / AWS) — which all speak STAC API v1 and differ only
in **asset signing**. A request is `variables={collection_key: [asset, ...]}`
plus a bbox + date window; the backend searches the endpoint, mosaics the
matched items to the bbox per acquisition date, and writes one Cloud-Optimized
GeoTIFF per `(collection, date)` to `path`.

The GIS heavy lifting lives in pyramids (per the pyramids/earthlens split):
`pyramids.stac.open_client` / `resolved_href` / `read_extension_metadata`,
`pyramids.dataset.merge`'s `merge_rasters` / `stack_bands(align=True)`,
`Dataset.crop`, and `pyramids.dataset.cog.write_cog`. earthlens owns the
provider signers (`earthlens.stac.signers` — Planetary Computer / Earthdata /
CDSE bearer / CDSE S3; the generic `Signer` protocol + `anonymous` /
`aws-requester-pays` signers come from `pyramids.stac`), the endpoint ×
collection × asset catalog, and the search→load→write orchestration. There is
no `odc-stac` / `stackstac` dependency.

`OUTPUT_KIND = "raster"`, so the `EarthLens` facade forwards
`aggregate=AggregationConfig(...)` to `download()` rather than rejecting it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from earthlens.base import (
    CADENCE_ALIASES,
    AbstractDataSource,
    LazyClientMixin,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    crop_to_aoi,
    safe_filename,
    window_labels,
)

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.stac.catalog import Catalog, Endpoint


class STAC(LazyClientMixin, AbstractDataSource):
    """Unified STAC-API + COG backend (Planetary Computer / CDSE / Earth Search).

    Attributes:
        OUTPUT_KIND: `"raster"` — writes COGs; the facade forwards
            `aggregate=` (a multi-date pull composes with the aggregator).
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        endpoint: str | None = None,
        resolution: float | None = None,
        epsg: int | None = None,
        chunks: int | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        client_id: str | None = None,
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
            username: Bearer-signer username for an `earthdata` / `cdse`
                endpoint (else `EARTHDATA_USERNAME` / `CDSE_USERNAME`).
            password: Bearer-signer password for an `earthdata` / `cdse`
                endpoint (else `EARTHDATA_PASSWORD` / `CDSE_PASSWORD`).
            token: Pre-minted Earthdata bearer token for an `earthdata`
                endpoint (else `EARTHDATA_TOKEN` / `EARTHDATA_PAT`).
            client_id: Keycloak client id for a `cdse` bearer endpoint
                (defaults to `cdse-public` in the signer).
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
        self._username = username
        self._password = password
        self._token = token
        self._client_id = client_id
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
        # Per-collection signer overrides, built on demand and cached by type.
        self._signer_cache: dict[str, Any] = {}
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
        """Load the catalog and resolve the endpoint + signer (offline).

        Eager, network-free setup: validate the request, load the catalog,
        resolve the endpoint, and build the signer. Returns `None` — the
        STAC client itself is opened lazily on first access to
        `self.client` (see :meth:`_open_client`), so constructing the
        backend never opens a connection.

        Raises:
            ValueError: When `variables` is empty, names an endpoint /
                collection the catalog does not know, or mixes collections that
                are not all served by the chosen endpoint (one endpoint per
                request).
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
        # All requested collections must be served by the chosen endpoint —
        # otherwise a later collection would silently search the wrong API.
        for col_key in self._variables:
            collection = self._catalog.get_collection(col_key)
            if (
                self._endpoint != collection.endpoint
                and self._endpoint not in collection.aliases
            ):
                raise ValueError(
                    f"collection {col_key!r} is not served by endpoint "
                    f"{self._endpoint!r} (home {collection.endpoint!r}, aliases "
                    f"{sorted(collection.aliases)}). Use one endpoint per request, "
                    "or pass endpoint= explicitly."
                )
        self._signer = build_signer(
            self._endpoint_obj.signer, **self._signer_credentials()
        )
        return None

    def _open_client(self) -> Any:
        """Open the STAC API client for the resolved endpoint (lazily).

        Called by :attr:`~earthlens.base.LazyClientMixin.client` on first
        use, so the network round-trip `open_client` makes (reading the
        API's landing page) happens at `search()` / `download()` time
        rather than at construction.

        Returns:
            The opened STAC client, signed with the endpoint's signer.
        """
        from pyramids.stac import open_client

        assert self._endpoint_obj is not None  # set in _initialize
        return open_client(self._endpoint_obj.url, signer=self._signer)

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
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

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
        assert self._catalog is not None  # set in _initialize
        assert self._endpoint is not None  # resolved in _initialize
        assert isinstance(self.vars, dict)  # STAC always uses the mapping form
        for collection_key, requested in self.vars.items():
            collection = self._catalog.get_collection(collection_key)
            assets = list(requested) or list(collection.default_assets)
            resolved_id = self._catalog.resolve(self._endpoint, collection_key)
            for bbox in self._bboxes():
                search = self.client.search(
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
        from pyramids.dataset.merge import merge_rasters
        from pyramids.stac import resolved_href

        out: list[Path] = []
        # (collection_key, date, half index, path) for each written COG, so
        # download(aggregate=) can group + window them without re-parsing names.
        self._written = []
        multi = len(self._aoi_bboxes) > 1
        assert self._catalog is not None  # set in _initialize
        assert self._endpoint is not None  # resolved in _initialize
        for (collection_key, date, bbox_key), group in _group_products(products):
            # Per-collection signer (e.g. requester-pays for usgs-landsat) — its
            # GDAL env must be active for the remote reads inside merge_rasters.
            signer = self._signer_for(collection_key)
            with CloudConfig(extra=signer.gdal_env()):
                idx = self._aoi_bboxes.index(bbox_key) if multi else 0
                assets = group[0].metadata["assets"]
                band_paths: list[Path] = []
                target_crs = self._target_crs(group)
                nodata = self._nodata_for(collection_key, assets)
                # Endpoint-namespaced keys contain "/"; flatten for filenames so
                # they don't create phantom subdirectories.
                safe_key = safe_filename(collection_key)
                # Only the item lookup takes the endpoint's own key (CDSE splits
                # Sentinel-2 per resolution, so `B04` is `B04_10m` there).
                # `assets` itself stays in the catalog's naming, so the nodata
                # lookup and the written band names still match. Resolved once
                # for the whole list, which is the shape resolve_assets takes.
                item_keys = self._catalog.resolve_assets(
                    self._endpoint, collection_key, assets
                )
                for band, item_key in zip(assets, item_keys, strict=True):
                    # resolved_href resolves the asset href and applies the
                    # signer's sign_href (SAS graft / CDSE /vsis3 rewrite /
                    # no-op for requester-pays); _to_vsi then normalises a
                    # left-over s3:// to the GDAL /vsis3/ path.
                    hrefs = [
                        _to_vsi(
                            resolved_href(p.metadata["item"], item_key, signer=signer)
                        )
                        for p in group
                    ]
                    tmp = Path(self.root_dir) / f".{safe_key}_{band}_{date}_{idx}.tif"
                    # merge_rasters mosaics the tiles and, when dst_crs is given,
                    # reprojects mismatched-CRS tiles onto one grid in a single
                    # pass (multi-UTM Sentinel-2). A single tile is handled too.
                    merge_rasters(
                        [str(h) for h in hrefs],
                        str(tmp),
                        method="last",
                        dst_crs=target_crs,
                        no_data_value=nodata,
                    )
                    band_paths.append(tmp)
                stacked = self._stack_bands(band_paths, list(assets), nodata)
                # One COG per (collection, date); a crossing AOI yields one per
                # half, suffixed _part0 (eastern) / _part1 (western).
                part = f"_part{idx}" if multi else ""
                target = Path(self.root_dir) / f"{safe_key}_{date}{part}.tif"
                # crop wants the bbox as a keyword in an explicit CRS; the AOI
                # is WGS84 while the mosaic is in the tiles' native CRS. A
                # polygon aoi= masks to the exact shape (see crop_to_aoi).
                write_cog(
                    crop_to_aoi(stacked, self.space, bbox=list(bbox_key), touch=True),
                    str(target),
                )
            out.append(target)
            self._written.append((collection_key, date, idx, target))
            _cleanup(band_paths)
        logger.info(f"STAC download: {len(out)} COG(s) written to {self.root_dir}")
        return out

    def _signer_for(self, collection_key: str) -> Any:
        """Return the signer to read `collection_key`'s assets with.

        A collection may override its endpoint's signer (catalog `signer:`
        field) — e.g. a requester-pays bucket on an otherwise-anonymous
        endpoint. Built signers are cached by type.

        Args:
            collection_key: The logical collection key.

        Returns:
            The endpoint signer, or the collection's override signer.
        """
        assert self._catalog is not None  # set in _initialize
        assert self._endpoint_obj is not None  # set in _initialize
        override = self._catalog.get_collection(collection_key).signer
        if not override or override == self._endpoint_obj.signer:
            return self._signer
        cached = self._signer_cache.get(override)
        if cached is None:
            from earthlens.stac.signers import build_signer

            cached = build_signer(override, **self._signer_credentials())
            self._signer_cache[override] = cached
        return cached

    def _signer_credentials(self) -> dict[str, Any]:
        """Assemble the non-`None` credential kwargs forwarded to `build_signer`.

        `build_signer` whitelists the kwargs each signer accepts, so the full
        set is safe to pass; `None` values are dropped so an unset `client_id`
        does not override the CDSE signer's default.

        Returns:
            A mapping of the set credential kwargs (`region`, `access_key`,
            `secret_key`, `username`, `password`, `token`, `client_id`).
        """
        assert self._endpoint_obj is not None  # set in _initialize
        creds = {
            "region": self._region or self._endpoint_obj.region,
            "access_key": self._access_key,
            "secret_key": self._secret_key,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "client_id": self._client_id,
        }
        return {key: value for key, value in creds.items() if value is not None}

    def _target_crs(self, group: list[RemoteProduct]) -> int | None:
        """Pick the mosaic target CRS for a date group, without opening rasters.

        Returns the user's `epsg` when set; otherwise, if the group's tiles
        report differing `proj:epsg` in their STAC metadata, the lowest of those
        (so `merge_rasters` reprojects them onto one grid); otherwise `None`
        (tiles share a CRS — `merge_rasters` keeps it). Reading the CRS from item
        metadata avoids opening every remote tile just to compare CRSs.

        Args:
            group: The products for one `(collection, date, half)`.

        Returns:
            An EPSG code to reproject to, or `None` to keep the native CRS.
        """
        if self._epsg:
            return self._epsg
        epsgs = {e for e in (_item_epsg(p.metadata["item"]) for p in group) if e}
        return sorted(epsgs)[0] if len(epsgs) > 1 else None

    def _nodata_for(self, collection_key: str, assets: list[str]) -> float | int:
        """Pick a dtype-safe no-data value for the mosaic/stack of a collection.

        Uses the catalog asset's `nodata` for the first requested asset that
        declares one, else `0`. pyramids' default no-data (`-9999`) overflows
        unsigned dtypes such as Sentinel-2's `uint16`, so a catalog-driven
        (or `0`) value is passed to `merge_rasters` / `stack_bands` instead.

        Args:
            collection_key: The logical collection key.
            assets: The requested asset keys, in priority order.

        Returns:
            The no-data value to write (catalog `nodata`, or `0`).
        """
        assert self._catalog is not None  # set in _initialize
        try:
            collection = self._catalog.get_collection(collection_key)
        except ValueError:
            return 0
        for band in assets:
            asset = collection.assets.get(band)
            if asset is not None and asset.nodata is not None:
                return asset.nodata
        return 0

    def _stack_bands(
        self, band_paths: list[Path], assets: list[str], nodata: float | int
    ) -> Any:
        """Stack per-band mosaics into one multiband `Dataset`.

        Delegates to pyramids' `stack_bands(align=True, no_data_value=…)`: same
        -resolution bands stack directly, and mixed-resolution bands (e.g.
        Sentinel-2 `red` 10 m + `swir16` 20 m) are resampled onto the **first**
        requested band's grid. `no_data_value` is threaded through so the grid
        template adopts a dtype-safe fill — pyramids' earlier `from_band_files`
        default of -9999 overflowed unsigned dtypes such as `uint16`; that is
        fixed upstream, so the previous `Dataset.align` + `from_array`
        workaround is no longer needed.

        Args:
            band_paths: One single-band mosaic per requested asset, in order.
            assets: The requested asset keys (used as band names); the first
                also defines the output grid for mixed-resolution stacks.
            nodata: The no-data value to stamp on the output.

        Returns:
            A pyramids `Dataset` with one band per `band_paths` entry.
        """
        from pyramids.dataset.merge import stack_bands

        return stack_bands(
            [str(p) for p in band_paths],
            band_names=list(assets),
            align=True,
            no_data_value=nodata,
        )

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
                When set, the per-date COGs are reduced per time window into
                per-window COGs (see :meth:`_aggregate_cogs`) and the per-date
                intermediates are removed.

        Returns:
            The written COG paths: one per `(collection, date)` when `aggregate`
            is `None`, or one per `(collection, window)` when `aggregate` is set.

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
        from pyramids.dataset import Dataset, DatasetCollection, GeoReference
        from pyramids.dataset.cog import write_cog

        op = "mean" if config.op == "auto" else config.op
        out_dir = (
            Path(config.out_dir) if config.out_dir is not None else Path(self.root_dir)
        )
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
            labels = window_labels(dates, config.freq)
            collection = DatasetCollection.from_files(files)
            reduced = getattr(collection.groupby(labels), op)(skipna=config.skipna)
            geo, epsg = _geo_of(Dataset, files[0])
            part = f"_part{idx}" if multi else ""
            # Flatten endpoint-namespaced keys ("eodc/gfm") so the "/" does not
            # target a non-existent subdirectory (matches _fetch's filenames).
            safe_key = safe_filename(collection_key)
            for label, array in reduced.items():
                target = out_dir / f"{safe_key}_{op}_{config.freq}_{label}{part}.tif"
                write_cog(
                    Dataset.from_array(
                        arr=array, geo_ref=GeoReference(geo=geo, epsg=epsg)
                    ),
                    str(target),
                )
                written.append(target)
        # The per-date COGs are intermediates of the aggregation; drop them so
        # the caller is left with only the per-window outputs.
        _cleanup([path for _, _, _, path in self._written])
        logger.info(
            f"STAC aggregate: {len(self._written)} COG(s) -> {len(written)} "
            f"window COG(s) (time/{config.freq} {op}) in {out_dir}"
        )
        return written


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


def _item_epsg(item: Any) -> int | None:
    """Return a STAC item's `proj:epsg` from its properties, or `None`.

    Delegates to `pyramids.stac.read_extension_metadata`, which reads the
    item's `proj` extension (resolving `proj:code` / `proj:epsg`) the same way
    for a pystac `Item` or a raw STAC dict — so earthlens does not re-parse the
    projection extension itself.

    Args:
        item: A pystac `Item` or a raw STAC item dict.

    Returns:
        The integer EPSG code declared by the item, or `None` when absent.

    Examples:
        - Read the CRS from a raw STAC item dict:
            ```python
            >>> from earthlens.stac.backend import _item_epsg
            >>> _item_epsg({"properties": {"proj:epsg": 32631}, "assets": {}})
            32631

            ```
        - An item without `proj:epsg` yields `None`:
            ```python
            >>> from earthlens.stac.backend import _item_epsg
            >>> _item_epsg({"properties": {}, "assets": {}}) is None
            True

            ```
    """
    from pyramids.stac import read_extension_metadata

    return cast("int | None", read_extension_metadata(item, None).get("epsg"))


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
        return cast("str", dttm.strftime("%Y-%m-%d"))
    props = getattr(item, "properties", None)
    if props is None and isinstance(item, dict):
        props = item.get("properties")
    props = props or {}
    value = props.get("datetime") if isinstance(props, dict) else None
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return "unknown"


def _to_vsi(href: str) -> str:
    """Rewrite an `s3://bucket/key` href to the GDAL `/vsis3/bucket/key` path.

    GDAL cannot open an `s3://` URL directly. Signers that only set the GDAL env
    (e.g. the requester-pays signer) leave the href as `s3://`, so normalise it
    here so the asset is actually readable. Non-`s3://` hrefs are returned
    unchanged (an already-`/vsis3/`-rewritten CDSE href is left as-is).

    Args:
        href: A (possibly already signed) asset href.

    Returns:
        The GDAL-readable href.
    """
    if href.startswith("s3://"):
        return "/vsis3/" + href[len("s3://") :]
    return href


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
