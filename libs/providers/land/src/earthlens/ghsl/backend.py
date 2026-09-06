"""GHSL backend — JRC Global Human Settlement Layer over open HTTPS.

`GHSL` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`).
A request is a bbox + time window + a list of GHSL **product keys**
(`variables=["GHS_POP", ...]`, canonical or friendly aliases) plus
`release` / `epoch(s)` / `resolution` / `crs` / `tiling` / `api` kwargs. The
backend resolves the matching epochs against the bundled availability
catalog, builds the deterministic JRC `.zip` URL(s) (or STAC-searches),
downloads the intersecting Mollweide tiles (or the whole-globe file) over
anonymous HTTPS, then uses `pyramids` to mosaic, reproject (Mollweide →
the output CRS), and crop to the AOI — writing one GeoTIFF per
`(product, epoch)`.

The provider is **open, attribution-only** — no credentials (see
`earthlens.ghsl.auth`). The GIS work happens locally in `pyramids`, so this
is a genuine pyramids-consuming backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    import numpy as np

    from earthlens.aggregate import AggregationConfig

from earthlens.base import OutputKind, close_quietly, date_windows, to_datetime
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.base.spatial import crop_to_aoi
from earthlens.ghsl._helpers import (
    BASE_URL,
    download_and_extract,
    download_and_unzip,
    ghsl_url,
    latest_version_dir,
    list_remote_dir,
    tiles_for_bbox,
)
from earthlens.ghsl.auth import GhslAuth
from earthlens.ghsl.catalog import Catalog, native_source_crs

#: Allowed values for the `api=` access-path selector.
_API_MODES: frozenset[str] = frozenset({"direct", "stac"})
#: Allowed values for the `tiling=` selector.
_TILING_MODES: frozenset[str] = frozenset({"auto", "global"})
#: The primary release auto-resolution prefers when `release=` is omitted.
_DEFAULT_RELEASE: str = "R2023A"


class GHSL(AbstractDataSource):
    """Download GHSL products from the JRC over open HTTPS, localised via pyramids.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every product yields a gridded
            GeoTIFF, so the facade forwards `aggregate=` (reduced across
            epochs).
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: Set by `download(force=...)`; bypasses the skip-if-exists check.
    _force: bool = False

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "yearly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        release: str | None = None,
        epoch: int | None = None,
        epochs: list[int] | None = None,
        resolution: str | None = None,
        crs: str = "EPSG:4326",
        tiling: str = "auto",
        api: str = "direct",
        catalog: Catalog | None = None,
    ):
        """Initialise a GHSL backend instance.

        Resolves and statically validates every requested product against
        the availability catalog **before** the parent constructor runs
        (the parent calls `_initialize` first, where `self.vars` is not yet
        set). Epoch validation is deferred until the epoch list is derived
        from the date window.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`);
                its year selects the first GHSL epoch in range.
            end: Inclusive end of the date window.
            variables: GHSL product keys — canonical (`"GHS_POP"`) or
                friendly aliases (`"population"`, `"settlement_model"`, …).
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only (GHSL epochs are
                5-yearly points); accepted for facade parity. Defaults to
                `"yearly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            release: GHSL release id, or `None` (default) to auto-resolve per
                product via `_release_for`. An **explicit** release must be
                available for every requested product (an unavailable one
                raises — a typo is not silently accepted). When `None`, each
                product resolves to the default release `"R2023A"` if it offers
                it, else to its single release (so single-release products like
                `GHS_LAND` (R2022A), the R2019A/R2024A statistical families, and
                the R2025A WUP family work without an explicit release, and one
                request may mix products from different releases); a product
                with several releases, none of them the default, raises.
            epoch: A single reference year to fetch (overrides the date
                window). Mutually informative with `epochs`.
            epochs: An explicit list of reference years (overrides the date
                window and `epoch`).
            resolution: Friendly resolution label (`"100m"`, `"1km"`,
                `"3ss"`, `"30ss"`, `"10m"`). `None` uses each product's
                `default_resolution`.
            crs: Output CRS as an EPSG string / code (default `"EPSG:4326"`).
                The source CRS is implied by `resolution`; the backend
                reprojects to this when they differ.
            tiling: `"auto"` (tile-select + mosaic for fine resolutions) or
                `"global"` (always download the whole-globe file).
            api: Access path — `"direct"` (deterministic URL builder,
                default) or `"stac"` (JRC STAC search, when available).
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a product / alias is
                unknown, the release or resolution is unavailable for a
                product, or `crs` / `tiling` / `api` is malformed.
        """
        if not variables:
            raise ValueError(
                "GHSL requires a non-empty `variables` list of product keys, "
                'e.g. ["GHS_POP"] or ["population"].'
            )
        if api not in _API_MODES:
            raise ValueError(f"api must be one of {sorted(_API_MODES)}; got {api!r}.")
        if tiling not in _TILING_MODES:
            raise ValueError(
                f"tiling must be one of {sorted(_TILING_MODES)}; got {tiling!r}."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._release = release
        self._release_cache: dict[str, str] = {}
        self._epoch_arg = epoch
        self._epochs_arg = epochs
        self._resolution_arg = resolution
        self._crs = crs
        self._output_epsg = _epsg_int(crs)
        self._tiling = tiling
        # `_api` is intentionally reused as an instance-level access-path string
        # ("direct"/"stac"); GHSL drives downloads through its own download()
        # rather than the inherited _api() method.
        self._api = api  # type: ignore[method-assign, assignment]
        self._auth = GhslAuth()
        # Resolve + statically validate (product / release / resolution) up
        # front; epoch validation happens once the epoch list is derived.
        self._codes: list[str] = [self._catalog.resolve(v) for v in variables]
        self._validate_static()

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

    def _validate_static(self) -> None:
        """Check release + resolution availability for every product.

        Epoch validation is deferred (the epoch list is not known until the
        date window is parsed); this catches the static dimensions early so
        a bad `release` / `resolution` fails at construction.

        Raises:
            ValueError: If a product has no usable release (see
                `_release_for`), or the requested (or its default) resolution
                is not available for it.
        """
        for code in self._codes:
            product = self._catalog.get(code)
            release = self._release_for(code)
            resolution = self._resolution_for(code)
            resolutions = product.release_resolutions(release)
            if resolution not in resolutions:
                raise ValueError(
                    f"{code} ({release}) has no resolution "
                    f"{resolution!r}; available: {resolutions}."
                )

    def _release_for(self, code: str) -> str:
        """Resolve (and memoise) the release to use for one product.

        When `release=` was given explicitly, it must be available for the
        product — an unavailable release raises, so a typo is never silently
        accepted. When `release` is `None` (auto), the product resolves to the
        default release `_DEFAULT_RELEASE` if it offers it, else to its single
        release (so single-release products like `GHS_LAND` (R2022A) or
        `GHS_FUA_UCDB2015` (R2019A) work without forcing `release=`, and a
        request can mix products at different releases); a product with several
        releases, none of them the default, is ambiguous and raises. The result
        is cached per code so it is computed once.

        Args:
            code: A canonical product code.

        Returns:
            str: The release id to use for this product.

        Raises:
            ValueError: If the product has no usable release for the request.
        """
        cached = self._release_cache.get(code)
        if cached is not None:
            return cached
        product = self._catalog.get(code)
        if self._release is not None:
            if self._release not in product.releases:
                raise ValueError(
                    f"{code} has no release {self._release!r}; "
                    f"available releases: {sorted(product.releases)}."
                )
            resolved = self._release
        elif _DEFAULT_RELEASE in product.releases:
            resolved = _DEFAULT_RELEASE
        elif len(product.releases) == 1:
            resolved = next(iter(product.releases))
        else:
            raise ValueError(
                f"{code} is published at several releases "
                f"{sorted(product.releases)}, none of them the default "
                f"{_DEFAULT_RELEASE!r}; pass an explicit release=."
            )
        self._release_cache[code] = resolved
        return resolved

    def _resolution_for(self, code: str) -> str:
        """Return the resolution to use for one product.

        Args:
            code: A canonical product code.

        Returns:
            str: The explicit `resolution=` if given, else the product's
                `default_resolution`.

        Raises:
            ValueError: If neither is available (the product declares no
                default and the request omitted `resolution=`).
        """
        if self._resolution_arg is not None:
            return self._resolution_arg
        default = self._catalog.get(code).default_resolution
        if default is None:
            raise ValueError(
                f"{code} declares no default_resolution; pass resolution=."
            )
        return default

    def _initialize(self):
        """Configure the (no-op) auth; no network client is created.

        Returns:
            None: GHSL is open + anonymous, so the parent binds no
                `self.client`.
        """
        self._auth.configure()
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent`.

        The window's years select the GHSL epochs in range (the discrete
        5-yearly steps); the per-epoch expansion happens in `_epochs`.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: Advisory label (ignored).
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        dates = date_windows(start_dt, end_dt, "YS")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="YS",
            dates=dates,
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs.

        Args:
            progress_bar: Whether per-download progress is shown.
            force: Re-fetch even when a complete output already exists,
                bypassing the skip-if-exists check. Defaults to `False`.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-epoch raster stack across the epochs in range
                (`C6`). Rejected for categorical products (averaging class
                codes is meaningless).

        Returns:
            list[Path]: One GeoTIFF per `(product, epoch)`, or — when
                `aggregate` is set — the per-window reduced rasters.

        Raises:
            ValueError: If `aggregate` is set and any requested product is
                categorical.
        """
        self._show_progress = progress_bar
        self._force = force
        if aggregate is not None:
            categorical = [c for c in self._codes if self._catalog.get(c).categorical]
            if categorical:
                raise ValueError(
                    f"cannot aggregate class codes for categorical product(s) "
                    f"{categorical}; averaging class codes is meaningless. "
                    "Drop aggregate= (or request a continuous product)."
                )
        products = self._search()
        if not products:
            return []
        paths = self._fetch(products)
        if aggregate is None:
            return paths
        return self._aggregate_epochs(products, paths, aggregate)

    def _aggregate_epochs(
        self,
        products: list[RemoteProduct],
        paths: list[Path],
        config: AggregationConfig,
    ) -> list[Path]:
        """Reduce each product's per-epoch raster stack into per-window rasters.

        Groups the written GeoTIFFs by product (the epochs share an identical
        grid after `_localise`), buckets the discrete epochs into windows with
        `config.freq` (GHSL epochs are 5-yearly points, so a coarse `freq`
        collapses them to one output), and reduces each window's stack with
        `config.op` (`"auto"` resolves to `"mean"`) via the shared
        `earthlens.aggregate` reducer. One GeoTIFF per `(product, window)`.

        Args:
            products: The plan from `_search` (same order as `paths`).
            paths: The per-`(product, epoch)` GeoTIFFs from `_fetch`.
            config: The aggregation spec (`freq`, `op`, `skipna`, `min_count`,
                `out_dir`).

        Returns:
            list[Path]: The per-window reduced GeoTIFFs (written under
                `config.out_dir` when set, else `self.path`), plus any tabular
                product outputs passed through unchanged.
        """
        from collections import defaultdict

        import numpy as np
        from pyramids.dataset import Dataset, GeoReference

        from earthlens.aggregate import reduce_time_axis, window_groups

        op = "mean" if config.op == "auto" else config.op
        out_dir = (
            Path(config.out_dir) if config.out_dir is not None else Path(self.path)
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[tuple[int, str, Path]]] = defaultdict(list)
        # Tabular products have no epoch stack to reduce; pass their written
        # outputs through unchanged rather than dropping them from the result.
        passthrough: list[Path] = []
        for product, path in zip(products, paths):
            if product.metadata.get("kind") != "raster":
                passthrough.append(path)
                continue
            groups[product.metadata["product"]].append(
                (product.metadata["epoch"], product.metadata["resolution"], path)
            )

        out: list[Path] = list(passthrough)
        for code, items in groups.items():
            items.sort(key=lambda triple: triple[0])
            epochs = [epoch for epoch, _, _ in items]
            resolution = items[0][1]
            datasets = [Dataset.read_file(str(path)) for _, _, path in items]
            bands = [self._first_band(ds.read_array()) for ds in datasets]
            shapes = {epoch: band.shape for epoch, band in zip(epochs, bands)}
            if len(set(shapes.values())) > 1:
                raise ValueError(
                    f"cannot aggregate {code}: its per-epoch grids differ in "
                    f"shape and cannot be stacked — {shapes}. This usually means "
                    "the epochs resolved to different tile sets or a download was "
                    "incomplete; re-run, or request a single epoch."
                )
            stack = np.stack(bands).astype("float64")
            time_axis = pd.to_datetime([f"{epoch}-01-01" for epoch in epochs])
            geo = datasets[0].geotransform
            nodata = datasets[0].no_data_value
            fill = nodata[0] if isinstance(nodata, (list, tuple)) else nodata
            for label, mask in window_groups(time_axis, config.freq):
                reduced = reduce_time_axis(
                    stack[mask],
                    op,
                    skipna=config.skipna,
                    min_count=config.min_count,
                )
                result = Dataset.from_array(
                    reduced,
                    no_data_value=fill if fill is not None else -9999,
                    geo_ref=GeoReference(geo=geo, epsg=self._output_epsg),
                )
                target = out_dir / (
                    f"{code}_{op}_{label.strftime('%Y')}_{resolution}"
                    f"_epsg{self._output_epsg}.tif"
                )
                result.to_file(str(target))
                close_quietly(result)
                out.append(target)
            for ds in datasets:
                close_quietly(ds)
        return out

    @staticmethod
    def _first_band(arr) -> np.ndarray:
        """Return the first band of a raster array (2-D as-is, 3-D's band 0)."""
        import numpy as np

        array = np.asarray(arr)
        return cast("np.ndarray", array[0] if array.ndim == 3 else array)

    @property
    def _raw_dir(self) -> Path:
        """Directory the downloaded `.zip`/`.tif` artefacts are cached in."""
        return self.root_dir / ".ghsl_cache"

    @property
    def _bbox(self) -> tuple[float, float, float, float]:
        """The AOI as `(west, south, east, north)` in degrees."""
        return (self.space.west, self.space.south, self.space.east, self.space.north)

    def _epochs_for(self, code: str) -> list[int]:
        """Resolve the epochs to fetch for one product (`C4`; refined in `C6`).

        Precedence: an explicit `epochs=` list wins; then a single `epoch=`;
        otherwise the catalog epochs falling in the `[start, end]` year range.
        When the range is narrower than the 5-year step (no epoch in range),
        snaps to the single nearest catalog epoch.

        Args:
            code: A canonical product code.

        Returns:
            list[int]: Sorted epochs to fetch (a subset of the product's
                catalog epochs for the release).

        Raises:
            ValueError: If an explicit epoch is not available for the product.
        """
        available = self._catalog.get(code).release_epochs(self._release_for(code))
        if self._epochs_arg is not None:
            requested = list(self._epochs_arg)
        elif self._epoch_arg is not None:
            requested = [self._epoch_arg]
        else:
            y0, y1 = self.time.start_date.year, self.time.end_date.year
            requested = [e for e in available if y0 <= e <= y1]
            if not requested:
                midpoint = (y0 + y1) / 2
                nearest = min(available, key=lambda e: abs(e - midpoint))
                logger.info(
                    f"GHSL: no {code} epoch in [{y0}, {y1}]; snapping to the "
                    f"nearest catalog epoch {nearest}."
                )
                requested = [nearest]
        unknown = [e for e in requested if e not in available]
        if unknown:
            raise ValueError(
                f"{code} ({self._release_for(code)}) has no epoch(s) {unknown}; "
                f"available epochs: {available}."
            )
        return sorted(set(requested))

    def _urls_for(self, code: str, epoch: int) -> list[str]:
        """Build the JRC `.zip` URL(s) for one `(product, epoch)`.

        Returns the intersecting per-tile URLs for a tiled fine-resolution
        product (unless `tiling="global"`), otherwise the single whole-globe
        URL.

        Args:
            code: A canonical product code.
            epoch: A reference year (already validated for the product).

        Returns:
            list[str]: One or more `.zip` URLs.

        Raises:
            ValueError: If the product is tiled but no land tile intersects
                the AOI.
        """
        product = self._catalog.get(code)
        release = self._release_for(code)
        resolution = self._resolution_for(code)
        block = product.block_for(release, epoch, resolution)
        # The epoch was validated for this (product, release, resolution), so a
        # matching availability block always exists here.
        assert block is not None
        family = product.family_token()
        url_kw: dict[str, Any] = dict(
            version=block.version, region=block.region, nested=block.nested
        )
        is_tiled = resolution in block.tiled() and self._tiling != "global"
        if not is_tiled:
            return [ghsl_url(family, code, epoch, release, resolution, **url_kw)]
        tiles = tiles_for_bbox(self._bbox)
        if not tiles:
            raise ValueError(
                f"no GHSL land tiles intersect the AOI {self._bbox} for {code} "
                f"at {resolution}; the area may be entirely ocean. Use a land "
                "AOI, a coarser whole-globe resolution, or tiling='global'."
            )
        return [
            ghsl_url(family, code, epoch, release, resolution, tile=t, **url_kw)
            for t in tiles
        ]

    def _search(self) -> list[RemoteProduct]:
        """Resolve the request to one `RemoteProduct` per `(product, epoch)`.

        No network: each product carries its resolved `.zip` URLs (or, for
        tabular products, a `kind="tabular"` flag routing it to the DUC
        side-table fetch) in `metadata` so a dry-run inspection is cheap.

        Returns:
            list[RemoteProduct]: The download plan.
        """
        plan: list[RemoteProduct] = []
        for code in self._codes:
            product = self._catalog.get(code)
            if product.kind == "tabular":
                plan.append(
                    RemoteProduct(
                        id=code,
                        metadata={"product": code, "kind": "tabular"},
                    )
                )
                continue
            resolution = self._resolution_for(code)
            release = self._release_for(code)
            for epoch in self._epochs_for(code):
                self._catalog.validate(code, release, epoch, resolution)
                plan.append(
                    RemoteProduct(
                        id=f"{code}_E{epoch}",
                        metadata={
                            "product": code,
                            "epoch": epoch,
                            "resolution": resolution,
                            "categorical": product.categorical,
                            "kind": "raster",
                            "urls": self._urls_for(code, epoch),
                        },
                    )
                )
        return plan

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download + unzip each product's tiles, then localise via pyramids.

        The per-tile downloads of a product are run concurrently with a bounded
        `joblib` thread pool (the network is the bottleneck; each
        `download_and_unzip` is idempotent and uses its own request), then the
        mosaic / reproject / crop runs once per product.

        Args:
            products: The plan from `_search`.

        Returns:
            list[Path]: One written GeoTIFF (or table) path per product.
        """
        if self._api == "stac":
            return self._fetch_via_stac(products)
        written: list[Path] = []
        for rp in products:
            if rp.metadata.get("kind") == "tabular":
                written.append(self._fetch_duc(rp))
                continue
            tifs = self._download_tiles(rp.metadata["urls"])
            written.append(self._localise(tifs, rp))
        return written

    def _download_tiles(self, urls: list[str]) -> list[Path]:
        """Download + unzip a product's tile URLs concurrently (order preserved).

        Args:
            urls: The `.zip` URLs for one `(product, epoch)`.

        Returns:
            list[Path]: The extracted `.tif` paths, in `urls` order.
        """
        if len(urls) == 1:
            return [download_and_unzip(urls[0], self._raw_dir)]
        from joblib import Parallel, delayed

        n_jobs = min(8, len(urls))
        return cast(
            "list[Path]",
            Parallel(n_jobs=n_jobs, prefer="threads")(
                delayed(download_and_unzip)(url, self._raw_dir) for url in urls
            ),
        )

    def _localise(self, tifs: list[Path], rp: RemoteProduct) -> Path:
        """Mosaic + reproject + crop the downloaded tiles into one GeoTIFF.

        The pyramids-consuming core: `merge_rasters` mosaics the tiles **and**
        reprojects them to the output CRS in one call (skipped when the source
        already matches the output CRS), then `Dataset.crop` clips to the AOI
        bbox. The mosaic inherits the source tiles' declared no-data (JRC uses
        `-200`, or `65535` on the uint16 products) instead of `merge_rasters`'
        `0` default, which would mask every zero-population / zero-built-up
        cell and promote the real sentinel to valid data. Categorical products reproject with nearest-neighbour (so class
        codes are never blended); those with a curated legend also carry a
        colour table + a `.legend.json` sidecar, while a legend-less categorical
        product (e.g. `GHS_BUILT_C_VEG`) still gets the safe NN resampling but
        no colour table or sidecar.

        Args:
            tifs: The downloaded source `.tif` tiles (Mollweide or WGS84).
            rp: The `RemoteProduct` (its `metadata` carries `product`,
                `epoch`, `resolution`, `categorical`).

        Returns:
            pathlib.Path: The AOI-cropped GeoTIFF written under `self.path`.
                Returned without re-running the GIS pipeline when it already
                exists (idempotent, like the tile-level cache).
        """
        from pyramids.dataset import Dataset
        from pyramids.dataset.merge import merge_rasters

        resolution = rp.metadata["resolution"]
        target = Path(self.path) / (f"{rp.id}_{resolution}_epsg{self._output_epsg}.tif")
        if self._is_complete(target, force=self._force):
            return target
        categorical = rp.metadata["categorical"]
        resampling = "nearest neighbor" if categorical else "bilinear"
        source_is_wgs84 = native_source_crs(resolution) == "4326"
        reproject = not (source_is_wgs84 and self._output_epsg == 4326)
        dst_crs = self._output_epsg if reproject else None

        self._raw_dir.mkdir(parents=True, exist_ok=True)
        merged = self._raw_dir / f"{rp.id}_merged.tif"
        # merge_rasters defaults no_data_value to 0, which is doubly wrong for
        # GHSL: 0 is an ordinary value (no population, no built-up surface), and
        # JRC's own sentinel (-200, or 65535 on the uint16 products) would be
        # demoted to valid data. Inherit what the source tiles declare.
        first_tile = Dataset.read_file(tifs[0])
        source_no_data = first_tile.no_data_value
        first_tile.close()
        fill = (
            source_no_data[0]
            if isinstance(source_no_data, (list, tuple))
            else source_no_data
        )
        merge_rasters(
            src=[str(t) for t in tifs],
            dst=str(merged),
            dst_crs=dst_crs,
            resampling=resampling,
            no_data_value=fill if fill is not None else "none",
        )

        dataset = Dataset.read_file(str(merged))
        cropped = crop_to_aoi(
            dataset,
            self.space,
            bbox=[self.space.west, self.space.south, self.space.east, self.space.north],
            touch=False,
        )
        # A categorical product reprojects with nearest-neighbour (set above)
        # regardless of whether its class legend is curated; the colour table +
        # legend sidecar are only written when a legend exists (a legend-less
        # categorical product — e.g. GHS_BUILT_C_VEG, whose class codes are not
        # curated — still gets the safe NN resampling, just no colour table).
        has_legend = categorical and bool(
            self._catalog.get(rp.metadata["product"]).legend
        )
        if has_legend:
            # The GeoTIFF colour table is best-effort: pyramids needs its
            # optional viz extra to write one. The legend always survives via
            # the {target}.legend.json sidecar written below.
            try:
                cropped.color_table = self._catalog.get(
                    rp.metadata["product"]
                ).color_table()
            except Exception as exc:  # noqa: BLE001 - optional colour-table dep
                logger.warning(
                    f"GHSL: could not embed the colour table for "
                    f"{rp.metadata['product']} ({type(exc).__name__}); the "
                    "legend sidecar is still written."
                )

        # Write through a sibling and rename, so a crash mid-write cannot
        # leave a partial file that the skip-if-exists check would accept.
        # The real suffix is kept — pyramids picks its driver from the
        # extension, and a bare `.part` matches none.
        staged = target.with_name(f"{target.stem}.part{target.suffix}")
        try:
            cropped.to_file(str(staged))
            # pyramids keeps the written dataset's GDAL handle open, which
            # holds a Windows lock and blocks the rename; release it first.
            close_quietly(cropped)
            staged.replace(target)
        except BaseException:
            # A failed write must not strand the `.part` for the next run to
            # trip over, and both handles have to go either way.
            close_quietly(cropped)
            close_quietly(dataset)
            staged.unlink(missing_ok=True)
            raise
        if has_legend:
            self._write_legend_sidecar(target, rp.metadata["product"])
        close_quietly(dataset)
        # Best-effort cleanup of the merge intermediate; on Windows the GDAL
        # handle can briefly outlive the Python object, so a locked file is
        # left in the cache rather than raising.
        try:
            merged.unlink(missing_ok=True)
        except OSError:
            pass
        return target

    def _fetch_via_stac(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch via the JRC STAC search path — unconfirmed, raises (`C9`/`G1`).

        The `H21` spec mentioned "STAC at `data.jrc.ec.europa.eu`", but the
        `A1` verification (2026-05) found JRC exposes only a STAC *browser*, no
        queryable STAC API (`/stac`, `/collections` all `404`). Until a stable
        endpoint is confirmed, `api="stac"` raises this documented error rather
        than guessing a URL; the deterministic `api="direct"` path is fully
        functional and is the default.

        Args:
            products: The plan from `_search` (unused — the path is inactive).

        Raises:
            ValueError: Always — directing the caller to `api="direct"`.
        """
        raise ValueError(
            "GHSL api='stac' is unavailable: no stable, queryable JRC STAC API "
            "endpoint is confirmed (verified 2026-05 — JRC exposes a STAC "
            "browser only). Use api='direct' (the default), which serves every "
            "GHSL product over the deterministic HTTPS file tree."
        )

    def _write_legend_sidecar(self, target: Path, code: str) -> None:
        """Write a `{target}.legend.json` class-code → label sidecar.

        Ensures the categorical legend survives regardless of whether the
        GeoTIFF colour table is preserved by downstream readers.

        Args:
            target: The written categorical GeoTIFF.
            code: The product code whose legend to serialise.
        """
        import json

        legend = self._catalog.get(code).legend or {}
        sidecar = target.with_suffix(".legend.json")
        sidecar.write_text(
            json.dumps({str(k): v for k, v in legend.items()}, indent=2),
            encoding="utf-8",
        )

    def _fetch_duc(self, rp: RemoteProduct) -> Path:
        """Download a tabular DUC / WUP-statistics product as a side table.

        Tabular products do not follow the per-epoch raster URL convention:
        their payload is a single `.zip` (CSV / GeoPackage / xlsx) under the
        latest `V{maj}-{min}` directory of the product family. This
        auto-discovers that directory + zip, downloads and extracts it under
        `self.path/{code}/`, and returns that directory. No mosaic / reproject
        / crop, and no GADM polygon join (a scope guard).

        Args:
            rp: The tabular `RemoteProduct` (its `metadata["product"]` names
                the catalog code).

        Returns:
            pathlib.Path: The directory the table was extracted into.

        Raises:
            ValueError: If no version directory / `.zip` is found upstream.
        """
        code = rp.metadata["product"]
        product = self._catalog.get(code)
        release = self._release_for(code)
        blocks = product.releases.get(release) or []
        region = blocks[0].region if blocks else "GLOBE"
        family_url = f"{BASE_URL}/{product.family_token()}_{region}_{release}"
        version = latest_version_dir(family_url)
        version_url = f"{family_url}/{version}"
        zips = sorted(n for n in list_remote_dir(version_url) if n.endswith(".zip"))
        if not zips:
            raise ValueError(f"no .zip table found under {version_url} for {code}.")
        if len(zips) > 1:
            logger.warning(
                f"GHSL: {code} {version} has multiple table zips {zips}; "
                f"downloading the first ({zips[0]})."
            )
        dest: Path = Path(self.path) / code
        download_and_extract(f"{version_url}/{zips[0]}", dest)
        return dest


def _epsg_int(crs: str | int) -> int:
    """Parse an EPSG string / code to its integer code.

    Args:
        crs: An EPSG code as `4326`, `"4326"`, or `"EPSG:4326"`
            (case-insensitive).

    Returns:
        int: The numeric EPSG code.

    Raises:
        ValueError: If `crs` is not a recognised EPSG code.

    Examples:
        - Accepts the common spellings:
            ```python
            >>> from earthlens.ghsl.backend import _epsg_int
            >>> _epsg_int("EPSG:4326")
            4326
            >>> _epsg_int(3035)
            3035

            ```
    """
    if isinstance(crs, int):
        return crs
    text = str(crs).strip().upper()
    if text.startswith("EPSG:"):
        text = text[len("EPSG:") :]
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"could not parse {crs!r} as an EPSG code "
            "(expected e.g. 4326 or 'EPSG:4326')."
        ) from None
