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

**What it owns.** The decision and the request: the serve/decline verdict
(:meth:`PixelsEngine.plan`), the output grid (:meth:`PixelsEngine.grid_for`),
the `computePixels` request body (:meth:`PixelsEngine.request`) and the endpoint
to initialise against (:meth:`PixelsEngine.endpoint`). `GEE._export_via_pixels`
posts that body and writes the GeoTIFF it returns, so `engine='pixels'` fetches
through this engine end to end.

Wiring it exposed a defect worth recording. The backend read "some engine
accepted" as "the EEDAI reader accepted", so the first request routed here was
served from stored pixels with the composite silently dropped. An accepted plan
now has to name its engine, which is why
:class:`~earthlens.gee.plans.EnginePlan` carries `engine` at all.

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

__all__ = ["PixelsEngine", "HIGH_VOLUME_ENDPOINT", "affine_transform"]

#: The host Earth Engine documents for parallel programmatic requests.
HIGH_VOLUME_ENDPOINT: str = "https://earthengine-highvolume.googleapis.com"


class PixelsEngine(Engine):
    """Plan and shape a read for `ee.data.computePixels` / `ee.data.getPixels`.

    Accepts a computed image, so unlike the EEDAI reader it does not decline a
    `cloud_mask` or `filters`. It sizes its request from an
    :class:`~earthlens.gee.grid.OutputGrid` rather than deriving a grid of its
    own, so it cannot disagree with the other engines about the output shape.

    It answers for the routing verdict, the grid, the request body and the
    endpoint; `GEE._export_via_pixels` posts that body.
    """

    name: ClassVar[str] = "pixels"

    #: Selectable: the backend dispatches a `pixels` plan to
    #: `GEE._export_via_pixels`, which posts the request this engine builds.
    implemented: ClassVar[bool] = True

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
            `(True, plan)` when the pixel API will serve the request, else
            `(False, None)` so the caller falls back to Earth Engine.
        """
        return self.verdict(backend, var_info)

    def verdict(
        self, backend: GEE, var_info: Dataset
    ) -> tuple[bool, EnginePlan | None]:
        """Return the plan this engine would serve, given only what sizes it.

        The sizing half of :meth:`plan`, which calls it. Split out because it
        takes only what it uses: the grid follows from the AOI, `crs` and either
        `scale` or a pinned `crs_transform`, so the band count and bucket window
        :meth:`plan` receives are not parameters here, and a caller after the
        grid verdict alone need not invent them.

        Args:
            backend: The `GEE` instance, for its config and the grid.
            var_info: The catalog entry being read.

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

        A `crs_transform` pins the grid outright, which is the one case where
        `scale` is not consulted: Earth Engine treats the two as an either/or,
        and the point of pinning is that two requests over different AOIs land
        on the same pixel boundaries. Otherwise the metre convention applies:
        `computePixels` takes an explicit `grid`, so the pixels are sized on the
        ground rather than as a degree-equivalent.

        Args:
            backend: The `GEE` instance, for the AOI, CRS, cutline and the
                optional pinned transform.
            scale: Ground sample distance in metres. Ignored when the request
                pins a `crs_transform`.

        Returns:
            The sized grid, carrying the AOI's cutline alongside the bbox.

        Raises:
            ValueError: If the AOI reprojects to non-finite bounds in the
                output CRS, `scale` is not a positive number of metres, or a
                pinned transform is malformed. :meth:`plan` catches this and
                falls back rather than failing the download.
        """
        pinned = backend._pinned_grid()
        if pinned is not None:
            return pinned
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
            `fileFormat`, and a `grid` of `dimensions` + `affineTransform` +
            `crsCode`, plus `bandIds` when bands were named. The configured
            `nodata` is declared on the grid when one was set.
        """
        body: dict[str, Any] = {
            "expression": image,
            "fileFormat": "GEO_TIFF",
            "grid": {
                "dimensions": {"width": grid.cols, "height": grid.rows},
                "crsCode": grid.crs,
            },
        }
        # Always, not only for a pinned grid: dimensions alone leave Earth Engine
        # to use the CRS's default transform, which returned a 90 m AOI over
        # Cairo as 1-degree pixels starting at lat/lon 0. The transform fixes
        # where the pixels fall, the dimensions how many come back.
        body["grid"]["affineTransform"] = affine_transform(grid.request_transform())
        if bands:
            body["bandIds"] = list(bands)
        nodata = backend._export_config.nodata
        if nodata is not None:
            body["grid"]["nodata"] = nodata
        return body

    def endpoint(self, high_volume: bool) -> str | None:
        """Return the host to initialise against, or `None` for the default.

        Overrides the ABC's `None`, which is how `GEE(high_volume=True)` reaches
        `ee.Initialize` for this engine and for no other: the endpoint is
        documented for the pixel API's parallel requests, and its trade-off
        (less caching, more EECU-time on complex queries) is only worth taking
        where that applies.

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


def affine_transform(crs_transform: tuple[float, ...]) -> dict[str, float]:
    """Spell a 6-element transform the way Earth Engine's `PixelGrid` wants it.

    The tuple order is the one
    :meth:`~earthlens.gee.grid.OutputGrid.from_crs_transform` documents, which
    is also the order Earth Engine's own `crsTransform` lists, so this is a
    renaming and not a reordering.

    Args:
        crs_transform: The 6-element affine transform
            `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`.

    Returns:
        The same six terms keyed as `PixelGrid.affineTransform` expects.

    Examples:
        - A north-up 10 m grid in a projected CRS:
            ```python
            >>> terms = affine_transform((10.0, 0.0, 300000.0, 0.0, -10.0, 3400000.0))
            >>> terms["scaleX"], terms["scaleY"], terms["translateX"]
            (10.0, -10.0, 300000.0)
            >>> sorted(terms)
            ['scaleX', 'scaleY', 'shearX', 'shearY', 'translateX', 'translateY']

            ```
    """
    x_scale, x_shear, x_origin, y_shear, y_scale, y_origin = crs_transform
    return {
        "scaleX": x_scale,
        "shearX": x_shear,
        "translateX": x_origin,
        "shearY": y_shear,
        "scaleY": y_scale,
        "translateY": y_origin,
    }


#: Tiles are sized the same as the EEDAI streamer's, so a tiled request through
#: either engine cuts the same way.
_TILE_PIXELS: int = 2048

# Registered on import, which is the whole point: adding an engine is a subclass
# plus this line. `engine='pixels'` becomes selectable because the accepted names
# are derived from this dict, not restated next to the routing methods.
ENGINES[PixelsEngine.name] = PixelsEngine
