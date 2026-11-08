"""Tests for the Earth-Engine-side previews `ServerThumbnail` / `MapTiles` (Task M1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.viz._style import StyleHints
from earthlens.gee.viz.server import (
    MAX_THUMB_DIMENSION,
    MapTiles,
    ServerThumbnail,
    TileSource,
    _vis_params,
)

_TILE_URL = (
    "https://earthengine.googleapis.com/v1/projects/p/maps/abc/tiles/{z}/{x}/{y}"
)


class _FakeImage:
    """An `ee.Image` stand-in recording the params each preview call received."""

    def __init__(self, map_info=None):
        self.thumb_params = None
        self.map_params = None
        self._map_info = (
            map_info
            if map_info is not None
            else {"mapid": "projects/p/maps/abc", "tile_fetcher": _FakeFetcher()}
        )

    def getThumbURL(self, params):  # noqa: N802 - mirrors the ee SDK's spelling
        self.thumb_params = params
        return "https://earthengine.googleapis.com/thumb/abc.png"

    def getMapId(self, params):  # noqa: N802 - mirrors the ee SDK's spelling
        self.map_params = params
        return self._map_info


class _FakeFetcher:
    """The `tile_fetcher` object `getMapId` returns in current earthengine-api."""

    url_format = _TILE_URL


_RGB = StyleHints(
    bands=("B4", "B3", "B2"),
    limits=((0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0)),
    source="catalog",
)
_SINGLE = StyleHints(
    bands=("NDVI",),
    limits=((0.0, 8000.0),),
    palette=("ffffff", "004c00"),
    gamma=(1.2,),
    source="catalog",
)


class TestVisParams:
    """Translation into Earth Engine's own visParams vocabulary."""

    def test_no_hints_passes_nothing(self):
        """Earth Engine's own defaults survive when earthlens has no opinion."""
        assert _vis_params(None) == {}

    def test_a_composite_passes_per_band_min_and_max_lists(self):
        """Earth Engine takes parallel min/max lists, not pairs."""
        params = _vis_params(_RGB)
        assert params["bands"] == ["B4", "B3", "B2"]
        assert params["min"] == [0.0, 0.0, 0.0]
        assert params["max"] == [3000.0, 3000.0, 3000.0]

    def test_palette_and_gamma_are_forwarded(self):
        """The publisher's ramp and gamma reach Earth Engine."""
        params = _vis_params(_SINGLE)
        assert params["palette"] == ["ffffff", "004c00"]
        assert params["gamma"] == [1.2]

    def test_a_band_with_no_stretch_passes_no_min_or_max(self):
        """Half a stretch is never sent."""
        params = _vis_params(StyleHints(bands=("b1",), source="heuristic"))
        assert params == {"bands": ["b1"]}


class TestServerThumbnail:
    """The `getThumbURL` still."""

    def test_the_defaults_are_a_512px_png_in_wgs84(self):
        """A preview needs no configuration to be useful."""
        thumb = ServerThumbnail()
        assert (thumb.dimensions, thumb.image_format, thumb.crs) == (
            512,
            "png",
            "EPSG:4326",
        )

    def test_the_url_carries_the_dimensions_format_and_crs(self):
        """What the caller configured reaches the SDK call."""
        image = _FakeImage()
        ServerThumbnail(dimensions=256, image_format="jpg", crs="EPSG:3857").url(image)
        assert image.thumb_params["dimensions"] == 256
        assert image.thumb_params["format"] == "jpg"
        assert image.thumb_params["crs"] == "EPSG:3857"

    def test_the_style_hints_reach_the_thumbnail(self):
        """A preview is stretched the way the publisher intended."""
        image = _FakeImage()
        ServerThumbnail().url(image, _RGB)
        assert image.thumb_params["bands"] == ["B4", "B3", "B2"]
        assert image.thumb_params["max"] == [3000.0, 3000.0, 3000.0]

    def test_no_region_is_passed_when_none_is_set(self):
        """An absent region lets Earth Engine use the image's own footprint."""
        image = _FakeImage()
        ServerThumbnail().url(image)
        assert "region" not in image.thumb_params

    def test_a_region_is_passed_when_given(self):
        """An explicit region narrows the preview."""
        image = _FakeImage()
        ServerThumbnail(region={"type": "Point", "coordinates": [0, 0]}).url(image)
        assert image.thumb_params["region"]["type"] == "Point"

    def test_an_oversize_thumbnail_is_refused_at_construction(self):
        """The cap is checked here, with the remedy named, not inside the SDK."""
        with pytest.raises(ValueError, match="thumbnail cap"):
            ServerThumbnail(dimensions=MAX_THUMB_DIMENSION + 1)

    def test_the_cap_itself_is_allowed(self):
        """The limit is inclusive, so the largest legal preview still builds."""
        assert ServerThumbnail(dimensions=MAX_THUMB_DIMENSION).dimensions == 4096

    def test_a_bool_is_not_a_pixel_count(self):
        """`bool` is an `int` subclass, so it is refused before coercion."""
        with pytest.raises(ValidationError):
            ServerThumbnail(dimensions=True)

    def test_an_unknown_field_is_refused(self):
        """A typo cannot read as an unset option."""
        with pytest.raises(ValidationError):
            ServerThumbnail(dimension=512)


class TestTileSource:
    """The resolved tile endpoint."""

    def test_attribution_defaults_to_earth_engine(self):
        """Earth Engine's terms require credit, so it is not left empty."""
        assert TileSource(url=_TILE_URL).attribution == "Google Earth Engine"

    def test_an_unknown_field_is_refused(self):
        """A frozen value object with no silent extras."""
        with pytest.raises(ValidationError):
            TileSource(url=_TILE_URL, attributon="x")


class TestMapTiles:
    """The `getMapId` XYZ endpoint."""

    def test_the_tile_fetchers_url_format_becomes_the_template(self):
        """The current SDK shape resolves to a `{z}/{x}/{y}` template."""
        source = MapTiles().resolve(_FakeImage())
        assert "{z}/{x}/{y}" in source.url
        assert source.map_id == "projects/p/maps/abc"

    def test_a_response_with_only_urlformat_still_resolves(self):
        """The older SDK shape is read too, rather than failing."""
        image = _FakeImage(map_info={"urlFormat": _TILE_URL, "mapid": "m"})
        assert MapTiles().resolve(image).url == _TILE_URL

    def test_the_style_hints_reach_the_tile_request(self):
        """Tiles are stretched like the thumbnail, from the same hints."""
        image = _FakeImage()
        MapTiles().resolve(image, _SINGLE)
        assert image.map_params["palette"] == ["ffffff", "004c00"]

    def test_a_response_with_no_url_is_refused_naming_its_keys(self):
        """An unusable layer is impossible: no template means an error."""
        image = _FakeImage(map_info={"mapid": "m"})
        tiles = MapTiles()
        with pytest.raises(ValueError, match="no tile URL format"):
            tiles.resolve(image)

    def test_the_layer_name_is_carried(self):
        """A name given for the legend is kept."""
        assert MapTiles(name="NDVI").name == "NDVI"
