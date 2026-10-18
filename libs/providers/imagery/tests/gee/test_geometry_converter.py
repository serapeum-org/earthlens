"""Tests for the GeometryConverter vector bridge."""

from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import (
    LinearRing,
    LineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

from earthlens.gee.vector import geometry as geometry_module
from earthlens.gee.vector.geometry import GeometryConverter, GeometryConverterConfig

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
_SQUARE_2 = Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])


class _FakeGeometry:
    """Records the factory kind and args in place of `ee.Geometry.*`."""

    def __init__(self, kind: str, coords, crs):
        self.kind = kind
        self.coords = coords
        self.crs = crs


class _FakeFeature:
    """Stands in for `ee.Feature(geometry[, properties])`."""

    def __init__(self, geometry, properties=None):
        self.geometry = geometry
        self.properties = properties


class _FakeFeatureCollection:
    """Stands in for `ee.FeatureCollection([Feature, ...])`."""

    def __init__(self, features):
        self.features = list(features)


@pytest.fixture
def fake_ee(monkeypatch):
    """Replace the converter module's `ee` with a recording fake."""
    fake = SimpleNamespace(
        Geometry=SimpleNamespace(
            Polygon=lambda coords, crs=None: _FakeGeometry("Polygon", coords, crs),
            Point=lambda coords, crs=None: _FakeGeometry("Point", coords, crs),
            LineString=lambda coords, crs=None: _FakeGeometry("Line", coords, crs),
        ),
        Feature=lambda geometry, properties=None: _FakeFeature(geometry, properties),
        FeatureCollection=lambda features: _FakeFeatureCollection(features),
    )
    monkeypatch.setattr(geometry_module, "ee", fake)
    return fake


class TestGeometryConverterConfig:
    """The frozen configuration a converter carries."""

    def test_defaults_are_wgs84_and_all_columns(self):
        """The default config is EPSG:4326 with no column narrowing."""
        config = GeometryConverterConfig()
        assert config.epsg == 4326
        assert config.columns is None

    def test_it_is_frozen(self):
        """A converter's configuration cannot be mutated after construction."""
        config = GeometryConverterConfig(epsg=3857)
        with pytest.raises(Exception, match="frozen"):
            config.epsg = 4326


class TestToEeGeometry:
    """Converting a single Shapely geometry."""

    def test_a_polygon_carries_the_projection(self, fake_ee):
        """A polygon converts through the Polygon factory with the EPSG string."""
        geom = GeometryConverter(epsg=3857).to_ee_geometry(_SQUARE)
        assert geom.kind == "Polygon"
        assert geom.crs == "epsg:3857"

    def test_a_point_converts(self, fake_ee):
        """A point converts through the Point factory."""
        assert GeometryConverter().to_ee_geometry(Point(3, 4)).kind == "Point"

    def test_a_linestring_converts(self, fake_ee):
        """Unlike the free create_geometry, the class accepts a LineString."""
        geom = GeometryConverter().to_ee_geometry(LineString([(0, 0), (1, 1)]))
        assert geom.kind == "Line"
        assert geom.crs == "epsg:4326"

    def test_an_unsupported_type_is_refused(self, fake_ee):
        """A MultiPoint has no single-geometry factory here."""
        converter = GeometryConverter()
        geom = MultiPoint([(0, 0), (1, 1)])
        with pytest.raises(NotImplementedError, match="MultiPoint"):
            converter.to_ee_geometry(geom)


class TestToEe:
    """Converting a whole GeoDataFrame to a FeatureCollection."""

    def test_rows_become_features_with_properties(self, fake_ee):
        """Each row becomes one feature carrying its non-geometry columns."""
        gdf = gpd.GeoDataFrame(
            {"name": ["a", "b"], "geometry": [_SQUARE, _SQUARE_2]}, crs="EPSG:4326"
        )
        fc = GeometryConverter().to_ee(gdf)
        assert len(fc.features) == 2
        assert fc.features[0].properties == {"name": "a"}

    def test_columns_narrows_the_properties(self, fake_ee):
        """Only the configured columns survive as feature properties."""
        gdf = gpd.GeoDataFrame(
            {"name": ["a"], "value": [1], "geometry": [_SQUARE]}, crs="EPSG:4326"
        )
        fc = GeometryConverter(columns=["name"]).to_ee(gdf)
        assert fc.features[0].properties == {"name": "a"}

    def test_a_geometry_only_frame_has_no_properties(self, fake_ee):
        """With no non-geometry columns, features are built geometry-only."""
        gdf = gpd.GeoDataFrame({"geometry": [_SQUARE]}, crs="EPSG:4326")
        fc = GeometryConverter().to_ee(gdf)
        assert fc.features[0].properties is None

    def test_a_multipolygon_row_explodes(self, fake_ee):
        """A MultiPolygon row is exploded to one feature per polygon part."""
        gdf = gpd.GeoDataFrame(
            {"geometry": [MultiPolygon([_SQUARE, _SQUARE_2])]}, crs="EPSG:4326"
        )
        assert len(GeometryConverter().to_ee(gdf).features) == 2

    def test_a_multipoint_row_explodes(self, fake_ee):
        """A MultiPoint row is exploded to one feature per point part."""
        gdf = gpd.GeoDataFrame(
            {"geometry": [MultiPoint([(0, 0), (1, 1)])]}, crs="EPSG:4326"
        )
        assert len(GeometryConverter().to_ee(gdf).features) == 2

    def test_an_unconvertible_row_is_named(self, fake_ee):
        """A row the converter cannot handle raises naming its index and type."""
        gdf = gpd.GeoDataFrame(
            {"geometry": [_SQUARE, LinearRing([(0, 0), (1, 0), (1, 1), (0, 0)])]},
            crs="EPSG:4326",
        )
        converter = GeometryConverter()
        with pytest.raises(ValueError, match="row 1 .LinearRing."):
            converter.to_ee(gdf)


class TestGeometryConverterProperties:
    """The converter exposes its configured projection read-only."""

    def test_properties_echo_the_config(self):
        """epsg / columns return the constructor's values."""
        converter = GeometryConverter(epsg=3857, columns=["a", "b"])
        assert converter.epsg == 3857
        assert converter.columns == ("a", "b")
