"""Tests for the Sampler abstract base class."""

from __future__ import annotations

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from earthlens.gee.reducers import Reducer, resolve_reducer
from earthlens.gee.sampling.base import Sampler

_POINTS = frozenset({"Point", "MultiPoint"})


class _ConcreteSampler(Sampler):
    """A minimal concrete sampler for exercising the base contract."""

    def sample(self, image, geometry):
        """Return a sentinel; the base contract is what is under test."""
        return "sampled"


class TestSampler:
    """The shared base: reducer coercion, immutability, geometry guard."""

    def test_the_abc_cannot_be_instantiated(self):
        """`sample` is abstract, so the base itself has no instances."""
        with pytest.raises(TypeError, match="abstract"):
            Sampler(scale_m=30)

    def test_a_string_reducer_resolves_through_the_registry(self):
        """A registry key becomes the matching `Reducer`."""
        sampler = _ConcreteSampler(scale_m=30, reducer="mean")
        assert isinstance(sampler.reducer, Reducer)
        assert sampler.reducer.name == "mean"

    def test_the_default_reducer_is_first(self):
        """Omitting the reducer yields the registry's `first`."""
        assert _ConcreteSampler(scale_m=30).reducer.name == "first"

    def test_a_reducer_instance_passes_through(self):
        """An already-built `Reducer` is accepted unchanged."""
        reducer = resolve_reducer("median")
        assert _ConcreteSampler(scale_m=10, reducer=reducer).reducer is reducer

    def test_a_bad_reducer_name_fails_closed_at_construction(self):
        """An unknown reducer raises at construction, not at sample time."""
        with pytest.raises(ValidationError, match="unsupported reducer 'bogus'"):
            _ConcreteSampler(scale_m=30, reducer="bogus")

    def test_it_is_frozen(self):
        """A sampler is an immutable value object."""
        sampler = _ConcreteSampler(scale_m=30)
        with pytest.raises(ValidationError, match="frozen"):
            sampler.scale_m = 10

    def test_an_unexpected_field_is_refused(self):
        """`extra='forbid'` rejects a field that is not the sampler's."""
        with pytest.raises(ValidationError):
            _ConcreteSampler(scale_m=30, bogus=1)


class TestValidateGeometry:
    """The shared geometry guard."""

    def test_an_empty_frame_is_refused(self):
        """Sampling needs at least one geometry."""
        empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
        with pytest.raises(ValueError, match="non-empty GeoDataFrame"):
            Sampler._validate_geometry(empty, _POINTS)

    def test_a_disallowed_geometry_names_the_row(self):
        """A geometry outside the allowed set names the first offending row."""
        gdf = gpd.GeoDataFrame(
            {"geometry": [Point(0, 0), Polygon([(0, 0), (1, 0), (1, 1)])]},
            crs="EPSG:4326",
        )
        with pytest.raises(ValueError, match="row 1 is a Polygon"):
            Sampler._validate_geometry(gdf, _POINTS)

    def test_an_allowed_frame_passes(self):
        """A frame of allowed geometries raises nothing."""
        gdf = gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs="EPSG:4326")
        assert Sampler._validate_geometry(gdf, _POINTS) is None
