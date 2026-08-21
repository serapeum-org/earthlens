"""FABDEM backend — bare-earth DEM over the Bristol open HTTPS file tree.

`FABDEM` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`).
FABDEM V1-2 (Forest And Buildings removed Copernicus DEM; Hawker & Neal,
University of Bristol / Fathom) is the Copernicus GLO-30 DSM with forest canopy
and building heights removed — a ~30 m (1 arc-second) global bare-earth terrain
model, the recommended DEM for flood routing.

A request is a bbox (`lat_lim` / `lon_lim`) — FABDEM has a single `elevation`
band, so the backend is facet-only (it declares no `variables` axis). The DEM is
static, so `start` / `end` are accepted for facade parity and ignored, and the
facade-forwarded `aggregate=` is rejected (there is no temporal axis to reduce).
The backend maps
the bbox to the intersecting Bristol 10° bundle zip(s), downloads them over
anonymous HTTPS, extracts only the intersecting 1° Cloud-Optimized GeoTIFF
tiles, then uses `pyramids` to mosaic and crop to the AOI — writing one
GeoTIFF. Ocean-only bundles / cells are absent upstream and are skipped.

FABDEM is **CC-BY-NC-SA 4.0** (non-commercial), so `download()` emits a
`LicenseWarning`; commercial use needs a Fathom licence. The provider is public
and anonymous, so there is no auth module. The GIS work happens locally in
`pyramids`, so this is a genuine pyramids-consuming backend.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    aoi_tag,
    close_quietly,
    sidecar_is_fresh,
    write_sidecar,
)
from earthlens.base.spatial import crop_to_aoi
from earthlens.biodiversity import warn_license
from earthlens.fabdem._helpers import (
    bundle_url,
    bundles_for_bbox,
    download_bundle,
    extract_tiles,
)
from earthlens.fabdem.catalog import Catalog, Dataset


class FABDEM(AbstractDataSource):
    """FABDEM V1-2 bare-earth DEM backend (raster GeoTIFF output).

    Fetches the ~30 m global bare-earth DEM subset to the request bbox from the
    University of Bristol data repository over anonymous HTTPS, localised via
    pyramids. The request is a search/fetch split: `_search` resolves the
    intersecting 10° bundles and their 1° tiles, `_fetch` realises them
    (download → extract → mosaic → crop → GeoTIFF).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; the DEM yields a gridded GeoTIFF. The
            facade reads it to gate `aggregate=` (rejected — FABDEM is static).

    Examples:
        - A small AOI writes one cropped DEM GeoTIFF (marked `+SKIP` — it hits
          the live Bristol repository):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="fabdem",
            ...     lat_lim=[50.4, 50.6],
            ...     lon_lim=[0.4, 0.6],
            ...     path="fabdem_out",
            ... ).download()  # -> [Path('fabdem_out/fabdem_V1-2.tif')]

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "FABDEM is a single static bare-earth DEM with no temporal axis, so there is nothing to reduce. Call download() without aggregate="

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: FABDEM is time-invariant, so a missing `start` / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    #: Set by `download(force=...)`; bypasses the skip-if-exists check.
    _force: bool = False

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        catalog: Catalog | None = None,
    ):
        """Initialise a FABDEM backend instance.

        Args:
            start: Accepted for facade parity; ignored (FABDEM is static).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Required.
            temporal_resolution: Advisory label only (FABDEM is static).
            path: Output directory for the written GeoTIFF.
            fmt: Accepted for facade parity; unused.
            catalog: Optional pre-built `Catalog` (tests inject a faked one);
                defaults to the bundled catalog.

        Raises:
            ValueError: If the bounding box is missing.
        """
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "FABDEM requires a bounding box (lat_lim=[s, n], lon_lim=[w, e]) "
                "— a DEM subset has no default global extent."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset: Dataset = self._catalog.get("fabdem")

        super().__init__(
            start=start,
            end=end,
            variables=[self._dataset.band],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """No-op initialiser — FABDEM is public + anonymous (no client).

        Returns:
            None: The parent binds no `self.client`.
        """
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a degenerate (timeless) extent — FABDEM is static.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty date
                index (a static DEM has no time axis).
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    @property
    def _bbox(self) -> tuple[float, float, float, float]:
        """The AOI as `(west, south, east, north)` in degrees."""
        return (self.space.west, self.space.south, self.space.east, self.space.north)

    @property
    def _raw_dir(self) -> Path:
        """Directory the downloaded `.zip` / extracted `.tif` tiles are cached in."""
        return self.root_dir / ".fabdem_cache"

    @property
    def _target(self) -> Path:
        """The deterministic output GeoTIFF path for this request."""
        return Path(self.path) / f"fabdem_{self._dataset.version}.tif"

    def _is_cached(self, target: Path) -> bool:
        """Whether `target` already holds this exact AOI (AOI-aware skip).

        The output filename does not encode the AOI, so a bare exists-check
        would return a previous AOI's raster for a new bbox in the same `path`.
        The `<target>.aoi` sidecar records the AOI the file was written for
        (`earthlens.base.cache`); the skip only fires when it matches the current
        request and `force` is off.

        Args:
            target: The candidate output GeoTIFF path.

        Returns:
            bool: `True` when a matching cached output exists and may be reused.
        """
        return not self._force and sidecar_is_fresh(target, aoi_tag(self.space))

    def download(
        self,
        progress_bar: bool = True,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Fetch the FABDEM DEM subset as one AOI-cropped GeoTIFF.

        Emits a `LicenseWarning` (FABDEM is CC-BY-NC-SA 4.0, non-commercial)
        before any download.

        Args:
            progress_bar: Accepted for signature parity; one request per call.
            force: Re-fetch even when a complete output already exists,
                bypassing the skip-if-exists check. Defaults to `False`.

        Returns:
            list[Path]: The single written GeoTIFF path (`fabdem_<version>.tif`).

        Raises:
            ValueError: If the AOI intersects no published FABDEM land tile
                (an ocean-only area).
        """
        self._force = force
        products = self._search()
        result = self._fetch(products)
        # Warn about the non-commercial licence only once the data has actually
        # been fetched, so a request that resolves to nothing (e.g. an ocean-only
        # AOI, which raises in `_fetch`) does not print a spurious warning.
        warn_license(
            self._catalog.license_id,
            "fabdem",
            detail=(
                "FABDEM V1-2 is CC-BY-NC-SA 4.0: non-commercial use only, "
                f"and every product must cite {self._catalog.attribution} "
                f"{self._catalog.commercial_contact}"
            ),
        )
        return result

    def _search(self) -> list[RemoteProduct]:
        """Resolve the AOI to one `RemoteProduct` per intersecting 10° bundle.

        No network: each product carries its bundle `.zip` URL and the 1° tile
        member names to extract in `metadata`.

        Returns:
            list[RemoteProduct]: The download plan, one per bundle.

        Raises:
            ValueError: If the AOI intersects no FABDEM land cell. (An
                antimeridian-crossing `west > east` AOI is already rejected by
                `SpatialExtent` at construction.)
        """
        plan = bundles_for_bbox(self._bbox)
        if not plan:
            raise ValueError(
                f"no FABDEM land tiles intersect the AOI {self._bbox}; the area "
                "may be entirely ocean or outside the -90..90 / -180..180 grid."
            )
        return [
            RemoteProduct(
                id=bundle,
                metadata={"bundle": bundle, "tiles": tiles, "url": bundle_url(bundle)},
            )
            for bundle, tiles in plan.items()
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download the bundles, extract the tiles, mosaic + crop to one GeoTIFF.

        Idempotent: a complete output for this AOI is returned without
        re-downloading (unless `force`). Downloaded bundle `.zip`s are **kept**
        in the cache (`.fabdem_cache/`), so a later AOI in the same 10° block
        extracts the tiles it needs from the cached zip without re-fetching, and
        an ocean-only 1° cell absent from the archive simply stays absent.
        Ocean-only bundles (`404`) are skipped; an AOI that yields no tile at all
        raises rather than writing an empty raster.

        Args:
            products: The plan from `_search`.

        Returns:
            list[Path]: The single written GeoTIFF path.

        Raises:
            ValueError: If no tile could be fetched for the AOI.
        """
        target = self._target
        if self._is_cached(target):
            logger.info(
                f"FABDEM: {target.name} already holds this AOI; skipping download."
            )
            return [target]

        self._raw_dir.mkdir(parents=True, exist_ok=True)
        tifs: list[Path] = []
        for rp in products:
            bundle = rp.metadata["bundle"]
            tiles = rp.metadata["tiles"]
            # Only the tiles this AOI needs and does not already have are
            # extracted; the 0.8-2.4 GB bundle `.zip` is kept in the cache (not
            # deleted), so a later AOI in the same 10° block extracts the tiles
            # it needs from the cached zip without re-downloading — and an
            # ocean-only 1° cell absent from the archive simply stays absent
            # rather than forcing a re-fetch.
            missing = [n for n in tiles if not (self._raw_dir / n).exists()]
            if missing:
                zip_path = download_bundle(rp.metadata["url"], self._raw_dir)
                if zip_path is None:
                    logger.info(
                        f"FABDEM: bundle {bundle} is not published (ocean-only "
                        "block); skipping."
                    )
                    continue
                extract_tiles(zip_path, self._raw_dir, missing)
            tifs.extend(
                self._raw_dir / n for n in tiles if (self._raw_dir / n).exists()
            )

        if not tifs:
            raise ValueError(
                f"FABDEM: the AOI {self._bbox} intersects no published 1° tile "
                "(ocean-only area); nothing to write."
            )
        return [self._localise(sorted(tifs), target)]

    def _localise(self, tifs: list[Path], target: Path) -> Path:
        """Mosaic the 1° tiles and crop to the AOI, writing one GeoTIFF.

        The pyramids-consuming core: `merge_rasters` mosaics the intersecting 1°
        COGs (FABDEM is already EPSG:4326, so no reprojection), then
        `crop_to_aoi` clips to the AOI bbox — or to the exact polygon when the
        request carried an `aoi=` polygon. The source no-data value is stamped on
        the mosaic (`merge_rasters` otherwise defaults it to `0`, which would
        wrongly mark every genuine 0 m sea-level cell as missing).

        Args:
            tifs: The extracted 1° `.tif` tiles (WGS84).
            target: The destination GeoTIFF path.

        Returns:
            pathlib.Path: The AOI-cropped GeoTIFF at `target`.
        """
        from pyramids.dataset import Dataset as PyramidsDataset
        from pyramids.dataset.merge import merge_rasters

        self._raw_dir.mkdir(parents=True, exist_ok=True)
        merged = self._raw_dir / "fabdem_merged.tif"
        # The 1° tiles share FABDEM's grid and CRS, so the mosaic is a straight
        # composite with no reprojection; nearest-neighbour keeps the exact
        # source elevations and never blends real values with the -9999 no-data.
        merge_rasters(
            src=[str(t) for t in tifs],
            dst=str(merged),
            dst_crs=None,
            resampling="nearest neighbor",
            no_data_value=self._dataset.nodata,
        )

        dataset = PyramidsDataset.read_file(str(merged))
        cropped = crop_to_aoi(
            dataset,
            self.space,
            bbox=[self.space.west, self.space.south, self.space.east, self.space.north],
            touch=False,
        )
        # Write through a sibling and rename, so a crash mid-write cannot leave a
        # partial file the skip-if-exists check would accept. The real suffix is
        # kept — pyramids picks its driver from the extension.
        staged = target.with_name(f"{target.stem}.part{target.suffix}")
        try:
            cropped.to_file(str(staged))
            # pyramids keeps the written dataset's GDAL handle open, which holds a
            # Windows lock and blocks the rename; release it first.
            close_quietly(cropped)
            staged.replace(target)
        except BaseException:
            close_quietly(cropped)
            staged.unlink(missing_ok=True)
            raise
        finally:
            # Always drop the handle + the multi-GB merge intermediate, so a
            # failed write does not strand `fabdem_merged.tif` in the cache. On
            # Windows the GDAL handle can briefly outlive the Python object, so a
            # still-locked file is left rather than raising.
            close_quietly(dataset)
            try:
                merged.unlink(missing_ok=True)
            except OSError:
                pass
        write_sidecar(target, aoi_tag(self.space))
        return target
