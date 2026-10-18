"""Tests for RandomSampler and StratifiedSampler training-point generation."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from earthlens.gee.sampling import random as random_module
from earthlens.gee.sampling.random import RandomSampler, StratifiedSampler

_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


class _FakeImage:
    """Records sample / stratifiedSample calls in place of an ee.Image."""

    def __init__(self):
        self.sample_kwargs = None
        self.stratified_kwargs = None

    def sample(self, **kwargs):
        self.sample_kwargs = kwargs
        return "random-fc"

    def stratifiedSample(self, **kwargs):  # noqa: N802 - mirrors the ee method
        self.stratified_kwargs = kwargs
        return "stratified-fc"


class _FakeConverter:
    """Returns a sentinel region in place of GeometryConverter."""

    def to_ee_geometry(self, geom):
        return ("region", geom.geom_type)


@pytest.fixture
def fake_converter(monkeypatch):
    """Patch the GeometryConverter the random samplers use to bound the draw."""
    monkeypatch.setattr(
        random_module, "GeometryConverter", lambda *a, **k: _FakeConverter()
    )


def _region() -> gpd.GeoDataFrame:
    """A one-row polygon frame bounding the draw."""
    return gpd.GeoDataFrame({"id": [0]}, geometry=[_SQUARE], crs="EPSG:4326")


class TestRandomSampler:
    """Uniform random draws over a bounding region."""

    def test_sample_passes_the_budget_and_seed(self, fake_converter):
        """numPixels / seed / geometries reach ee.Image.sample with the region."""
        image = _FakeImage()
        result = RandomSampler(scale_m=30, n_points=500, seed=42).sample(
            image, _region()
        )
        assert result == "random-fc"
        assert image.sample_kwargs == {
            "region": ("region", "Polygon"),
            "scale": 30,
            "numPixels": 500,
            "seed": 42,
            "geometries": True,
        }

    def test_a_non_polygon_region_is_refused(self, fake_converter):
        """The bounding region must be a polygon."""
        gdf = gpd.GeoDataFrame({"id": [0]}, geometry=[Point(0, 0)], crs="EPSG:4326")
        with pytest.raises(ValueError, match="row 0 is a Point"):
            RandomSampler(scale_m=30, n_points=10).sample(_FakeImage(), gdf)


class TestStratifiedSampler:
    """Per-class draws from a class/label band."""

    def test_scalar_count_goes_through_num_points(self, fake_converter):
        """A scalar n_per_class is passed as numPoints with no per-class lists."""
        image = _FakeImage()
        StratifiedSampler(scale_m=30, class_band="lc", n_per_class=200).sample(
            image, _region()
        )
        assert image.stratified_kwargs["numPoints"] == 200
        assert image.stratified_kwargs["classValues"] is None
        assert image.stratified_kwargs["classPoints"] is None
        assert image.stratified_kwargs["classBand"] == "lc"

    def test_scalar_count_with_named_class_values(self, fake_converter):
        """A scalar count with named class_values pairs classValues/classPoints."""
        image = _FakeImage()
        StratifiedSampler(
            scale_m=30, class_band="lc", n_per_class=50, class_values=(1, 2, 3)
        ).sample(image, _region())
        assert image.stratified_kwargs["classValues"] == [1, 2, 3]
        assert image.stratified_kwargs["classPoints"] == [50, 50, 50]
        assert image.stratified_kwargs["numPoints"] == 0

    def test_mapping_splits_into_parallel_lists(self, fake_converter):
        """A {class: count} mapping becomes parallel classValues / classPoints."""
        image = _FakeImage()
        StratifiedSampler(
            scale_m=30, class_band="lc", n_per_class={1: 100, 2: 50}
        ).sample(image, _region())
        assert image.stratified_kwargs["classValues"] == [1, 2]
        assert image.stratified_kwargs["classPoints"] == [100, 50]
        assert image.stratified_kwargs["numPoints"] == 0

    def test_a_non_polygon_region_is_refused(self, fake_converter):
        """The bounding region must be a polygon."""
        gdf = gpd.GeoDataFrame({"id": [0]}, geometry=[Point(0, 0)], crs="EPSG:4326")
        with pytest.raises(ValueError, match="row 0 is a Point"):
            StratifiedSampler(scale_m=30, class_band="lc", n_per_class=10).sample(
                _FakeImage(), gdf
            )
