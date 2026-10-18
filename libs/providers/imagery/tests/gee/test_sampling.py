"""Tests for the `sample_points` / `sample_points_to_gdf` back-compat shims."""

from __future__ import annotations

import inspect

import geopandas as gpd
import pytest
from shapely.geometry import Point

from earthlens.gee import sampling
from earthlens.gee.sampling import sample_points, sample_points_to_gdf

#: The `gee` extra is optional, so a missing `earthengine-api` skips this
#: module rather than erroring the whole directory's collection.
ee = pytest.importorskip("ee")


def _points_gdf(n: int) -> gpd.GeoDataFrame:
    """An `n`-point GeoDataFrame at the shared origin."""
    pts = [Point(i * 0.1, i * 0.1) for i in range(n)]
    return gpd.GeoDataFrame({"id": list(range(n)), "geometry": pts}, crs="EPSG:4326")


class _FakePointSampler:
    """Records its construction and call in place of the real `PointSampler`."""

    last: dict = {}

    def __init__(self, *, scale_m, reducer):
        _FakePointSampler.last = {"scale_m": scale_m, "reducer": reducer}

    def sample(self, image, gdf):  # noqa: D102 - fake
        _FakePointSampler.last["sample"] = (image, id(gdf))
        return "fc"

    def sample_to_gdf(self, image, gdf, *, crs=4326):  # noqa: D102 - fake
        _FakePointSampler.last["sample_to_gdf"] = (image, id(gdf), crs)
        return "gdf"


class TestShimDelegation:
    """The shims build a `PointSampler` and return its result."""

    def test_sample_points_forwards_to_point_sampler(self, monkeypatch):
        """It builds `PointSampler(scale_m, reducer)` and returns its `sample`."""
        monkeypatch.setattr(sampling, "PointSampler", _FakePointSampler)
        result = sample_points(object(), _points_gdf(3), scale_m=42, reducer="mean")
        assert result == "fc"
        assert _FakePointSampler.last["scale_m"] == 42
        # The string is resolved to a `Reducer` before it reaches the sampler.
        assert _FakePointSampler.last["reducer"].name == "mean"

    def test_sample_points_to_gdf_forwards_crs(self, monkeypatch):
        """It delegates to `PointSampler.sample_to_gdf`, threading `crs`."""
        monkeypatch.setattr(sampling, "PointSampler", _FakePointSampler)
        result = sample_points_to_gdf(
            object(), _points_gdf(2), scale_m=10, reducer="first", crs=3857
        )
        assert result == "gdf"
        assert _FakePointSampler.last["sample_to_gdf"][2] == 3857


class TestShimReducerContract:
    """The shim preserves the historical sampling-reducer contract."""

    def test_no_eval_in_module(self):
        """Anti-regression: the sampling package body never uses `eval()`."""
        assert "eval(" not in inspect.getsource(sampling)

    def test_mosaic_is_refused(self):
        """`mosaic` has no `ee.Reducer`, so the strict default refuses it up front."""
        with pytest.raises(ValueError, match="mosaic"):
            sample_points(object(), _points_gdf(1), scale_m=30, reducer="mosaic")

    def test_unknown_reducer_is_refused(self):
        """An unknown reducer name is refused at construction."""
        with pytest.raises(ValueError, match="unsupported reducer"):
            sample_points(object(), _points_gdf(1), scale_m=30, reducer="bogus")

    def test_empty_gdf_is_refused(self):
        """An empty GeoDataFrame is rejected before any Earth Engine call."""
        empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
        with pytest.raises(ValueError, match="non-empty"):
            sample_points(object(), empty, scale_m=30)
