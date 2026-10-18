"""Tests for TimeSeriesSampler (per-point time-series across a stack)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

pytest.importorskip("ee")

from earthlens.gee.sampling import point as point_module  # noqa: E402
from earthlens.gee.sampling import timeseries as ts_module  # noqa: E402
from earthlens.gee.sampling.timeseries import TimeSeriesSampler  # noqa: E402


class _FakeFeature:
    """A feature whose `set` records the stamped property."""

    def __init__(self):
        self.props: dict = {}

    def set(self, key, value):
        self.props[key] = value
        return self


class _FakeSampled:
    """A sampled collection whose `map` exercises the stamping function."""

    def __init__(self, image_tag):
        self.image_tag = image_tag

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


def _points() -> gpd.GeoDataFrame:
    """A one-point frame in EPSG:4326."""
    return gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs="EPSG:4326")


@pytest.fixture
def fake_sample(monkeypatch):
    """Patch the empty collection and the delegated PointSampler.sample."""
    monkeypatch.setattr(
        ts_module.ee, "FeatureCollection", lambda payload=None: _FakeMerged()
    )
    monkeypatch.setattr(
        point_module.PointSampler,
        "sample",
        lambda self, image, geometry: _FakeSampled(image._time_start),
    )


class TestTimeSeriesSamplerSample:
    """Mapping the point sampler over each bucket image."""

    def test_each_bucket_is_sampled_and_stamped(self, fake_sample):
        """One sampled, timestamp-stamped collection per bucket is merged."""
        images = [_FakeImage(100), _FakeImage(200)]
        merged = TimeSeriesSampler(scale_m=30).sample(images, _points())
        assert isinstance(merged, _FakeMerged)
        assert len(merged.merged) == 2
        stamped_timestamps = [entry[2] for entry in merged.merged]
        assert stamped_timestamps == [
            "system:time_start=100",
            "system:time_start=200",
        ]


class TestTimeSeriesSamplerToGdf:
    """Pulling the series into a long or wide table."""

    def test_long_format_is_the_default(self, monkeypatch):
        """Without `wide`, the downloader's long table is returned unchanged."""
        from earthlens.gee.vector import download as download_module

        long_df = gpd.GeoDataFrame(
            {"point_id": [0], "timestamp": [100], "value": [1.0]},
            geometry=[Point(0, 0)],
            crs="EPSG:4326",
        )
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: long_df,
        )
        monkeypatch.setattr(
            ts_module.TimeSeriesSampler, "sample", lambda self, image, geometry: "fc"
        )
        out = TimeSeriesSampler(scale_m=30).sample_to_gdf([], _points())
        assert out is long_df

    def test_wide_pivots_on_timestamp(self, monkeypatch):
        """With `wide`, the long table pivots to one column per timestamp."""
        from earthlens.gee.vector import download as download_module

        long_df = pd.DataFrame(
            {"point_id": [0, 0], "timestamp": [100, 200], "value": [1.0, 2.0]}
        )
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: long_df,
        )
        monkeypatch.setattr(
            ts_module.TimeSeriesSampler, "sample", lambda self, image, geometry: "fc"
        )
        out = TimeSeriesSampler(scale_m=30, wide=True).sample_to_gdf([], _points())
        assert list(out.columns) == [100, 200]
        assert out.loc[0, 100] == 1.0
        assert out.loc[0, 200] == 2.0
