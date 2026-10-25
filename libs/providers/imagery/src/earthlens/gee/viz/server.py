"""Earth-Engine server-side previews: a thumbnail still and an XYZ tile source.

The only part of visualization that is genuinely Earth Engine's. Both objects
operate on an `ee.Image` **before** anything is downloaded, and neither imports
a renderer: `ServerThumbnail` asks Earth Engine to render a PNG and hands back
its URL, and `MapTiles` asks for a tile endpoint and hands back a `{z}/{x}/{y}`
template that digitalearth's `tiles(url=...)` can draw directly.

This is the cheap path for "show me roughly what is there". Nothing is read
into memory, no export is queued, and the request is not subject to the
32768-px per-axis download cap, because Earth Engine is rasterising its own
preview rather than serving pixels.

Both take their vis-params from `viz._style`, so a preview is stretched the way
the publisher intended rather than with Earth Engine's unhelpful default of
"full dynamic range of the data type".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict

from earthlens.gee._helpers import NoBool
from earthlens.gee.viz._style import StyleHints

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee

__all__ = ["MapTiles", "ServerThumbnail", "TileSource"]

#: Earth Engine's own cap on a `getThumbURL` axis. Beyond this the service
#: refuses the request rather than downsampling, so it is checked here with a
#: message naming the remedy instead of letting the SDK raise from inside.
MAX_THUMB_DIMENSION = 4096


def _vis_params(hints: StyleHints | None) -> dict[str, Any]:
    """Spell style hints as an Earth Engine `visParams` mapping.

    Earth Engine's own vocabulary, which is not digitalearth's: `bands`,
    `min`, `max`, `palette`, `gamma`. A single-band stretch passes scalars; a
    composite passes the per-band lists Earth Engine expects.

    Args:
        hints: The hints to translate, or `None` for Earth Engine's defaults.

    Returns:
        The `visParams` mapping, with absent hints omitted.
    """
    out: dict[str, Any] = {}
    if hints is not None:
        out["bands"] = list(hints.bands)
        if hints.limits:
            out["min"] = [lo for lo, _ in hints.limits]
            out["max"] = [hi for _, hi in hints.limits]
        if hints.palette:
            out["palette"] = list(hints.palette)
        if hints.gamma:
            out["gamma"] = list(hints.gamma)
    return out


class TileSource(BaseModel):
    """A resolved Earth Engine XYZ tile endpoint.

    What `MapTiles.resolve` returns, and what digitalearth's
    `WebMap.tiles(url=...)` / `InteractiveMap.tiles(url=...)` consume. It is a
    plain value object: the URL template, plus the attribution a published map
    is expected to carry.

    Attributes:
        url: The `{z}/{x}/{y}` tile template, ready to hand to a tile layer.
        attribution: Credit line for the layer. Earth Engine's terms require
            attribution on a published map, so this defaults to something
            usable rather than empty.
        map_id: The Earth Engine map id the template was built from, kept for
            debugging a tile endpoint that stops serving.

    Examples:
        - A tile source is just a template plus its credit:
            ```python
            >>> src = TileSource(url="https://earthengine.googleapis.com/v1/x/tiles/{z}/{x}/{y}")
            >>> "{z}/{x}/{y}" in src.url
            True
            >>> src.attribution
            'Google Earth Engine'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    attribution: str = "Google Earth Engine"
    map_id: str | None = None


class ServerThumbnail(BaseModel):
    """A still preview rendered by Earth Engine, as a URL (or its bytes).

    Wraps `ee.Image.getThumbURL`. Nothing is downloaded to produce the URL —
    Earth Engine renders the PNG on demand when the URL is fetched — so this is
    the cheapest possible look at a request.

    Attributes:
        dimensions: Longest axis of the thumbnail in pixels. Earth Engine caps
            an axis at `MAX_THUMB_DIMENSION`; a larger value is refused here,
            with the remedy named.
        region: The area to render, as an `ee.Geometry` or a GeoJSON mapping.
            `None` lets Earth Engine use the image's own footprint, which for
            a global asset means the whole world.
        image_format: `"png"` (transparent where masked) or `"jpg"` (smaller,
            no transparency).
        crs: Output projection for the preview.

    Examples:
        - The defaults are a 512-px PNG in WGS 84:
            ```python
            >>> thumb = ServerThumbnail()
            >>> (thumb.dimensions, thumb.image_format, thumb.crs)
            (512, 'png', 'EPSG:4326')

            ```
        - An oversize request is refused at construction, not by the SDK:
            ```python
            >>> ServerThumbnail(dimensions=8192)
            Traceback (most recent call last):
                ...
            ValueError: dimensions=8192 exceeds Earth Engine's 4096-px thumbnail cap; use MapTiles for an interactive view, or export the pixels

            ```
        - A boolean is not a pixel count:
            ```python
            >>> ServerThumbnail(dimensions=True)  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ServerThumbnail

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    dimensions: Annotated[int, NoBool] = 512
    region: Any = None
    image_format: str = "png"
    crs: str = "EPSG:4326"

    def model_post_init(self, _context: Any, /) -> None:
        """Refuse a thumbnail axis Earth Engine will not render.

        Raises:
            ValueError: If `dimensions` exceeds `MAX_THUMB_DIMENSION`.
        """
        if self.dimensions > MAX_THUMB_DIMENSION:
            raise ValueError(
                f"dimensions={self.dimensions} exceeds Earth Engine's "
                f"{MAX_THUMB_DIMENSION}-px thumbnail cap; use MapTiles for an "
                f"interactive view, or export the pixels"
            )

    def url(self, image: ee.Image, hints: StyleHints | None = None) -> str:
        """Return the URL Earth Engine will render this thumbnail at.

        Args:
            image: The image to preview.
            hints: Style hints from `viz.style_for`, or `None` for Earth
                Engine's own defaults.

        Returns:
            The `getThumbURL` string.
        """
        params: dict[str, Any] = {
            **_vis_params(hints),
            "dimensions": self.dimensions,
            "format": self.image_format,
            "crs": self.crs,
        }
        if self.region is not None:
            params["region"] = self.region
        return str(image.getThumbURL(params))

    def fetch(self, image: ee.Image, hints: StyleHints | None = None) -> bytes:
        """Render the thumbnail and return the PNG/JPEG bytes.

        The one method here that touches the network beyond Earth Engine's own
        call, so it is deliberately separate from `url`: a caller that only
        wants a link to embed should not pay for the render.

        Args:
            image: The image to preview.
            hints: Style hints, or `None`.

        Returns:
            The encoded image bytes.
        """
        import requests

        response = requests.get(self.url(image, hints), timeout=120)
        response.raise_for_status()
        return response.content


