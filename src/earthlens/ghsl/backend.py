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

import datetime as dt
from pathlib import Path

import pandas as pd
import requests
from loguru import logger

from earthlens.base import OutputKind
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.ghsl._helpers import download_and_unzip, ghsl_url, tiles_for_bbox
from earthlens.ghsl.auth import GhslAuth
from earthlens.ghsl.catalog import Catalog, native_source_crs

#: Allowed values for the `api=` access-path selector.
_API_MODES: frozenset[str] = frozenset({"direct", "stac"})
#: Allowed values for the `tiling=` selector.
_TILING_MODES: frozenset[str] = frozenset({"auto", "global"})


class GHSL(AbstractDataSource):
    """Download GHSL products from the JRC over open HTTPS, localised via pyramids.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every product yields a gridded
            GeoTIFF, so the facade forwards `aggregate=` (reduced across
            epochs).
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "yearly",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        release: str = "R2023A",
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
            release: GHSL release id — `"R2023A"` (default), `"R2022A"`
                (LAND), or `"R2025A"` (WUP projections).
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
            raise ValueError(
                f"api must be one of {sorted(_API_MODES)}; got {api!r}."
            )
        if tiling not in _TILING_MODES:
            raise ValueError(
                f"tiling must be one of {sorted(_TILING_MODES)}; got {tiling!r}."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._release = release
        self._epoch_arg = epoch
        self._epochs_arg = epochs
        self._resolution_arg = resolution
        self._crs = crs
        self._output_epsg = _epsg_int(crs)
        self._tiling = tiling
        self._api = api
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
            ValueError: If a product has no such release, or the requested
                (or its default) resolution is not available for it.
        """
        for code in self._codes:
            product = self._catalog.get(code)
            if self._release not in product.releases:
                raise ValueError(
                    f"{code} has no release {self._release!r}; "
                    f"available releases: {sorted(product.releases)}."
                )
            resolution = self._resolution_for(code)
            resolutions = product.release_resolutions(self._release)
            if resolution not in resolutions:
                raise ValueError(
                    f"{code} ({self._release}) has no resolution "
                    f"{resolution!r}; available: {resolutions}."
                )

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

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a `SpatialExtent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox (WGS84).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        dates = pd.date_range(start_dt, end_dt, freq="YS")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="YS",
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True, aggregate=None) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs.

        Args:
            progress_bar: Whether per-download progress is shown.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-epoch raster stack (`C6`).

        Returns:
            list[Path]: One GeoTIFF per `(product, epoch)`, or — when
                `aggregate` is set — the per-window reduced rasters.
        """
        self._show_progress = progress_bar
        self._aggregate_cfg = aggregate
        return self._api_via_search_fetch()

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
        available = self._catalog.get(code).release_epochs(self._release)
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
                f"{code} ({self._release}) has no epoch(s) {unknown}; "
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
        resolution = self._resolution_for(code)
        block = product.block_for(self._release, epoch, resolution)
        version = block.version
        family = product.family_token()
        is_tiled = resolution in block.tiled() and self._tiling != "global"
        if not is_tiled:
            return [
                ghsl_url(family, code, epoch, self._release, resolution, version=version)
            ]
        tiles = tiles_for_bbox(self._bbox)
        if not tiles:
            raise ValueError(
                f"no GHSL land tiles intersect the AOI {self._bbox} for {code} "
                f"at {resolution}; the area may be entirely ocean. Use a land "
                "AOI, a coarser whole-globe resolution, or tiling='global'."
            )
        return [
            ghsl_url(family, code, epoch, self._release, resolution, tile=t, version=version)
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
            for epoch in self._epochs_for(code):
                self._catalog.validate(code, self._release, epoch, resolution)
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

        Args:
            products: The plan from `_search`.

        Returns:
            list[Path]: One written GeoTIFF (or table) path per product.
        """
        if self._api == "stac":
            return self._fetch_via_stac(products)
        session = requests.Session()
        written: list[Path] = []
        try:
            for rp in products:
                if rp.metadata.get("kind") == "tabular":
                    written.append(self._fetch_duc(rp))
                    continue
                tifs = [
                    download_and_unzip(url, self._raw_dir, session=session)
                    for url in rp.metadata["urls"]
                ]
                written.append(self._localise(tifs, rp))
        finally:
            session.close()
        return written

    def _localise(self, tifs: list[Path], rp: RemoteProduct) -> Path:
        """Mosaic + reproject + crop the downloaded tiles into one GeoTIFF.

        The pyramids-consuming core: `merge_rasters` mosaics the tiles **and**
        reprojects them to the output CRS in one call (skipped when the source
        already matches the output CRS), then `Dataset.crop` clips to the AOI
        bbox. Categorical products reproject with nearest-neighbour (so class
        codes are never blended) and carry their colour table.

        Args:
            tifs: The downloaded source `.tif` tiles (Mollweide or WGS84).
            rp: The `RemoteProduct` (its `metadata` carries `product`,
                `epoch`, `resolution`, `categorical`).

        Returns:
            pathlib.Path: The AOI-cropped GeoTIFF written under `self.path`.
        """
        from pyramids.dataset import Dataset
        from pyramids.dataset.merge import merge_rasters

        resolution = rp.metadata["resolution"]
        categorical = rp.metadata["categorical"]
        resampling = "nearest neighbor" if categorical else "bilinear"
        source_is_wgs84 = native_source_crs(resolution) == "4326"
        reproject = not (source_is_wgs84 and self._output_epsg == 4326)
        dst_crs = self._output_epsg if reproject else None

        self._raw_dir.mkdir(parents=True, exist_ok=True)
        merged = self._raw_dir / f"{rp.id}_merged.tif"
        merge_rasters(
            src=[str(t) for t in tifs],
            dst=str(merged),
            dst_crs=dst_crs,
            resampling=resampling,
        )

        dataset = Dataset.read_file(str(merged))
        cropped = dataset.crop(
            bbox=[self.space.west, self.space.south, self.space.east, self.space.north],
            epsg=4326,
            touch=False,
        )
        if categorical:
            cropped.color_table = self._catalog.get(
                rp.metadata["product"]
            ).color_table()

        target = Path(self.path) / (
            f"{rp.id}_{resolution}_epsg{self._output_epsg}.tif"
        )
        cropped.to_file(str(target))
        _close_dataset(dataset)
        _close_dataset(cropped)
        # Best-effort cleanup of the merge intermediate; on Windows the GDAL
        # handle can briefly outlive the Python object, so a locked file is
        # left in the cache rather than raising.
        try:
            merged.unlink(missing_ok=True)
        except OSError:
            pass
        return target

    def _fetch_via_stac(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch via the optional JRC STAC search path (implemented in `C9`)."""
        raise NotImplementedError(
            "GHSL api='stac' search path is implemented in C9; use api='direct'."
        )

    def _fetch_duc(self, rp: RemoteProduct) -> Path:
        """Download a tabular DUC / WUP-statistics product (implemented in `C7`)."""
        raise NotImplementedError(
            "GHSL tabular products (DUC / WUP statistics) are implemented in C7."
        )


def _close_dataset(dataset: object) -> None:
    """Release a pyramids `Dataset`'s underlying GDAL handle if it exposes one.

    pyramids `Dataset` objects may hold an open GDAL dataset; closing it lets
    the OS release the file lock (notably on Windows) before the intermediate
    is deleted. A no-op when the object has no `close`.

    Args:
        dataset: A pyramids `Dataset` (or anything with an optional `close`).
    """
    closer = getattr(dataset, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - best-effort handle release
            pass


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
