"""Tests for RegionSampler, zonal statistics, and the zonal_stats helper."""

from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from earthlens.gee.sampling import region as region_module
from earthlens.gee.sampling.region import RegionSampler, ZonalStrategy, zonal_stats

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


class _FakeReducer:
    """Records combine() chaining in place of an ee.Reducer."""

    def __init__(self, name):
        self.name = name
        self.combined = []

    def combine(self, reducer2, sharedInputs):  # noqa: N803 - mirrors the ee kwarg
        self.combined.append((reducer2.name, sharedInputs))
        return self


class _FakeImage:
    """Records the reduceRegions call in place of an ee.Image."""

    def __init__(self):
        self.calls = []

    def reduceRegions(self, collection, reducer, scale, tileScale):  # noqa: N802,N803
        self.calls.append(
            {
                "collection": collection,
                "reducer": reducer,
                "scale": scale,
                "tileScale": tileScale,
            }
        )
        return f"reduced<{scale}>"


class _FakeConverter:
    """Returns a sentinel for to_ee in place of GeometryConverter."""

    def to_ee(self, gdf):
        return ("fc", len(gdf))


class _FakeDataset:
    """A pyramids Dataset stand-in exposing zonal_stats."""

    def __init__(self):
        self.calls = []

    def zonal_stats(self, features, stats, band):
        self.calls.append({"stats": stats, "band": band})
        return "client-result"


@pytest.fixture
def fake_server(monkeypatch):
    """Patch the resolve_reducer and GeometryConverter the server path uses."""
    monkeypatch.setattr(
        region_module,
        "resolve_reducer",
        lambda name: SimpleNamespace(as_ee_reducer=lambda: _FakeReducer(name)),
    )
    monkeypatch.setattr(
        region_module, "GeometryConverter", lambda *a, **k: _FakeConverter()
    )


def _polygons(n: int = 1) -> gpd.GeoDataFrame:
    """A GeoDataFrame of `n` unit squares."""
    return gpd.GeoDataFrame(
        {"id": list(range(n)), "geometry": [_SQUARE] * n}, crs="EPSG:4326"
    )


class TestRegionSamplerConstruction:
    """The frozen configuration and its validators."""

    def test_defaults(self):
        """A server sampler defaults to mean over tile_scale 1.0."""
        sampler = RegionSampler(scale_m=30)
        assert (sampler.strategy, sampler.stats, sampler.tile_scale) == (
            ZonalStrategy.SERVER,
            ("mean",),
            1.0,
        )

    def test_empty_stats_is_refused(self):
        """stats must name at least one reducer."""
        with pytest.raises(ValidationError, match="at least one reducer"):
            RegionSampler(scale_m=30, stats=())

    def test_tile_scale_out_of_range_is_refused(self):
        """tile_scale outside [0.1, 16] is refused at construction."""
        with pytest.raises(ValidationError, match="within"):
            RegionSampler(scale_m=30, tile_scale=99)

    def test_tile_scale_in_range_is_kept(self):
        """A valid tile_scale passes through."""
        assert RegionSampler(scale_m=30, tile_scale=4.0).tile_scale == 4.0


class TestServerStrategy:
    """sample reduces each polygon inside Earth Engine."""

    def test_single_stat_builds_one_reducer(self, fake_server):
        """One stat makes a reducer with nothing combined onto it."""
        image = _FakeImage()
        RegionSampler(scale_m=30, stats=("mean",)).sample(image, _polygons())
        reducer = image.calls[0]["reducer"]
        assert reducer.name == "mean"
        assert reducer.combined == []
        assert image.calls[0]["scale"] == 30
        assert image.calls[0]["tileScale"] == 1.0

    def test_multiple_stats_combine_with_shared_inputs(self, fake_server):
        """Extra stats are combined onto the first with sharedInputs=True."""
        image = _FakeImage()
        RegionSampler(scale_m=30, stats=("mean", "max", "min"), tile_scale=2.0).sample(
            image, _polygons()
        )
        reducer = image.calls[0]["reducer"]
        assert reducer.name == "mean"
        assert reducer.combined == [("max", True), ("min", True)]
        assert image.calls[0]["tileScale"] == 2.0

    def test_empty_geometry_is_refused(self, fake_server):
        """An empty frame is rejected before any reduction."""
        empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
        sampler = RegionSampler(scale_m=30)
        image = _FakeImage()
        with pytest.raises(ValueError, match="non-empty"):
            sampler.sample(image, empty)

    def test_a_point_row_is_refused(self, fake_server):
        """A non-polygon geometry raises, naming the row."""
        gdf = gpd.GeoDataFrame({"id": [0]}, geometry=[Point(0, 0)], crs="EPSG:4326")
        sampler = RegionSampler(scale_m=30)
        image = _FakeImage()
        with pytest.raises(ValueError, match="row 0 is a Point"):
            sampler.sample(image, gdf)


class TestSampleToGdf:
    """The two strategies' client-facing table output."""

    def test_server_routes_through_the_downloader(self, fake_server, monkeypatch):
        """The server strategy pulls sample() through FeatureDownloader."""
        from earthlens.gee.vector import download as download_module

        seen = {}
        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: seen.update(fc=fc, crs=crs) or "server-result",
        )
        out = RegionSampler(scale_m=30).sample_to_gdf(
            _FakeImage(), _polygons(), crs=3857
        )
        assert out == "server-result"
        assert seen["crs"] == 3857

    def test_client_reduces_locally(self, monkeypatch):
        """The client strategy reduces with the dataset's zonal_stats, no EE."""
        monkeypatch.setattr("pyramids.feature.FeatureCollection", lambda gdf: gdf)
        dataset = _FakeDataset()
        out = RegionSampler(
            scale_m=30, strategy=ZonalStrategy.CLIENT, stats=("mean", "max")
        ).sample_to_gdf(None, _polygons(), dataset)
        assert out == "client-result"
        assert dataset.calls == [{"stats": ["mean", "max"], "band": 0}]

    def test_client_without_dataset_is_refused(self):
        """The client strategy needs an in-memory dataset."""
        sampler = RegionSampler(scale_m=30, strategy=ZonalStrategy.CLIENT)
        polygons = _polygons()
        with pytest.raises(ValueError, match="in-memory pyramids Dataset"):
            sampler.sample_to_gdf(None, polygons)


class TestZonalStatsHelper:
    """The free zonal_stats mirrors sample_points' shape."""

    def test_server_helper(self, fake_server, monkeypatch):
        """zonal_stats server path builds a sampler and downloads."""
        from earthlens.gee.vector import download as download_module

        monkeypatch.setattr(
            download_module.FeatureDownloader,
            "to_gdf",
            lambda self, fc, crs=4326: "helper-result",
        )
        assert (
            zonal_stats(_FakeImage(), _polygons(), scale_m=30, stats=("mean",))
            == "helper-result"
        )

    def test_client_helper(self, monkeypatch):
        """zonal_stats client path reduces with the dataset."""
        monkeypatch.setattr("pyramids.feature.FeatureCollection", lambda gdf: gdf)
        dataset = _FakeDataset()
        assert (
            zonal_stats(
                None, _polygons(), scale_m=30, strategy="client", dataset=dataset
            )
            == "client-result"
        )
