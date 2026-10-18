"""Tests for PointSampler and its parity with the legacy sample_points."""

from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import MultiPoint, Point, Polygon

ee = pytest.importorskip("ee")

from earthlens.gee.sampling import _REDUCER_WHITELIST  # noqa: E402
from earthlens.gee.sampling import point as point_module  # noqa: E402
from earthlens.gee.sampling.point import PointSampler  # noqa: E402


class _FakeFeatureCollection:
    """Stand-in for `ee.FeatureCollection` - records merges, ignores payload."""

    def __init__(self, payload=None):
        self.payload = payload
        self.merged_with: list = []

    def merge(self, other):
        self.merged_with.append(other)
        return self


class _FakeClipped:
    def __init__(self, recorder):
        self._recorder = recorder

    def reduceRegions(self, collection, reducer, scale):  # noqa: N802
        self._recorder["reduce_calls"].append(
            {"collection": collection, "reducer": reducer, "scale": scale}
        )
        return f"reduced<{scale}>"


class _FakeImage:
    def __init__(self):
        self.recorder = {"clip_bboxes": [], "reduce_calls": []}

    def clip(self, bbox):
        self.recorder["clip_bboxes"].append(bbox)
        return _FakeClipped(self.recorder)


class _FakeConverter:
    """Stand-in for GeometryConverter - returns a sentinel feature collection."""

    def to_ee(self, gdf):
        return ("fake_fc", tuple(gdf.index))


def _fake_reducer_namespace():
    """A namespace mimicking `ee.Reducer` for every whitelisted name."""
    return SimpleNamespace(
        **{name: lambda n=name: f"reducer<{n}>" for name in _REDUCER_WHITELIST}
    )


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the `ee` symbols and the converter PointSampler.sample touches."""
    monkeypatch.setattr(point_module.ee, "Reducer", _fake_reducer_namespace())
    monkeypatch.setattr(point_module.ee, "FeatureCollection", _FakeFeatureCollection)
    monkeypatch.setattr(
        point_module.ee,
        "Geometry",
        SimpleNamespace(BBox=lambda *args: ("bbox", args)),
    )
    monkeypatch.setattr(
        point_module, "GeometryConverter", lambda *a, **k: _FakeConverter()
    )


def _points_gdf(n: int) -> gpd.GeoDataFrame:
    pts = [Point(i * 0.1, i * 0.1) for i in range(n)]
    return gpd.GeoDataFrame({"id": list(range(n)), "geometry": pts}, crs="EPSG:4326")


class TestPointSampler:
    """The point-sampling strategy and its leaf-batching behaviour."""

    def test_empty_gdf_is_refused(self):
        """An empty frame is rejected before any Earth Engine call."""
        empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
        with pytest.raises(ValueError, match="non-empty GeoDataFrame"):
            PointSampler(scale_m=30).sample(_FakeImage(), empty)

    def test_a_polygon_row_is_refused(self, fake_ee):
        """A non-point geometry raises, naming the offending row."""
        gdf = gpd.GeoDataFrame(
            {"id": [0, 1]},
            geometry=[Point(0, 0), Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])],
            crs="EPSG:4326",
        )
        with pytest.raises(ValueError, match="row 1 is a Polygon"):
            PointSampler(scale_m=30).sample(_FakeImage(), gdf)

    def test_multipoint_is_accepted(self, fake_ee, monkeypatch):
        """A MultiPoint row explodes through the real converter and samples cleanly."""
        from earthlens.gee.vector import geometry as geometry_module
        from earthlens.gee.vector.geometry import GeometryConverter

        monkeypatch.setattr(point_module, "GeometryConverter", GeometryConverter)
        monkeypatch.setattr(
            geometry_module,
            "ee",
            SimpleNamespace(
                Geometry=SimpleNamespace(Point=lambda coords, proj: ("pt", coords)),
                Feature=lambda geom, props=None: ("feat", geom, props),
                FeatureCollection=lambda feats: ("fc", list(feats)),
            ),
        )
        gdf = gpd.GeoDataFrame(
            {"id": [0]}, geometry=[MultiPoint([(0, 0), (1, 1)])], crs="EPSG:4326"
        )
        PointSampler(scale_m=30).sample(_FakeImage(), gdf)

    def test_a_bad_reducer_is_refused_at_construction(self):
        """The reducer is validated when the sampler is built, not at sample."""
        with pytest.raises(ValidationError, match="unsupported reducer 'bogus'"):
            PointSampler(scale_m=30, reducer="bogus")

    def test_one_reduce_regions_per_leaf_with_the_scale_and_reducer(self, fake_ee):
        """Each rtree leaf yields a matched clip + reduceRegions at the given scale."""
        image = _FakeImage()
        result = PointSampler(scale_m=42).sample(image, _points_gdf(20))
        n_clips = len(image.recorder["clip_bboxes"])
        n_reduces = len(image.recorder["reduce_calls"])
        assert n_clips == n_reduces >= 1
        assert {call["scale"] for call in image.recorder["reduce_calls"]} == {42}
        assert {call["reducer"] for call in image.recorder["reduce_calls"]} == {
            "reducer<first>"
        }
        assert isinstance(result, _FakeFeatureCollection)

    def test_a_named_reducer_threads_to_every_call(self, fake_ee):
        """A non-default reducer resolves once and reaches every reduceRegions."""
        image = _FakeImage()
        PointSampler(scale_m=10, reducer="mean").sample(image, _points_gdf(5))
        assert {call["reducer"] for call in image.recorder["reduce_calls"]} == {
            "reducer<mean>"
        }

    def test_sample_to_gdf_composes_sample_and_downloader(self, fake_ee, monkeypatch):
        """sample_to_gdf routes the sampled collection through FeatureDownloader."""
        from earthlens.gee.vector import download as download_module

        seen = {}
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: seen.update(fc=fc, crs=crs) or "gdf",
        )
        out = PointSampler(scale_m=30).sample_to_gdf(
            _FakeImage(), _points_gdf(5), crs=3857
        )
        assert out == "gdf"
        assert seen["crs"] == 3857
        assert isinstance(seen["fc"], _FakeFeatureCollection)
