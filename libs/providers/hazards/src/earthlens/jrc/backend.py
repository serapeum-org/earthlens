"""JRC European flood-hazard backend — `JRC(AbstractDataSource)`.

`JRC` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`)
for the JRC European Flood Hazard Map (EFHM): "River flood hazard maps for
Europe and the Mediterranean Basin". Each cell value is river-flood water depth
(m) for a chosen return period.

A request is a bbox (`lat_lim` / `lon_lim`) plus one or more `return_periods`.
The product is static, so `start` / `end` are accepted for facade parity and
ignored, and the facade-forwarded `aggregate=` is rejected (return periods are
not a reducible time axis). Each return period is one whole-Europe EPSG:4326
GeoTIFF of ~23 GB uncompressed, so the backend never reads it whole: it opens
the file lazily over GDAL's `/vsicurl` (HTTP range requests), reads **only** the
AOI's pixel window through `pyramids`, and writes one cropped GeoTIFF per return
period. An AOI outside the Europe / Mediterranean coverage raises a clear
`ValueError` rather than writing an empty raster.

The product is public and CC-BY-4.0 (permissive), so there is no auth module and
no `LicenseWarning`. The raster read happens through `pyramids` (a windowed
`read_array`), so this is a genuine pyramids-consuming backend — no `xarray`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    aoi_tag,
    sidecar_is_fresh,
    write_sidecar,
)
from earthlens.base.spatial import (
    bbox_overlaps,
    crop_to_aoi,
    ensure_no_data,
    vsicurl_config,
    widen_degenerate_bbox,
    windowed_bbox_crop,
)
from earthlens.jrc._helpers import efhm_url
from earthlens.jrc.catalog import Catalog, Dataset


class JRC(AbstractDataSource):
    """JRC European Flood Hazard Map backend (raster GeoTIFF output).

    Fetches the EFHM water-depth grid for one or more return periods, cropped to
    the request bbox, via lazy `/vsicurl` windowed reads. The request is a
    search/fetch split: `_search` names one product per return period, `_fetch`
    realises each (windowed read → crop → GeoTIFF).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; each return period yields a gridded
            GeoTIFF. The facade reads it to gate `aggregate=` (rejected — the
            return periods are not a temporal axis).

    Examples:
        - A small AOI writes one cropped GeoTIFF per return period (marked
          `+SKIP` — it hits the live JRC directory):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="jrc-flood",
            ...     lat_lim=[51.8, 52.0],
            ...     lon_lim=[4.8, 5.0],
            ...     return_periods=[100],
            ...     path="efhm_out",
            ... ).download()  # -> [Path('efhm_out/efhm_RP100.tif')]

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Maps a catalog row's `kind` to the instance `OUTPUT_KIND`, set per instance
    #: in `__init__` so a future non-raster JRC dataset can return another shape
    #: (the `RiskIndicators` / `NSI` / `ecmwf`-endpoint pattern). EFHM is raster.
    _KIND_TO_OUTPUT: dict[str, OutputKind] = {"flood_hazard_raster": "raster"}

    AGGREGATE_REFUSAL_REASON = "the JRC flood hazard map is a set of static per-return-period depth grids with no temporal axis, so there is nothing to reduce. Call download() without aggregate="

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: The EFHM is time-invariant, so a missing `start` / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        return_periods: list[int | str] | int | str | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        catalog: Catalog | None = None,
    ):
        """Initialise a JRC-flood backend instance.

        The EFHM has a single `water_depth` band, so the backend is facet-only
        (it declares no `variables` axis); the request axis is `return_periods`.

        Args:
            start: Accepted for facade parity; ignored (the EFHM is static).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Required.
            return_periods: One return period, or a list, in years — as ints
                (`100`) or strings (`"100"` / `"RP100"`). Defaults to `[100]`.
                Every value must be a published return period.
            temporal_resolution: Advisory label only (the EFHM is static).
            path: Output directory for the written GeoTIFF(s).
            fmt: Accepted for facade parity; unused.
            catalog: Optional pre-built `Catalog` (tests inject a faked one);
                defaults to the bundled catalog.

        Raises:
            ValueError: If the bounding box is missing or a requested return
                period is not published.
        """
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "JRC requires a bounding box (lat_lim=[s, n], "
                "lon_lim=[w, e]) — a hazard-map subset has no default extent."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset: Dataset = self._catalog.get("efhm")
        self.OUTPUT_KIND = self._KIND_TO_OUTPUT.get(self._dataset.kind, "raster")
        self._return_periods = self._resolve_return_periods(return_periods)

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

    def _resolve_return_periods(
        self, return_periods: list[int | str] | int | str | None
    ) -> list[int]:
        """Normalise + validate the requested return periods against the catalog.

        Accepts a single value or a list; each may be an int (`100`) or a string
        (`"100"` / `"RP100"`, case-insensitive). Defaults to `[100]`.

        Args:
            return_periods: The raw request value.

        Returns:
            list[int]: Sorted, de-duplicated return periods to fetch.

        Raises:
            ValueError: If a value is unparseable or is not a published return
                period.
        """
        available = self._dataset.return_periods
        if return_periods is None:
            requested_raw: list[int | str] = [100]
        elif isinstance(return_periods, (list, tuple)):
            requested_raw = list(return_periods)
        else:
            requested_raw = [return_periods]

        resolved: list[int] = []
        for value in requested_raw:
            resolved.append(self._parse_rp(value))
        unknown = [rp for rp in resolved if rp not in available]
        if unknown:
            raise ValueError(
                f"return period(s) {unknown} are not published for the EFHM; "
                f"available: {available}."
            )
        return sorted(set(resolved))

    @staticmethod
    def _parse_rp(value: int | str) -> int:
        """Parse one return-period token to an int (`100`, `"100"`, `"RP100"`).

        Args:
            value: The raw return-period value.

        Returns:
            int: The integer return period in years.

        Raises:
            ValueError: If `value` is not an int or an `RP`-prefixed / bare
                integer string.
        """
        if isinstance(value, int):
            return value
        text = str(value).strip().upper()
        if text.startswith("RP"):
            text = text[2:]
        try:
            return int(text)
        except ValueError:
            raise ValueError(
                f"could not parse return period {value!r} (expected e.g. 100, "
                "'100', or 'RP100')."
            ) from None

    def _initialize(self):
        """No-op initialiser — the EFHM is public + anonymous (no client).

        Returns:
            None: The parent binds no `self.client`.
        """
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a degenerate (timeless) extent — the EFHM is static.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty date
                index (a static hazard map has no time axis).
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    @property
    def _bbox(self) -> tuple[float, float, float, float]:
        """The AOI as `(west, south, east, north)` in degrees."""
        return (self.space.west, self.space.south, self.space.east, self.space.north)

    def _bbox_overlaps(self, source: Any) -> bool:
        """Whether the AOI overlaps the source raster's geographic extent.

        Delegates to `earthlens.base.spatial.bbox_overlaps` so an AOI outside the
        EFHM's Europe / Mediterranean coverage is reported with a clear error
        before the windowed crop, rather than surfacing as an empty or opaque
        crop result.

        Args:
            source: An opened `pyramids.Dataset` exposing `geotransform`,
                `columns`, and `rows`.

        Returns:
            bool: `True` when the AOI bbox intersects the raster's extent.
        """
        return bbox_overlaps(source, self._bbox)

    def _is_cached(self, target: Path) -> bool:
        """Whether `target` already holds this exact AOI (AOI-aware skip).

        The output filename encodes the return period but not the AOI, so a bare
        exists-check would return a previous AOI's raster for a new bbox in the
        same `path`. The `<target>.aoi` sidecar records the AOI the file was
        written for (`earthlens.base.cache`); the skip only fires when it matches
        and `force` is off.

        Args:
            target: The candidate output GeoTIFF path.

        Returns:
            bool: `True` when a matching cached output exists and may be reused.
        """
        return not getattr(self, "_force", False) and sidecar_is_fresh(
            target, aoi_tag(self.space)
        )

    def download(
        self,
        progress_bar: bool = True,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Fetch the EFHM subset(s) as one AOI-cropped GeoTIFF per return period.

        Args:
            progress_bar: Accepted for signature parity; one read per period.
            force: Re-fetch even when a complete output already exists.

        Returns:
            list[Path]: The written GeoTIFF path(s), one per return period.

        Raises:
            ValueError: If the AOI is outside the EFHM's Europe / Mediterranean
                coverage. (An antimeridian-crossing `west > east` AOI is already
                rejected by `SpatialExtent` at construction.)
        """
        self._force = force
        products = self._search()
        return self._fetch(products)

    def _search(self) -> list[RemoteProduct]:
        """Resolve the request to one `RemoteProduct` per return period.

        No network: each product carries its return period and EFHM URL.

        Returns:
            list[RemoteProduct]: The download plan, one per return period.
        """
        return [
            RemoteProduct(
                id=f"efhm_RP{rp}",
                metadata={
                    "rp": rp,
                    "url": efhm_url(
                        rp,
                        base_url=self._dataset.base_url,
                        template=self._dataset.filename_template,
                    ),
                },
            )
            for rp in self._return_periods
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Windowed-read + crop each return period to one GeoTIFF.

        Args:
            products: The plan from `_search`.

        Returns:
            list[Path]: The written GeoTIFF path(s).

        Raises:
            ValueError: If the AOI is outside the EFHM coverage for a period.
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct) -> Path:
        """Read the AOI window of one return-period GeoTIFF and write the crop.

        Opens the whole-Europe GeoTIFF lazily and windowed-crops it to the AOI
        with `pyramids.Dataset.crop(bbox=)`, whose fast path reads **only** the
        AOI's pixel window over `/vsicurl` (HTTP range requests, tuned via
        `vsicurl_config()` — readdir-suppression + retry/timeout) for an
        axis-aligned box in the source CRS, carrying the source grid, CRS and
        no-data through (with the
        catalog no-data stamped when the source declares none). A point AOI is
        widened to one pixel so the strict fast path still fires. `crop_to_aoi`
        then trims the all-touched window to the exact bbox — or to the exact
        polygon when the request carried an `aoi=` polygon. An in-coverage AOI
        that is entirely no-data (e.g. open sea) still writes an all-no-data
        raster rather than raising.

        Args:
            product: The `RemoteProduct` whose `metadata` carries `rp` + `url`.

        Returns:
            pathlib.Path: The written GeoTIFF at `<path>/efhm_RP{rp}.tif`.

        Raises:
            ValueError: If the AOI does not overlap the EFHM coverage (an
                in-coverage AOI is written even when it holds no valid data).
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        from earthlens.base import close_quietly

        rp = product.metadata["rp"]
        url = product.metadata["url"]
        target = Path(self.path) / f"{product.id}.tif"
        if self._is_cached(target):
            logger.info(f"JRC: {target.name} already holds this AOI; skipping.")
            return target

        # Tune the /vsicurl read (readdir-suppression + retry/timeout) for the
        # duration of the open + windowed crop; a plain read_file installs none.
        with vsicurl_config():
            source = PyramidsDataset.read_file(url)
            try:
                if not self._bbox_overlaps(source):
                    raise ValueError(
                        f"the AOI {self._bbox} is outside the EFHM's Europe / "
                        f"Mediterranean coverage; no RP{rp} data to write."
                    )
                logger.info(
                    f"JRC RP{rp}: windowed /vsicurl crop of {self._bbox} "
                    f"from {url}"
                )
                # A point / cell-edge AOI (min == max on an axis) is widened to
                # one source pixel so crop(bbox=)'s fast path yields a 1x1 window
                # rather than raising on the zero-width box.
                geo = source.geotransform
                bbox = widen_degenerate_bbox(self._bbox, geo[1], geo[5])
                # The windowed fast path reads only the AOI pixel window from the
                # ~23 GB source; nodata / CRS / grid are carried onto the crop. An
                # in-coverage but all-no-data AOI keeps an all-no-data window
                # rather than raising.
                windowed = windowed_bbox_crop(source, bbox, epsg=4326)
            finally:
                close_quietly(source)

        try:
            # crop carries the source's own no-data through; when the source
            # declares none, fall back to the catalog value so the output stays
            # flagged and a polygon `aoi=` can trim exactly (matching the pre-crop
            # behaviour).
            windowed = ensure_no_data(windowed, self._dataset.nodata)
            # crop(bbox=) keeps every pixel the box overlaps (all-touched, up to
            # one extra pixel per edge); trim to the exact bbox (matching FABDEM)
            # — or to the exact polygon when the request carried an `aoi=` polygon.
            cropped = crop_to_aoi(
                windowed,
                self.space,
                bbox=[
                    self.space.west,
                    self.space.south,
                    self.space.east,
                    self.space.north,
                ],
                touch=False,
            )
            staged = target.with_name(f"{target.stem}.part{target.suffix}")
            try:
                cropped.to_file(str(staged))
                close_quietly(cropped)
                staged.replace(target)
            except BaseException:
                close_quietly(cropped)
                staged.unlink(missing_ok=True)
                raise
        finally:
            close_quietly(windowed)
        write_sidecar(target, aoi_tag(self.space))
        return target
