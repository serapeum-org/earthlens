"""Tests for TransectSampler (line-transect sampling, server + client paths)."""

from __future__ import annotations

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, Point

pytest.importorskip("ee")

from earthlens.gee.sampling import point as point_module  # noqa: E402
from earthlens.gee.sampling.transect import TransectSampler  # noqa: E402


def _line_gdf() -> gpd.GeoDataFrame:
    """A one-row frame holding a unit vertical line in EPSG:4326."""
    return gpd.GeoDataFrame(
        {"geometry": [LineString([(0.0, 0.0), (0.0, 1.0)])]}, crs="EPSG:4326"
    )


class _FakeDataset:
    """Records the `transect` call in place of a pyramids `Dataset`."""

    def __init__(self):
        self.calls: list = []

    def transect(self, line, *, n, band):
        self.calls.append({"line": line, "n": n, "band": band})
        return ("transect_fc", n, band)


class TestTransectSamplerServer:
    """The server densify-and-reduce path."""

    def test_empty_gdf_is_refused(self):
        """An empty frame is rejected before densifying."""
        empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
        sampler = TransectSampler(scale_m=30)
        with pytest.raises(ValueError, match="non-empty GeoDataFrame"):
            sampler.sample(object(), empty)

    def test_a_point_row_is_refused(self):
        """A non-line geometry raises, naming the offending row."""
        gdf = gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs="EPSG:4326")
        sampler = TransectSampler(scale_m=30)
        with pytest.raises(ValueError, match="row 0 is a Point"):
            sampler.sample(object(), gdf)

    def test_it_densifies_into_ordered_points_with_distance(self, monkeypatch):
        """The line becomes n_points ordered points carrying a dist_m column."""
        captured: dict = {}
        monkeypatch.setattr(
            point_module.PointSampler,
            "sample",
            lambda self, image, gdf: captured.update(image=image, gdf=gdf) or "fc",
        )
        image = object()
        out = TransectSampler(scale_m=30, n_points=5).sample(image, _line_gdf())
        assert out == "fc"
        points = captured["gdf"]
        assert len(points) == 5
        assert list(points["dist_m"]) == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
        assert all(geom.geom_type == "Point" for geom in points.geometry)
        assert points.crs == _line_gdf().crs
        assert captured["image"] is image

    def test_the_point_sampler_carries_scale_and_reducer(self, monkeypatch):
        """The delegated PointSampler is built with this sampler's scale + reducer."""
        seen: dict = {}
        monkeypatch.setattr(
            point_module.PointSampler,
            "sample",
            lambda self, image, gdf: (
                seen.update(scale=self.scale_m, reducer=self.reducer.name) or "fc"
            ),
        )
        TransectSampler(scale_m=42, reducer="mean", n_points=3).sample(
            object(), _line_gdf()
        )
        assert (seen["scale"], seen["reducer"]) == (42.0, "mean")


class TestTransectSamplerClient:
    """The client `Dataset.transect` path."""

    def test_sample_to_gdf_calls_dataset_transect(self):
        """sample_to_gdf forwards the line, n_points and band to Dataset.transect."""
        dataset = _FakeDataset()
        out = TransectSampler(scale_m=30, n_points=12, band=2).sample_to_gdf(
            dataset, _line_gdf()
        )
        assert out == ("transect_fc", 12, 2)
        assert dataset.calls[0]["n"] == 12
        assert dataset.calls[0]["band"] == 2
        assert dataset.calls[0]["line"].geom_type == "LineString"


class TestPositiveNPoints:
    """A transect needs a positive point count."""

    def test_n_points_must_be_positive(self):
        """TransectSampler rejects a non-positive n_points."""
        with pytest.raises(ValidationError):
            TransectSampler(scale_m=30, n_points=0)
