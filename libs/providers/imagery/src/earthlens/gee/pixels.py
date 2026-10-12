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

So this engine is the first whose routing verdict is both **compute-capable** and
**not cap-bound** — unlike `EedaiEngine`, it does not decline a `cloud_mask` or
`filters`, because `computePixels` evaluates the graph server-side.

**What it contributes today.** The engine owns the decision and the request: the
serve/decline verdict (:meth:`PixelsEngine.plan`), the output grid
(:meth:`PixelsEngine.grid_for`), the `computePixels` request body
(:meth:`PixelsEngine.request`) and the endpoint to initialise against
(:meth:`PixelsEngine.endpoint`). The call that posts that body is not wired into
the backend's export path yet, so a request routed here is still served by the
export mechanics the backend has — exactly the split
:mod:`earthlens.gee.engines` describes, where the decision moved and the
mechanics did not.

It lands with the `Engine` ABC rather than after it because the ABC's value is
the claim that a new engine is a subclass plus a registry entry. This engine is
that claim exercised: it registers itself on import, with no edit to the
routing or export methods.

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
    """Plan and shape a read for `ee.data.computePixels` / `ee.data.getPixels`.

    Accepts a computed image, so unlike the EEDAI reader it does not decline a
    `cloud_mask` or `filters`. It sizes its request from an
    :class:`~earthlens.gee.grid.OutputGrid` rather than deriving a grid of its
    own, so it cannot disagree with the other engines about the output shape.

    It answers for the routing verdict, the grid, the request body and the
    endpoint; posting that body is not part of the backend's export path yet.
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
        # Refuse rather than route. `_api` sends every `(True, plan)` to the
        # EEDAI reader - it has no `plan.engine` dispatch, because the reader
        # path is reached before the composited image exists. So returning a
        # servable plan here would hand a masked/filtered request to a reader
        # that materialises *stored* pixels, silently dropping the very compute
        # this engine exists to serve. Wiring the fetch means threading the
        # image into that branch, which is a change to the download path proper.
        raise NotImplementedError(
            "engine='pixels' can size and describe a computePixels request but "
            "cannot yet fetch one: the backend has no dispatch for it, and "
            "routing it would silently serve the request from stored pixels "
            "with any cloud_mask or filters dropped. Use engine='auto' (or "
            "'ee') until the fetch path lands."
        )

    def verdict(
        self,
        backend: GEE,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EnginePlan | None]:
        """Return the plan this engine *would* serve, without routing to it.

        The sizing half of :meth:`plan`, split out so it stays exercisable while
        the fetch path is unwired. It is deliberately not called by the backend.

        Args:
            backend: The `GEE` instance, for its config and the grid.
            var_info: The catalog entry being read.
            band_count: How many bands the read requests.
            bucket_start: The bucket's inclusive start, for a collection read.
            bucket_end: The bucket's exclusive end, for a collection read.

        Returns:
            `(True, plan)` when the pixel API could serve the request, else
            `(False, None)`.
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
            The sized grid, carrying the AOI's cutline alongside the bbox.

        Raises:
            ValueError: If the AOI reprojects to non-finite bounds in the
                output CRS, or `scale` is not a positive number of metres.
                :meth:`plan` catches this and falls back rather than failing
                the download.
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
            image: The `ee.Image` to evaluate. It becomes the request's
                `expression`, which is what makes a composite fetchable.
            grid: The output grid to sample on, which supplies the request's
                pixel dimensions and CRS.
            bands: The bands to return; an empty list leaves `bandIds` out, so
                the image's own bands are returned.

        Returns:
            The request body for `ee.data.computePixels`: `expression`,
            `fileFormat`, `grid`, and `bandIds` when bands were named. The
            configured `nodata` is declared on the grid when one was set.
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
# plus this line. `engine='pixels'` becomes selectable because the accepted names
# are derived from this dict, not restated next to the routing methods.
ENGINES[PixelsEngine.name] = PixelsEngine