class MapTiles(BaseModel):
    """An Earth Engine tile endpoint for an interactive or web map.

    Wraps `ee.Image.getMapId`, whose response carries a tile-fetcher whose URL
    format is the `{z}/{x}/{y}` template a slippy map wants. earthlens resolves
    the template; digitalearth draws the layer.

    Attributes:
        name: Layer name to carry into the map's legend or layer list.

    Examples:
        - The layer name is the only knob; the rest comes from the image:
            ```python
            >>> MapTiles(name="NDVI").name
            'NDVI'
            >>> MapTiles().name is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None

    def resolve(self, image: ee.Image, hints: StyleHints | None = None) -> TileSource:
        """Ask Earth Engine for a tile endpoint and return it as a `TileSource`.

        Args:
            image: The image to serve as tiles.
            hints: Style hints from `viz.style_for`, or `None` for Earth
                Engine's own defaults.

        Returns:
            The `TileSource` whose `url` is a `{z}/{x}/{y}` template.

        Raises:
            ValueError: If the `getMapId` response carries neither a
                `tile_fetcher` nor a usable URL format — the two shapes the
                SDK has used — so a silently unusable layer is impossible.
        """
        info = image.getMapId(_vis_params(hints))
        fetcher = info.get("tile_fetcher") if hasattr(info, "get") else None
        template = ""
        if fetcher is not None and hasattr(fetcher, "url_format"):
            template = str(fetcher.url_format)
        elif hasattr(info, "get"):
            template = str(info.get("urlFormat") or info.get("url_format") or "")
        if not template:
            keys = sorted(info) if hasattr(info, "__iter__") else []
            raise ValueError(
                f"the getMapId response carries no tile URL format, so there "
                f"is no tile endpoint to draw; got keys {keys}"
            )
        map_id = info.get("mapid") if hasattr(info, "get") else None
        return TileSource(
            url=template,
            map_id=str(map_id) if map_id else None,
        )
