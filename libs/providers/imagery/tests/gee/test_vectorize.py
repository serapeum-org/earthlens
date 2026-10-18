"""Tests for Vectorize, the raster->polygons converter (server + client)."""

from __future__ import annotations

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon

from earthlens.gee.vector import vectorize as vectorize_module
from earthlens.gee.vector.vectorize import SYNC_FETCH_LIMIT, Vectorize

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


class _FakeImage:
    """Records the reduceToVectors options in place of an ee.Image."""

    def __init__(self):
        self.options: dict | None = None

    def reduceToVectors(self, **options):  # noqa: N802 - mirrors the ee method
        self.options = options
        return ("fc", options["geometryType"])


class _FakeDataset:
    """A pyramids Dataset stand-in whose to_polygons yields a GeoDataFrame."""

    def __init__(self):
        self.band: int | None = None

    def to_polygons(self, band):
        self.band = band
        return gpd.GeoDataFrame({"label": [1], "geometry": [_SQUARE]}, crs=4326)


class TestConstruction:
    """The frozen configuration a Vectorize carries."""

    def test_defaults(self):
        """It defaults to eight-connected polygons labelled `label`."""
        vec = Vectorize(scale_m=30)
        assert (vec.geometry_type, vec.eight_connected, vec.label_property) == (
            "polygon",
            True,
            "label",
        )

    def test_the_sync_limit_is_exposed(self):
        """The getInfo ceiling is a module constant callers can check against."""
        assert SYNC_FETCH_LIMIT == 5000


class TestToEe:
    """Server-side reduceToVectors."""

    def test_options_are_forwarded(self):
        """geometry_type / connectedness / label / scale / max_pixels are passed."""
        vec = Vectorize(scale_m=30, geometry_type="centroid", eight_connected=False)
        image = _FakeImage()
        result = vec.to_ee(image)
        assert result == ("fc", "centroid")
        assert image.options["eightConnected"] is False
        assert image.options["labelProperty"] == "label"
        assert image.options["scale"] == 30
        assert "reducer" not in image.options

    def test_a_reducer_is_attached_when_named(self, monkeypatch):
        """A reducer name resolves through the registry and rides along."""
        monkeypatch.setattr(
            vectorize_module,
            "resolve_reducer",
            lambda name: type("R", (), {"as_ee_reducer": lambda self: f"ee<{name}>"})(),
        )
        vec = Vectorize(scale_m=30, reducer="mean")
        image = _FakeImage()
        vec.to_ee(image)
        assert image.options["reducer"] == "ee<mean>"


class TestToGdf:
    """Server-side vectorise then download."""

    def test_it_downloads_the_server_result(self, monkeypatch):
        """to_gdf passes the reduceToVectors collection through the downloader."""
        from earthlens.gee.vector import download as download_module

        class _FakeDownloader:
            def to_gdf(self, fc, crs):
                return ("gdf", fc, crs)

        monkeypatch.setattr(download_module, "FeatureDownloader", _FakeDownloader)
        vec = Vectorize(scale_m=30)
        result = vec.to_gdf(_FakeImage(), crs=3857)
        assert result == ("gdf", ("fc", "polygon"), 3857)


class TestToPolygons:
    """Client-side vectorise via pyramids Dataset.to_polygons."""

    def test_it_polygonises_the_dataset(self):
        """to_polygons reads the chosen band and wraps the result."""
        from pyramids.feature import FeatureCollection

        dataset = _FakeDataset()
        result = Vectorize(scale_m=30).to_polygons(dataset, band=1)
        assert dataset.band == 1
        assert isinstance(result, FeatureCollection)


class TestConfig:
    """Config validation."""

    def test_an_unknown_option_is_refused(self):
        """extra='forbid' rejects a mis-spelled option."""
        with pytest.raises(ValidationError):
            Vectorize(scale_m=30, connected=True)
