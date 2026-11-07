"""Tests for RegionTimeSeriesSampler (per-polygon AOI time-series across a stack)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

pytest.importorskip("ee")

from earthlens.gee.sampling import region as region_module  # noqa: E402
from earthlens.gee.sampling import timeseries as ts_module  # noqa: E402
from earthlens.gee.sampling.timeseries import RegionTimeSeriesSampler  # noqa: E402

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
_SQUARE_2 = Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])


class _FakeFeature:
    """A feature whose `set` records the stamped property."""

    def __init__(self):
        self.props: dict = {}

    def set(self, key, value):
        self.props[key] = value
        return self


class _FakeSampled:
    """A reduced collection whose `map` exercises the stamping function."""

    def __init__(self, image_tag, geometry=None):
        self.image_tag = image_tag
        self.geometry = geometry

    def map(self, fn):
        feature = _FakeFeature()
        fn(feature)
        return ("stamped", self.image_tag, feature.props.get("timestamp"))


class _FakeImage:
    """A per-bucket image carrying its `system:time_start`."""

    def __init__(self, time_start):
        self._time_start = time_start

    def get(self, key):
        return f"{key}={self._time_start}"


class _FakeMerged:
    """Stands in for `ee.FeatureCollection`, recording every merge."""

    def __init__(self):
        self.merged: list = []

    def merge(self, other):
        self.merged.append(other)
        return self


def _polys() -> gpd.GeoDataFrame:
    """A one-polygon frame in EPSG:4326."""
    return gpd.GeoDataFrame({"geometry": [_SQUARE]}, crs="EPSG:4326")


_SAMPLE_CALLS: list = []


def _recording_sample(self, image, geometry):
    """Record the geometry a region sampler was handed, for assertion."""
    _SAMPLE_CALLS.append(geometry)
    return _FakeSampled(image._time_start, geometry)


@pytest.fixture
def fake_sample(monkeypatch):
    """Patch the empty collection and the delegated RegionSampler.sample."""
    monkeypatch.setattr(
        ts_module.ee, "FeatureCollection", lambda payload=None: _FakeMerged()
    )
    monkeypatch.setattr(
        region_module.RegionSampler,
        "sample",
        lambda self, image, geometry: _FakeSampled(image._time_start, geometry),
    )


class TestClassVars:
    """The sampler declares the stack-consuming, tabular shape."""

    def test_is_tabular_and_consumes_the_stack(self):
        """Same ClassVars as TimeSeriesSampler, so the backend feeds it the stack."""
        assert RegionTimeSeriesSampler.output_kind == "tabular"
        assert RegionTimeSeriesSampler.consumes_stack is True

    def test_defaults_to_mean_and_long(self):
        """The default reduction is the mean, in the long table shape."""
        sampler = RegionTimeSeriesSampler(scale_m=5566)
        assert sampler.stats == ("mean",)
        assert sampler.wide is False


class TestRegionTimeSeriesSamplerSample:
    """Reducing the polygons over each bucket image."""

    def test_each_bucket_is_reduced_and_stamped(self, fake_sample):
        """One reduced, timestamp-stamped collection per bucket is merged."""
        images = [_FakeImage(100), _FakeImage(200)]
        merged = RegionTimeSeriesSampler(scale_m=5566).sample(images, _polys())
        assert isinstance(merged, _FakeMerged)
        assert len(merged.merged) == 2
        stamped = [entry[2] for entry in merged.merged]
        assert stamped == ["system:time_start=100", "system:time_start=200"]

    def test_a_region_id_is_injected_before_reducing(self, fake_sample, monkeypatch):
        """Each polygon carries a stable region_id so the series stays groupable."""
        _SAMPLE_CALLS.clear()
        monkeypatch.setattr(region_module.RegionSampler, "sample", _recording_sample)
        polys = gpd.GeoDataFrame({"geometry": [_SQUARE, _SQUARE_2]}, crs="EPSG:4326")
        RegionTimeSeriesSampler(scale_m=5566).sample([_FakeImage(100)], polys)
        assert list(_SAMPLE_CALLS[0]["region_id"]) == [0, 1]


class TestRegionTimeSeriesSamplerToGdf:
    """Pulling the series into a long or wide table."""

    def test_long_format_is_the_default(self, monkeypatch):
        """Without `wide`, the downloader's long table is returned unchanged."""
        from earthlens.gee.vector import download as download_module

        long_df = gpd.GeoDataFrame(
            {"region_id": [0], "timestamp": [100], "mean": [1.0]},
            geometry=[_SQUARE],
            crs="EPSG:4326",
        )
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: long_df,
        )
        monkeypatch.setattr(
            ts_module.RegionTimeSeriesSampler,
            "sample",
            lambda self, image, geometry: "fc",
        )
        out = RegionTimeSeriesSampler(scale_m=5566).sample_to_gdf([], _polys())
        assert out is long_df

    def test_wide_pivots_on_region_id_and_timestamp(self, monkeypatch):
        """With `wide`, the reduced value column pivots per timestamp per region."""
        from earthlens.gee.vector import download as download_module

        long_df = pd.DataFrame(
            {"region_id": [0, 0], "timestamp": [100, 200], "mean": [1.0, 2.0]}
        )
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: long_df,
        )
        monkeypatch.setattr(
            ts_module.RegionTimeSeriesSampler,
            "sample",
            lambda self, image, geometry: "fc",
        )
        out = RegionTimeSeriesSampler(scale_m=5566, wide=True).sample_to_gdf(
            [], _polys()
        )
        assert out.loc[0, ("mean", 100)] == 1.0
        assert out.loc[0, ("mean", 200)] == 2.0


def test_a_preexisting_region_id_column_is_refused():
    """A caller-supplied region_id would be clobbered, so sample() refuses it."""
    sampler = RegionTimeSeriesSampler(scale_m=10)
    geometry = gpd.GeoDataFrame(
        {"geometry": [_SQUARE], "region_id": [99]}, crs="EPSG:4326"
    )
    images = [_FakeImage(1)]
    with pytest.raises(ValueError, match="already has a 'region_id'"):
        sampler.sample(images, geometry)
