"""Tests for the VectorCodec file <-> ee bridge."""

from __future__ import annotations

import geopandas as gpd
import pytest
from pyramids.feature import FeatureCollection
from shapely.geometry import Point

from earthlens.gee.vector import codec as codec_module
from earthlens.gee.vector.codec import VectorCodec
from earthlens.gee.vector.download import FeatureDownloader
from earthlens.gee.vector.geometry import GeometryConverter


class _RecordingConverter:
    """Stands in for `GeometryConverter`, recording the collection it converts."""

    def __init__(self):
        self.seen: FeatureCollection | None = None

    def to_ee(self, gdf):
        self.seen = gdf
        return "EE_FC"


class _RecordingDownloader:
    """Stands in for `FeatureDownloader`, returning a fixed collection."""

    def __init__(self, result):
        self._result = result
        self.seen = None

    def to_gdf(self, fc):
        self.seen = fc
        return self._result


def _points() -> FeatureCollection:
    """A two-point collection with a `name` property, in EPSG:4326."""
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(31.0, 30.0), Point(31.1, 30.1)],
        crs=4326,
    )
    return FeatureCollection(gdf)


class TestConstruction:
    """The codec defaults its collaborators and exposes them read-only."""

    def test_defaults_are_plain_instances(self):
        """With no arguments the codec builds a plain converter and downloader."""
        codec = VectorCodec()
        assert isinstance(codec.converter, GeometryConverter)
        assert isinstance(codec.downloader, FeatureDownloader)

    def test_injected_collaborators_are_kept(self):
        """Passed-in converter and downloader are the ones used."""
        converter = _RecordingConverter()
        downloader = _RecordingDownloader(_points())
        codec = VectorCodec(converter=converter, downloader=downloader)
        assert codec.converter is converter
        assert codec.downloader is downloader


class TestFromFile:
    """Reading a vector file routes through pyramids then the converter."""

    def test_from_file_reads_then_converts(self, tmp_path):
        """A written geojson reads back and its features reach `to_ee`."""
        _points().to_file(tmp_path / "pts.geojson")
        converter = _RecordingConverter()
        codec = VectorCodec(converter=converter)
        result = codec.from_file(tmp_path / "pts.geojson")
        assert result == "EE_FC"
        assert list(converter.seen["name"]) == ["a", "b"]

    def test_from_csv_builds_points_from_xy(self, tmp_path):
        """A CSV of coordinates becomes point features before conversion."""
        (tmp_path / "pts.csv").write_text(
            "longitude,latitude,name\n31.0,30.0,a\n31.1,30.1,b\n", encoding="utf-8"
        )
        converter = _RecordingConverter()
        codec = VectorCodec(converter=converter)
        codec.from_csv(tmp_path / "pts.csv")
        assert list(converter.seen["name"]) == ["a", "b"]
        assert converter.seen.geometry.iloc[0].x == pytest.approx(31.0)


class TestToFile:
    """Writing an ee collection pulls it back then lets pyramids write it."""

    def test_to_file_round_trips_geometry_and_properties(self, tmp_path):
        """`to_file` then `from_file` preserves geometries and properties."""
        downloader = _RecordingDownloader(_points())
        codec = VectorCodec(downloader=downloader)
        codec.to_file("EE_FC", tmp_path / "out.geojson")
        assert downloader.seen == "EE_FC"
        back = FeatureCollection.from_file(tmp_path / "out.geojson")
        assert list(back["name"]) == ["a", "b"]
        assert back.geometry.iloc[1].y == pytest.approx(30.1)


def test_no_banned_io_libraries_are_imported():
    """The codec stays on pyramids: no rasterio / fiona / xarray / xee."""
    source = codec_module.__file__
    text = open(source, encoding="utf-8").read()
    for banned in ("import rasterio", "import fiona", "import xarray", "import xee"):
        assert banned not in text
