"""Visualization for the GEE backend — Earth Engine previews and style hints.

earthlens grows no plotting layer. Every figure, map, animation and chart is
drawn by **digitalearth** over **cleopatra**; this package contributes only the
two things that are genuinely Earth Engine's and cannot live in a renderer:

* **Style hints** (`style_for`, `StyleHints`) — the GEE catalog's curated
  band ranges and the publisher's own `gee:visualizations` translated into the
  keyword arguments a digitalearth verb takes. Pure, catalog-only, importable
  without the `[viz]` extra.
* **Server-side previews** (`ServerThumbnail`, `MapTiles`, `TileSource`) — a
  `getThumbURL` still and a `getMapId` XYZ tile template, both resolved inside
  Earth Engine before any pixels are downloaded.

`Visualizer` is the thin facade that pairs the two with digitalearth, reached
as `GEE.visualize(result)`. It holds no rendering logic: each method builds a
frozen config and calls a digitalearth verb, and it is the only thing here that
needs `earthlens[viz]` installed.
"""

from __future__ import annotations

from earthlens.gee.viz._style import CMAP_MATCHES, StyleHints, cmap_for, style_for
from earthlens.gee.viz.facade import Visualizer
from earthlens.gee.viz.server import MapTiles, ServerThumbnail, TileSource

__all__ = [
    "CMAP_MATCHES",
    "MapTiles",
    "ServerThumbnail",
    "StyleHints",
    "TileSource",
    "Visualizer",
    "cmap_for",
    "style_for",
]
