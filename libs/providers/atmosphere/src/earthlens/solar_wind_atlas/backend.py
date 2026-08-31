"""Solar & Wind Atlas backend — `SolarWindAtlas(AbstractDataSource)`.

`SolarWindAtlas` is a bbox-subset raster backend (`OUTPUT_KIND="raster"`) over
the Global Solar Atlas and Global Wind Atlas climatology layers. A request is a
bbox plus a list of layer ids (`variables=["ghi", "wind_100m"]`); `download()`
writes one cropped GeoTIFF per layer under `root_dir` and returns their paths.

Each layer is fetched by the transport its catalog row declares (pinned in the
A1 gate):

* **Global Wind Atlas** layers (`transport="vsicurl"`) are range-accessible COGs
  read **windowed** over `/vsicurl/` — only the AOI's byte ranges transfer.
* **Global Solar Atlas** layers (`transport="download_zip"`) are
  DEFLATE-compressed ZIP archives with no random access, so the per-variable
  archive is downloaded once into a cache and the bbox window is read from the
  local member (the `ghsl` download-then-localise model). The first fetch is a
  multi-GB download, logged as a one-time warning.

Every layer is static long-term-average climatology (no time axis), so the
facade-forwarded `aggregate=` is rejected. All raster I/O goes through
`pyramids`; there is no auth module — both atlases are keyless / CC-BY-4.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.config import cache_dir as _shared_cache_dir
from earthlens.solar_wind_atlas._helpers import (
    bbox_from_extent,
    download_cache_crop,
    window_crop,
)
from earthlens.solar_wind_atlas.catalog import Catalog, Layer

#: Approximate one-time download size, per Global Solar Atlas variable, surfaced
#: in the `download_zip` heads-up warning (the 1 km single-file product).
_GSA_DOWNLOAD_NOTE = "~2.7 GB per variable (1 km)"


class SolarWindAtlas(AbstractDataSource):
    """Global Solar Atlas + Global Wind Atlas layers, bbox-subset to GeoTIFF.

    Resolves each requested layer id against the bundled catalog and fetches it
    by its declared transport — windowed `/vsicurl` for the wind COGs,
    download-once-and-cache for the solar ZIPs — writing one cropped GeoTIFF per
    layer. The request is a search/fetch split: :meth:`_search` names one product
    per layer and :meth:`_fetch` realises each (windowed read → GeoTIFF).

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every layer yields a gridded GeoTIFF.
            The facade reads it to gate `aggregate=` (rejected here — the layers
            are static climatology).

    Examples:
        - One wind + one solar layer write two GeoTIFFs (marked `+SKIP` — it
          hits the live atlases):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="solar-wind-atlas",
            ...     variables=["wind_100m", "ghi"],
            ...     lat_lim=[55.0, 55.5],
            ...     lon_lim=[12.0, 12.5],
            ...     path="atlas_out",
            ... ).download()  # -> [Path('atlas_out/wind_100m.tif'), Path('atlas_out/ghi.tif')]

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "the Solar/Wind Atlas layers are static long-term-average climatology with no temporal axis, so there is nothing to reduce. Call download() without aggregate="

    #: The resource-atlas layers are long-term climatologies, so a missing `start` /
    #: `end` is legal here.
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
        cache_dir: Path | str | None = None,
        timeout: float = 600.0,
        catalog: Catalog | None = None,
    ):
        """Initialise a Solar & Wind Atlas backend instance.

        Resolves every requested layer id against the catalog (did-you-mean on a
        miss) **before** the parent constructor runs.

        Args:
            start: Accepted for facade parity; ignored (the layers are static).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Required.
            variables: Layer ids to fetch (`["ghi", "wind_100m"]`). Required.
                List ids with `earthlens.solar_wind_atlas.Catalog().available()`.
            temporal_resolution: Advisory label only (the layers are static).
            path: Output directory for the written GeoTIFF(s).
            fmt: Accepted for facade parity; unused.
            cache_dir: Directory the Global Solar Atlas ZIP archives are cached
                in. Defaults to `solar_wind_atlas/` under the shared earthlens
                cache directory (`set_cache_dir()` / `EARTHLENS_CACHE`), not
                under `path`.
            timeout: Per-request HTTP timeout (seconds) for a solar ZIP download.
            catalog: Optional pre-built `Catalog` (tests inject a faked one);
                defaults to the bundled catalog.

        Raises:
            ValueError: If `variables` is empty / unknown (did-you-mean
                surfaced) or the bounding box is missing.
            TypeError: If `variables` is a mapping (layers are named by id, not
                a per-dataset map).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "SolarWindAtlas `variables` must be a list of layer ids (e.g. "
                "['ghi', 'wind_100m']), not a mapping."
            )
        if not variables:
            raise ValueError(
                "SolarWindAtlas requires variables=[<layer id>, ...] naming "
                "curated layers (e.g. variables=['ghi', 'wind_100m']). List ids "
                "with earthlens.solar_wind_atlas.Catalog().available()."
            )
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "SolarWindAtlas requires a bounding box (lat_lim=[s, n], "
                "lon_lim=[w, e]) — a layer subset has no default global extent."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._layers: list[Layer] = [self._catalog.get(v) for v in variables]
        self._timeout = timeout
        self._cache_dir_arg = cache_dir

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
        """Return a degenerate (timeless) extent — the layers are static.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty date
                index (a static climatology layer has no time axis).
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    @property
    def cache_dir(self) -> Path:
        """Directory the Global Solar Atlas ZIP archives are cached in.

        Returns:
            Path: The `cache_dir=` argument, or `solar_wind_atlas/` under the
                shared earthlens cache directory (`set_cache_dir()` /
                `EARTHLENS_CACHE`) by default.
        """
        if self._cache_dir_arg is not None:
            return Path(self._cache_dir_arg)
        return _shared_cache_dir() / "solar_wind_atlas"

    def _search(self) -> list[RemoteProduct]:
        """Name one product per requested layer (metadata = the `Layer` row).

        Returns:
            list[RemoteProduct]: One product per requested layer, carrying its
                resolved :class:`Layer` row in `metadata`.
        """
        return [
            RemoteProduct(id=layer.id, href=layer.url, metadata={"layer": layer})
            for layer in self._layers
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch every product by its transport, writing one GeoTIFF each.

        Args:
            products: The list from :meth:`_search`.

        Returns:
            list[Path]: The written GeoTIFF path(s), in `products` order.
        """
        return [self._fetch_one_layer(product) for product in products]

    def _fetch_one_layer(self, product: RemoteProduct) -> Path:
        """Fetch one layer's bbox window as a GeoTIFF by its declared transport.

        Wind layers (`transport="vsicurl"`) are read windowed from the remote
        COG; solar layers (`transport="download_zip"`) download the ZIP once into
        the cache and read the window from the local member.

        Args:
            product: The `RemoteProduct` whose `metadata["layer"]` is the
                resolved catalog row.

        Returns:
            Path: The written GeoTIFF at `<root_dir>/<layer id>.tif`.
        """
        layer: Layer = product.metadata["layer"]
        bbox = bbox_from_extent(self.space)
        out_path = self.root_dir / f"{layer.id}.tif"
        if layer.transport == "vsicurl":
            logger.info(
                f"solar_wind_atlas {layer.id}: windowed /vsicurl read {layer.url}"
            )
            return window_crop(layer.url, bbox, out_path)
        logger.info(f"solar_wind_atlas {layer.id}: download + crop {layer.url}")
        return download_cache_crop(
            layer.url, bbox, out_path, self.cache_dir, timeout=self._timeout
        )

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch every requested layer's bbox subset as a written GeoTIFF.

        Args:
            progress_bar: Accepted for signature parity; one fetch per layer.

        Returns:
            list[Path]: The written GeoTIFF path(s), one per requested layer.
        """
        self._warn_large_downloads()
        paths = cast("list[Path]", self._api())
        self._log_attribution()
        return paths

    def _warn_large_downloads(self) -> None:
        """Warn once when any requested layer triggers a multi-GB ZIP download."""
        if any(layer.transport == "download_zip" for layer in self._layers):
            logger.warning(
                "solar_wind_atlas: a Global Solar Atlas layer downloads its full "
                f"global archive once ({_GSA_DOWNLOAD_NOTE}) into {self.cache_dir} "
                "before cropping; subsequent requests reuse the cache."
            )

    def _log_attribution(self) -> None:
        """Log the CC-BY-4.0 attribution once per atlas actually fetched."""
        seen: set[str] = set()
        for layer in self._layers:
            if layer.atlas in seen:
                continue
            seen.add(layer.atlas)
            logger.info(f"solar_wind_atlas attribution: {layer.license_note}")
