"""An engine on Earth Engine's documented pixel-extraction API.

`EeEngine` wraps `ee.Image.getDownloadURL`, the thumbnail-era path: capped at
32768 px per axis, delivered as a zipped GeoTIFF over HTTP. `auto_split` exists
only to work around that cap.

Earth Engine documents two purpose-built methods instead. `ee.data.computePixels`
extracts pixels from a *computed* image — a composite — passed through its
`expression` argument, and `ee.data.getPixels` extracts from an existing asset
with no computation, which is the official equivalent of what the EEDAI driver
reaches through GDAL. The guide presents `Export` as the default and these as the
option when exports hit scaling problems, noting they "add complexity, but can
scale to larger workloads", and points at the high-volume endpoint
(`https://earthengine-highvolume.googleapis.com`) for parallel requests.

So this engine is the first that is both **compute-capable** and **not cap-bound**
— unlike `EedaiEngine`, it does not decline a `cloud_mask` or `filters`, because
`computePixels` evaluates the graph server-side.

It exists in Epic 1 rather than later for a specific reason: the `Engine` ABC's
value was argued from a third engine that no task built, which left the central
claim of the epic untested. Registering this one is the proof — and it required
no edit to `_use_eedai` or `_api`, which is the acceptance criterion.

**The high-volume endpoint is per-engine, not global.** Its documented trade-off
is real: less caching, and complex queries cost more EECU-time there than on the
regular endpoint. So it is opted into through `EngineConfig.high_volume` rather
than switched on for everything.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, ClassVar

from loguru import logger

from earthlens.gee.engines import ENGINES, Engine
from earthlens.gee.grid import OutputGrid
from earthlens.gee.plans import EnginePlan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.backend import GEE
    from earthlens.gee.catalog import Dataset

__all__ = ["PixelsEngine", "HIGH_VOLUME_ENDPOINT"]

#: The host Earth Engine documents for parallel programmatic requests.
HIGH_VOLUME_ENDPOINT: str = "https://earthengine-highvolume.googleapis.com"


class PixelsEngine(Engine):
    """Fetch pixels through `ee.data.computePixels` / `ee.data.getPixels`.

    Serves a computed image, so unlike the EEDAI reader it accepts a
    `cloud_mask` and `filters`. It sizes its request from an
    :class:`~earthlens.gee.grid.OutputGrid` rather than deriving a grid of its
    own, so it cannot disagree with the other engines about the output shape.
    """

    name: ClassVar[str] = "pixels"

    def plan(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Decide whether to fetch this request through the pixel API.

        Declines only what the API genuinely cannot express — a request with no
        resolvable scale, or an AOI the output CRS cannot represent. It does
        **not** decline on `cloud_mask` or `filters`, which is the capability gap
        it closes relative to the EEDAI reader.

        Args:
            backend: The `GEE` instance, for its config and the grid.
            var_info: The catalog entry being read.
            band_count: How many bands the read requests.
            bucket_start: The bucket's inclusive start, for a collection read.
            bucket_end: The bucket's exclusive end, for a collection read.

        Returns:
            `(True, plan)` when it will serve the request, else `(False, None)`.
        """
        scale = backend.scale or var_info.spatial_resolution
        if not scale:
            logger.info(
                f"{var_info.id} has no resolvable scale, so the pixel API cannot "
                "size a request for it; falling back."
            )
            return False, None
        try:
            grid = self.grid_for(backend, float(scale))
        except ValueError as exc:
            logger.warning(
                f"the pixel API could not size {var_info.id} ({exc}); "
                "falling back to Earth Engine."
            )
            return False, None
        return True, EnginePlan.serves(tiles=grid.tiles(_TILE_PIXELS), engine=self.name)

    def grid_for(self, backend: GEE, scale: float) -> OutputGrid:
        """Return the output grid this engine would request.

        Uses the metre convention: `computePixels` takes an explicit `grid`, so
        the pixels are sized on the ground rather than as a degree-equivalent.

        Args:
            backend: The `GEE` instance, for the AOI, CRS and cutline.
            scale: Ground sample distance in metres.

        Returns:
            The sized grid.
        """
        bbox, cutline = backend._eedai_window()
        return OutputGrid.for_metre_grid(
            bbox, crs=backend.crs, scale=scale, cutline=cutline
        )

    def request(
        self, backend: GEE, image: Any, grid: OutputGrid, bands: list[str]
    ) -> dict:
        """Build the `computePixels` request body for one read.

        Args:
            backend: The `GEE` instance, for the export config.
            image: The `ee.Image` to evaluate.
            grid: The output grid to sample on.
            bands: The bands to return.

        Returns:
            The keyword arguments for `ee.data.computePixels`.
        """
        body: dict[str, Any] = {
            "expression": image,
            "fileFormat": "GEO_TIFF",
            "grid": {
                "dimensions": {"width": grid.cols, "height": grid.rows},
                "crsCode": grid.crs,
            },
        }
        if bands:
            body["bandIds"] = list(bands)
        nodata = backend._export_config.nodata
        if nodata is not None:
            body["grid"]["nodata"] = nodata
        return body

    @staticmethod
    def endpoint(high_volume: bool) -> str | None:
        """Return the host to initialise against, or `None` for the default.

        Args:
            high_volume: Whether the caller opted into the high-volume endpoint.

        Returns:
            The high-volume host when asked for, else `None`.
        """
        return HIGH_VOLUME_ENDPOINT if high_volume else None

    def refusal_is_fatal(self) -> bool:
        """Whether a refusal arriving after routing should propagate.

        Returns:
            `True` — this engine is only reached by being asked for explicitly,
            so its refusal is the answer.
        """
        return True


#: Tiles are sized the same as the EEDAI streamer's, so a tiled request through
#: either engine cuts the same way.
_TILE_PIXELS: int = 2048

# Registered on import, which is the whole point: adding an engine is a subclass
# plus this line, with no edit to `_use_eedai` or `_api`.
ENGINES[PixelsEngine.name] = PixelsEngine
