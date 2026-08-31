"""SoilGrids backend — `SoilGrids(AbstractDataSource)` over OGC WCS.

`SoilGrids` is a bbox-subset raster backend (`OUTPUT_KIND="raster"`) over the
ISRIC SoilGrids 2.0 global 250 m soil-property maps. A request is a bbox plus a
list of property ids (`variables=["clay", "phh2o"]`) and optional `depths=` /
`quantiles=`; `download()` expands it into `(property, depth, quantile)`
coverage triples, fetches each one server-side over WCS, and writes one GeoTIFF
per triple under `root_dir`, returning their paths.

The WCS transport lives in `pyramids` (`Dataset.from_wcs`): SoilGrids publishes
each property as an independent MapServer WCS service whose native grid is a
custom Interrupted Goode Homolosine (`EPSG:152160`) that PROJ cannot resolve, so
the reader is handed `IGH_PROJ4` as its `coverage_crs` shim and reprojects the
result to `output_crs` (default `EPSG:4326`, pinned in the `A1` gate). Every
layer is a single static prediction with no time axis, so the facade-forwarded
`aggregate=` is rejected. There is no auth module — SoilGrids is open, CC-BY 4.0;
this backend imports no OGC-WCS SDK and no array library directly (the WCS
transport and GeoTIFF I/O are pyramids' job, via `Dataset.from_wcs`).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    close_quietly,
)
from earthlens.base.spatial import mask_to_geometry
from earthlens.soilgrids._helpers import (
    IGH_PROJ4,
    SOILGRIDS_ATTRIBUTION,
    bbox_from_extent,
    coverage_id,
    expand_request,
)
from earthlens.soilgrids.catalog import Catalog, Property


class SoilGrids(AbstractDataSource):
    """ISRIC SoilGrids 2.0 soil properties, bbox-subset to GeoTIFF over WCS.

    Resolves each requested property id against the bundled catalog, expands the
    request into `(property, depth, quantile)` coverage triples, and fetches each
    one through `pyramids.dataset.Dataset.from_wcs` — writing one cropped GeoTIFF
    per triple. The request is a search/fetch split: `_search` names one product
    per triple and `_fetch_one` realises each (WCS subset → GeoTIFF).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every coverage yields a gridded GeoTIFF.
            The facade forwards `aggregate=` to raster backends, so `download`
            rejects it here (SoilGrids is a static soil-property map with no
            temporal axis).

    Examples:
        - Two properties at their default depths / the `mean` layer write one
          GeoTIFF per `(property, depth, quantile)` (marked `+SKIP` — it hits
          the live SoilGrids WCS):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="soilgrids",
            ...     variables=["clay", "phh2o"],
            ...     lat_lim=[51.0, 52.0],
            ...     lon_lim=[5.0, 6.0],
            ...     path="soil_out",
            ... ).download()  # -> 12 GeoTIFFs (2 properties x 6 depths x mean)

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "SoilGrids is a static soil-property map with no temporal axis, so there is nothing to reduce. Call download() without aggregate="

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: Partial-failure policy for the per-coverage loop; `download(errors=...)`
    #: overrides it per call.
    _errors: str = "warn"

    #: The soil property grids are time-invariant, so a missing `start` / `end` is legal
    #: here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        variables: list[str] | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        depths: list[str] | None = None,
        quantiles: list[str] | None = None,
        output_crs: str | None = "EPSG:4326",
        resolution: float | tuple[float, float] | None = None,
        coverage_crs: str = IGH_PROJ4,
        timeout: float = 120.0,
        catalog: Catalog | None = None,
    ):
        """Initialise a SoilGrids backend instance.

        Resolves every requested property id against the catalog (did-you-mean
        on a miss) **before** the parent constructor runs.

        Args:
            start: Accepted for facade parity; ignored (SoilGrids is static).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Required.
            variables: SoilGrids property ids (`["clay", "phh2o"]`). Required.
                List ids with `earthlens.soilgrids.Catalog().parameters()`.
            temporal_resolution: Advisory label only (the maps are static).
            path: Output directory for the written GeoTIFF(s).
            fmt: Accepted for facade parity; unused.
            depths: Depth intervals to fetch (`["0-5cm", "5-15cm"]`), or `None`
                for every depth each property publishes (all six standard
                depths, or the single `0-30cm` for `ocs`).
            quantiles: Quantile / layer tokens to fetch (`["mean", "Q0.05"]`),
                or `None` for the `mean` layer only.
            output_crs: CRS to reproject the coverage into. Defaults to
                `"EPSG:4326"` (lon/lat output); pass `None` to keep the native
                Interrupted Goode Homolosine grid.
            resolution: Output pixel size in `output_crs` units. `None`
                (default) keeps the native 250 m resolution (≈0.0025° in
                EPSG:4326).
            coverage_crs: The coverage's real CRS, handed to the WCS reader so
                the SoilGrids IGH grid resolves. Defaults to `IGH_PROJ4`;
                override only if ISRIC changes its native projection.
            timeout: Per-request HTTP timeout (seconds) for the WCS calls.
            catalog: A pre-built `Catalog` to use instead of loading the bundled
                one; defaults to the bundled catalog.

        Raises:
            ValueError: If `variables` is empty / unknown (did-you-mean
                surfaced) / has duplicate ids, the bounding box is missing, or
                `depths` / `quantiles` is an explicitly-empty list (which would
                select no coverages).
            TypeError: If `variables` is a mapping (properties are named by id,
                not a per-dataset map).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "SoilGrids `variables` must be a list of property ids (e.g. "
                "['clay', 'phh2o']), not a mapping."
            )
        if not variables:
            raise ValueError(
                "SoilGrids requires variables=[<property id>, ...] naming "
                "curated properties (e.g. variables=['clay', 'phh2o']). List "
                "ids with earthlens.soilgrids.Catalog().parameters()."
            )
        duplicates = sorted(v for v, n in Counter(variables).items() if n > 1)
        if duplicates:
            raise ValueError(
                f"SoilGrids variables has duplicate property ids {duplicates}; "
                "list each property once."
            )
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "SoilGrids requires a bounding box (lat_lim=[s, n], "
                "lon_lim=[w, e]) — a property subset has no default global "
                "extent."
            )
        if depths is not None and not depths:
            raise ValueError(
                "SoilGrids depths=[] selects no coverages; pass depths=None for "
                "every depth, or a non-empty list of depth intervals."
            )
        if quantiles is not None and not quantiles:
            raise ValueError(
                "SoilGrids quantiles=[] selects no coverages; pass quantiles=None "
                "for the 'mean' layer, or a non-empty list of quantile tokens."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        # Resolve every property up front so an unknown id fails at construction
        # (did-you-mean), before any network call.
        self._properties: list[Property] = [self._catalog.get(v) for v in variables]
        self._depths_arg = depths
        self._quantiles_arg = quantiles
        self._output_crs = output_crs
        self._resolution = resolution
        self._coverage_crs = coverage_crs
        self._timeout = timeout
        self._show_progress = True
        #: Scratch dir for the in-flight batch; `_api` owns its lifetime.
        self._tmp_dir: Path | None = None

        super().__init__(
            start=start,
            end=end,
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a degenerate (timeless) extent — the maps are static.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty date
                index (a static soil-property map has no time axis).
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    def _api(self) -> list[Path]:
        """Fetch each coverage under a progress bar, isolating per-coverage faults.

        A single SoilGrids request fans out to many coverages against one public
        MapServer WCS, so — unlike the shared `_search_fetch_each` all-or-nothing
        composition — this isolates each `_fetch_one`: a coverage whose WCS fetch
        fails is logged and skipped so the rest still land and are returned, and
        only an empty result set (every coverage failed) raises.

        Returns:
            list[Path]: The GeoTIFFs that were written, in plan order (a subset
                when some coverages failed).

        Raises:
            RuntimeError: If every requested coverage failed to fetch.
        """
        products = self._search()
        if not products:
            return []
        from tqdm import tqdm

        # One unique scratch dir per download, cleaned up at the end: each
        # coverage is written here then atomically renamed onto its final path.
        # A unique dir (not a fixed `.soilgrids-tmp`) keeps concurrent downloads
        # to the same output dir from colliding and never trips over a
        # pre-existing file of that name.
        self.root_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=self.root_dir, prefix=".soilgrids-tmp-"))
        # Hand the scratch dir to `_fetch_one` through the instance rather than
        # as an extra parameter, so `_fetch_one(product)` keeps the base hook's
        # one-argument shape. `_api` owns its lifetime either way.
        self._tmp_dir = tmp_dir
        written: list[Path] = []
        try:
            fetched, failures = self._run_items(
                tqdm(
                    products,
                    disable=not self._show_progress,
                    desc="soilgrids",
                    unit="coverage",
                ),
                self._fetch_one,
                errors=self._errors,
                label="coverage",
                describe=lambda product: str(product.id),
            )
            written.extend(fetched)
            failed = [described for described, _exc in failures]
        finally:
            self._tmp_dir = None
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if failed and not written:
            raise RuntimeError(
                f"soilgrids: all {len(failed)} requested coverage(s) failed to "
                f"fetch: {failed}."
            )
        if failed:
            logger.warning(
                f"soilgrids: {len(failed)} of {len(products)} coverage(s) failed "
                f"and were skipped: {failed}."
            )
        return written

    def _search(self) -> list[RemoteProduct]:
        """Expand the request into one `RemoteProduct` per coverage triple.

        No network: each product carries its `(property, depth, quantile)` and
        the resolved property row in `metadata`, so a dry-run inspection is
        cheap.

        Returns:
            list[RemoteProduct]: The download plan, one item per
                `(property, depth, quantile)` coverage to fetch.
        """
        rows_by_id = {row.id: row for row in self._properties}
        triples = expand_request(
            list(rows_by_id),
            self._depths_arg,
            self._quantiles_arg,
            self._catalog,
        )
        plan: list[RemoteProduct] = []
        for property_id, depth, quantile in triples:
            row = rows_by_id[property_id]
            plan.append(
                RemoteProduct(
                    id=coverage_id(property_id, depth, quantile),
                    href=row.endpoint,
                    metadata={
                        "property": property_id,
                        "depth": depth,
                        "quantile": quantile,
                        "row": row,
                    },
                )
            )
        return plan

    def _fetch_one(self, product: RemoteProduct) -> Path:
        """Fetch one coverage's bbox window as a GeoTIFF over WCS.

        Uses `pyramids.dataset.Dataset.from_wcs` — GDAL's WCS driver inside
        pyramids does the transport, handed `coverage_crs` so SoilGrids' custom
        IGH grid resolves and `output_crs` for the lon/lat reprojection. The WCS
        subset is rectangular; when the request carried a polygon `aoi=` (stored
        on `self.space.geometry`), the fetched coverage is masked to that exact
        shape before writing, matching the peer raster backends. The
        scaled-integer unit metadata is logged (never applied — the pixels stay
        as stored integers, `G2`).

        The coverage is fetched in-memory (`from_wcs(output=None)`), written into
        `self._tmp_dir` (the per-download scratch dir `_api` owns), then atomically
        renamed onto its final path only after a fully successful write. Because
        the final path is touched only by that rename, any failure leaves a
        pre-existing GeoTIFF from a prior run untouched and never leaves a partial
        behind — only the temp file is cleaned up.

        Args:
            product: The `RemoteProduct` whose `metadata` carries the resolved
                property row + the `(depth, quantile)` cell. The scratch
                directory the write is staged in before the atomic rename is
                read from `self._tmp_dir`, which :meth:`_api` creates and
                removes around the whole batch.

        Returns:
            Path: The written GeoTIFF at
                `<root_dir>/<property>_<depth>_<quantile>.tif`.
        """
        from pyramids.dataset import Dataset

        row: Property = product.metadata["row"]
        out_path = self.root_dir / f"{product.id}.tif"
        logger.info(
            f"soilgrids {product.id}: WCS subset {row.endpoint} "
            f"(values are scaled integers in {row.mapped_units or 'native units'}"
            f"; divide by {row.scale_factor:g} for {row.unit or 'the unit'})"
        )
        has_mask = getattr(self.space, "geometry", None) is not None
        # The temp keeps the `.tif` suffix — pyramids picks the GDAL driver from
        # the extension, so a driver-less suffix like `.part` would raise
        # DriverNotExistError.
        tmp_dir = self._tmp_dir
        if tmp_dir is None:
            # A real check, not an `assert`: asserts vanish under `python -O`,
            # and the next line would then fail as an obscure
            # `TypeError: unsupported operand type(s) for /: NoneType and str`.
            raise RuntimeError(
                "SoilGrids._fetch_one was called outside a download: the "
                "per-batch scratch directory is only set up by _api()."
            )
        tmp_path = tmp_dir / out_path.name
        dataset = None
        result = None
        try:
            dataset = Dataset.from_wcs(
                row.endpoint,
                coverage=product.id,
                bbox=bbox_from_extent(self.space),
                crs="EPSG:4326",
                coverage_crs=self._coverage_crs,
                output_crs=self._output_crs,
                resolution=self._resolution,
                output=None,
                timeout=self._timeout,
            )
            result = mask_to_geometry(dataset, self.space) if has_mask else dataset
            result.to_file(str(tmp_path))
            # Release the GDAL write handles before the rename (Windows file
            # lock), then promote the temp atomically. `_close_dataset` is
            # best-effort and never raises, so it is safe here inside the try;
            # keeping `os.replace` in the try means a rename failure is cleaned
            # up rather than orphaning a fully-written temp in the output tree.
            if result is not dataset:
                close_quietly(result)
            close_quietly(dataset)
            os.replace(tmp_path, out_path)
        except Exception:
            if result is not None and result is not dataset:
                close_quietly(result)
            close_quietly(dataset)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return out_path

    def download(
        self,
        progress_bar: bool = True,
        errors: str = "warn",
    ) -> list[Path]:
        """Fetch every requested coverage's bbox subset as a written GeoTIFF.

        Args:
            progress_bar: Show a per-coverage `tqdm` bar (a request can expand to
                many coverages). Defaults to `True`.

        Returns:
            list[Path]: The written GeoTIFF path(s), one per
                `(property, depth, quantile)` coverage requested.
        """
        self._show_progress = progress_bar
        self._errors = self.check_errors_policy(errors)
        paths = self._api()
        logger.info(f"soilgrids attribution: {SOILGRIDS_ATTRIBUTION}")
        return paths
