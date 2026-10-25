"""The thin earthlens-side facade over digitalearth.

`Visualizer` exists for ergonomics, not for rendering. It binds a
`DownloadResult` to the dataset's style hints and then calls a digitalearth
verb — `quickmap` for a still, `animate` for a timelapse, the chart functions
for a series, `tiles(url=...)` for an Earth Engine tile layer. Every method is
the same three steps: resolve hints, build the kwargs, delegate. There is no
matplotlib, no cleopatra and no pixel arithmetic in this module, and there must
never be: the moment a figure is composed here rather than in digitalearth, the
layering this epic exists to enforce is gone.

digitalearth is imported **lazily, inside the method that needs it**, and is
declared as the `earthlens-imagery[viz]` extra rather than a base dependency.
A bare earthlens install therefore pulls no plotting stack at all, and
importing `earthlens.gee` does not import digitalearth — a boundary test in
`libs/core/tests/test_distribution_boundaries.py` holds that line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from earthlens.gee.viz._style import StyleHints, style_for
from earthlens.gee.viz.server import MapTiles, ServerThumbnail, TileSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    import ee

    from earthlens.gee.work import DownloadResult

__all__ = ["Visualizer"]

#: What to tell a caller who has not installed the extra. Named once so the
#: message cannot drift between the methods that need digitalearth.
_MISSING_VIZ = (
    "visualization needs digitalearth, which ships with the `viz` extra: "
    "`pip install earthlens[viz]` (or `uv sync --extra viz`). earthlens does "
    "no rendering of its own by design"
)

#: digitalearth's backend names, exactly as `quickmap(backend=...)` spells
#: them. Note `"matplotlib"`, not `"static"`: the tier's package is
#: `digitalearth.static` but its backend name is the renderer's.
_BACKENDS = ("matplotlib", "interactive", "web", "3d")

#: The tile-capable backends, with the class each one's tile layer lives on.
#: `"matplotlib"` draws no slippy map and the 3-D tier spells the verb
#: `tiles_3d` with different semantics, so neither is offered by `tile_map`.
_TILE_SCENES = {
    "web": ("digitalearth.web", "WebMap"),
    "interactive": ("digitalearth.interactive", "InteractiveMap"),
}

#: The RGB-composite scene per backend, as (module, class, verb). `quickmap`
#: has no RGB renderer -- its raster kinds are all single-band -- so a composite
#: is drawn by building the tier's own map and calling its verb. All three take
#: the same `limits` keyword; only the name differs (interactive spells it
#: `rgb`). The 3-D tier has no RGB composite, so it is absent and refused.
_RGB_SCENES = {
    "matplotlib": ("digitalearth", "Map", "rgb_composite"),
    "web": ("digitalearth.web", "WebMap", "rgb_composite"),
    "interactive": ("digitalearth.interactive", "InteractiveMap", "rgb"),
}


def _digitalearth() -> Any:
    """Import digitalearth, or raise with the extra to install.

    Returns:
        The imported `digitalearth` module.

    Raises:
        ImportError: If digitalearth is not installed.
    """
    try:
        import digitalearth
    except ImportError as exc:  # pragma: no cover - exercised with a stubbed import
        raise ImportError(_MISSING_VIZ) from exc
    return digitalearth


class Visualizer:
    """Render a GEE download through digitalearth, with catalog-aware defaults.

    Reached as `GEE.visualize(result)`. It holds the result, the asset id and
    the band selection, resolves the style hints once, and delegates each call
    to digitalearth.

    Attributes:
        result: The `DownloadResult` being visualized.
        asset_id: The Earth Engine asset the result came from, used to look up
            the catalog's style hints.
        bands: The band ids to draw, or `None` to take the dataset's own
            default rendering.

    Examples:
        - A visualizer resolves its hints from the catalog up front:
            ```python
            >>> from earthlens.gee.viz import Visualizer
            >>> viz = Visualizer(result=None, asset_id="USGS/SRTMGL1_003")
            >>> viz.hints.bands
            ('elevation',)
            >>> viz.hints.source
            'heuristic'

            ```
        - An unknown backend is refused before digitalearth is even imported:
            ```python
            >>> viz.still(backend="webgl")
            Traceback (most recent call last):
                ...
            ValueError: backend='webgl' is not a digitalearth backend; choose from 'matplotlib', 'interactive', 'web', '3d'

            ```
    """

    def __init__(
        self,
        result: DownloadResult | None,
        asset_id: str,
        bands: tuple[str, ...] | None = None,
        *,
        name: str | None = None,
    ) -> None:
        """Bind a result to its dataset's style hints.

        Args:
            result: The download to render, or `None` to use the visualizer
                for style resolution and server-side previews only.
            asset_id: The Earth Engine asset id the result came from.
            bands: Band ids to draw, or `None` for the dataset default.
            name: A specific publisher rendering by `display_name`.
        """
        self.result = result
        self.asset_id = asset_id
        self.bands = bands
        self.hints: StyleHints = style_for(asset_id, bands, name=name)

    def __repr__(self) -> str:
        """Return a short, non-rendering summary."""
        return (
            f"Visualizer({self.asset_id!r}, bands={self.hints.bands!r}, "
            f"source={self.hints.source!r})"
        )

    def _checked_backend(self, backend: str, allowed: tuple[str, ...]) -> str:
        """Refuse a backend digitalearth does not have, naming the choices.

        Args:
            backend: The requested backend name.
            allowed: The backends this call accepts.

        Returns:
            The backend, unchanged, when it is one of `allowed`.

        Raises:
            ValueError: If `backend` is not in `allowed`.
        """
        if backend not in allowed:
            choices = ", ".join(repr(b) for b in allowed)
            raise ValueError(
                f"backend={backend!r} is not a digitalearth backend; choose "
                f"from {choices}"
            )
        return backend

    def _data(self) -> Any:
        """Return the bound result's in-memory object.

        Returns:
            The pyramids `Dataset` / `DatasetCollection` the result carries.

        Raises:
            ValueError: If no result is bound, or it holds paths only because
                the download was made without `materialize=True`.
        """
        if self.result is None:
            raise ValueError(
                "this Visualizer was built without a result, so there is "
                "nothing to render; pass the DownloadResult from download()"
            )
        data = self.result.data
        if data is None:
            raise ValueError(
                "the download holds paths only, so there is no in-memory "
                "raster to render; call download(materialize=True)"
            )
        return data

    def still(self, backend: str = "matplotlib", **kwargs: Any) -> Any:
        """Draw one map, catalog-styled, through digitalearth.

        A single-band request goes through `quickmap` (the `field` / `imshow`
        renderer), stretched with the hints' `vmin` / `vmax`. A three-band
        request is an RGB composite, which `quickmap` cannot draw — its raster
        kinds are all single-band — so it is routed to the tier's own
        `rgb_composite` (the interactive tier spells it `rgb`) with the hints'
        per-band `limits`. Either way, anything in `kwargs` overrides the
        hints, so the catalog decides the default and the caller keeps the
        last word.

        Args:
            backend: digitalearth backend: `"matplotlib"`, `"interactive"`,
                `"web"` or `"3d"`.
            **kwargs: Passed through to the draw, overriding the hints.

        Returns:
            The digitalearth scene — a `Map` / `WebMap` / `InteractiveMap` for
            the chosen backend.

        Raises:
            ValueError: If `backend` is unknown, if an RGB request names a
                backend with no composite renderer (`"3d"`), or if no
                in-memory data is bound.
            ImportError: If digitalearth is not installed.
        """
        import importlib

        self._checked_backend(backend, _BACKENDS)
        data = self._data()
        module = _digitalearth()
        if not self.hints.is_rgb:
            merged = {**self.hints.field_kwargs(), **kwargs}
            return module.quickmap(data, backend=backend, **merged)
        if backend not in _RGB_SCENES:
            raise ValueError(
                f"backend={backend!r} has no RGB composite renderer; a "
                f"three-band request needs one of "
                f"{', '.join(repr(b) for b in _RGB_SCENES)}"
            )
        module_name, class_name, verb = _RGB_SCENES[backend]
        scene = getattr(importlib.import_module(module_name), class_name)()
        merged = {**self.hints.rgb_kwargs(), **kwargs}
        return getattr(scene, verb)(data, **merged)

    def timelapse(self, path: str | Path, **kwargs: Any) -> Any:
        """Animate the result's collection through digitalearth's `animate`.

        The whole collection is handed over in one call — digitalearth's
        animation builder is already `DatasetCollection`-aware, and emitting
        per-bucket frames here would be exactly the rendering logic this
        facade must not contain.

        Args:
            path: Output file for the animation (`.gif` / `.mp4`).
            **kwargs: Passed through to `animate`, overriding the hints.

        Returns:
            Whatever digitalearth's `animate` returns.

        Raises:
            ValueError: If no result is bound, if it wrote no files to stack
                (a batch export returns destinations, not rasters), or if it is
                a single raster rather than a time series.
            ImportError: If digitalearth is not installed.
        """
        if self.result is None:
            raise ValueError(
                "this Visualizer was built without a result, so there is "
                "nothing to animate; pass the DownloadResult from download()"
            )
        # `collection()` stacks the written files into a pyramids
        # `DatasetCollection` (file-backed via `from_files`), and raises its own
        # clear error when nothing was written — a batch export, say. It is a
        # method, not a property.
        cube = self.result.collection()
        if cube.time_length < 2:
            raise ValueError(
                "the download wrote a single raster, not a time series, so "
                "there is nothing to animate; request several dates with a "
                "temporal_resolution and download(materialize=True)"
            )
        if self.hints.is_rgb:
            kind = "rgb_composite"
            hint_kwargs = self.hints.rgb_kwargs()
        else:
            kind = "imshow"
            hint_kwargs = self.hints.field_kwargs()
        merged = {"kind": kind, **hint_kwargs, **kwargs}
        scene = _digitalearth().Map()
        animation = scene.animate(cube, **merged)
        animation.save(str(path))
        return animation

    def chart(self, kind: str, data: Any, **kwargs: Any) -> Any:
        """Draw a chart through digitalearth's own chart functions.

        Args:
            kind: One of digitalearth's chart verbs — `"line"`, `"scatter"`,
                `"histogram"`, `"bar"` or `"bar_by"`.
            data: The series / frame to chart, passed straight through.
            **kwargs: Passed through to the chart function.

        Returns:
            Whatever the digitalearth chart function returns.

        Raises:
            ValueError: If `kind` is not one of digitalearth's chart verbs.
            ImportError: If digitalearth is not installed.
        """
        charts = ("line", "scatter", "histogram", "bar", "bar_by")
        if kind not in charts:
            choices = ", ".join(repr(c) for c in charts)
            raise ValueError(
                f"kind={kind!r} is not a digitalearth chart; choose from {choices}"
            )
        return getattr(_digitalearth(), kind)(data, **kwargs)

    def thumbnail(self, image: ee.Image, *, dimensions: int = 512) -> str:
        """Return an Earth Engine `getThumbURL` for an image, stretched by the hints.

        Args:
            image: The `ee.Image` to preview.
            dimensions: Longest axis of the thumbnail in pixels.

        Returns:
            The thumbnail URL.
        """
        return ServerThumbnail(dimensions=dimensions).url(image, self.hints)

    def tiles(self, image: ee.Image, *, name: str | None = None) -> TileSource:
        """Resolve an Earth Engine XYZ tile endpoint for an image.

        Args:
            image: The `ee.Image` to serve as tiles.
            name: Layer name to carry onto the map.

        Returns:
            The `TileSource` whose `url` is a `{z}/{x}/{y}` template.
        """
        return MapTiles(name=name).resolve(image, self.hints)

    def tile_map(
        self,
        image: ee.Image,
        backend: str = "web",
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Put an Earth Engine tile layer on a digitalearth slippy map.

        This is the whole M2 seam: earthlens resolves the tile template inside
        Earth Engine, digitalearth draws it. No pixels are downloaded — the
        map's own viewport decides which tiles Earth Engine renders.

        The map is built **empty** — `WebMap()` and `InteractiveMap()` both
        construct with no data — so nothing local is drawn or downloaded. The
        Earth Engine layer is the whole view.

        Args:
            image: The `ee.Image` to serve as tiles.
            backend: `"web"` (MapLibre + deck.gl) or `"interactive"`
                (HoloViz). `"matplotlib"` draws no slippy map, and the 3-D
                tier's `tiles_3d` has different semantics, so neither is
                offered here.
            name: Layer name to carry onto the map.
            **kwargs: Passed through to digitalearth's `tiles`.

        Returns:
            The digitalearth map with the Earth Engine layer added.

        Raises:
            ValueError: If `backend` is not a tile-capable backend.
            ImportError: If digitalearth is not installed.
        """
        import importlib

        self._checked_backend(backend, tuple(_TILE_SCENES))
        _digitalearth()
        module_name, class_name = _TILE_SCENES[backend]
        scene = getattr(importlib.import_module(module_name), class_name)()
        source = self.tiles(image, name=name)
        return scene.tiles(
            url=source.url,
            attribution=source.attribution,
            name=name,
            **kwargs,
        )
