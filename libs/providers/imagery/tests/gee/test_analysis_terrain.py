"""Tests for the terrain derivative operations."""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.terrain import Aspect, Hillshade, Slope

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _elevation(values: np.ndarray) -> Dataset:
    """Build a one-band elevation raster from `values`."""
    return Dataset.from_array(arr=values.astype("float32"), geo_ref=GEO)


def _flat(fill: float = 100.0) -> Dataset:
    """A surface with no relief at all."""
    return _elevation(np.full((6, 6), fill))


def _forty_five_degree_ramp() -> Dataset:
    """A ramp rising one metre per metre, in a metric CRS: exactly 45 degrees."""
    metric = GeoReference(top_left_corner=(0.0, 0.0), cell_size=1.0, epsg=3857)
    values = np.tile(np.arange(5, dtype="float32"), (5, 1))
    return Dataset.from_array(arr=values, geo_ref=metric)


def _east_facing_ramp() -> Dataset:
    """A surface descending to the east, so aspect and slope are determinate."""
    return _elevation(np.tile(np.arange(6, 0, -1, dtype="float32"), (6, 1)))


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as float64."""
    return np.asarray(ds.read_array(), dtype="float64")


class TestSlope:
    """Steepness, and the units it is reported in."""

    def test_a_flat_surface_has_zero_slope(self):
        """No relief means no steepness anywhere."""
        assert _values(Slope().apply(_flat())).max() == pytest.approx(0.0)

    def test_a_ramp_has_positive_slope(self):
        """A descending surface is not flat."""
        interior = _values(Slope().apply(_east_facing_ramp()))[1:-1, 1:-1]
        assert interior.min() > 0.0

    @pytest.mark.parametrize(
        ("units", "expected"),
        [("degrees", 45.0), ("radians", math.pi / 4), ("percent", 100.0)],
        ids=["degrees", "radians", "percent"],
    )
    def test_each_unit_reports_the_value_that_unit_means(self, units, expected):
        """A 45-degree slope is 45 degrees, pi/4 radians and 100 percent.

        Asserting the number rather than "differs from degrees" is the point:
        pyramids falls through to radians for any unit string it does not know,
        so a looser assertion certifies whatever comes back.
        """
        result = _values(Slope(units=units).apply(_forty_five_degree_ramp()))
        assert result[2, 2] == pytest.approx(expected, rel=1e-6)

    def test_an_unknown_unit_is_rejected_at_construction(self):
        """The literal is checked when the operation is built, not when it runs."""
        with pytest.raises(ValidationError, match="units"):
            Slope(units="grads")

    def test_the_output_keeps_the_source_grid(self):
        """A derivative stays georeferenced like the elevation it came from."""
        source = _flat()
        result = Slope().apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )


class TestAspect:
    """Downslope direction."""

    def test_a_ramp_has_a_consistent_aspect(self):
        """One planar surface faces one way, so the interior agrees."""
        interior = _values(Aspect().apply(_east_facing_ramp()))[1:-1, 1:-1]
        assert np.ptp(interior) == pytest.approx(0.0, abs=1e-6)

    def test_the_output_is_one_band(self):
        """Aspect is a single derived band, not a copy of the input."""
        assert Aspect().apply(_flat()).band_count == 1


class TestHillshade:
    """Shaded relief and the sun position it is computed for."""

    def test_the_sun_position_changes_the_result(self):
        """`azimuth` reaches pyramids; two light directions differ."""
        ramp = _east_facing_ramp()
        north_west = _values(Hillshade(azimuth=315.0).apply(ramp))[1:-1, 1:-1]
        south_east = _values(Hillshade(azimuth=135.0).apply(ramp))[1:-1, 1:-1]
        assert not np.allclose(north_west, south_east)

    def test_the_defaults_are_the_cartographic_convention(self):
        """North-west at 45 degrees is what makes relief read as terrain."""
        operation = Hillshade()
        assert (operation.azimuth, operation.altitude) == (315.0, 45.0)

    def test_a_flat_surface_shades_uniformly(self):
        """With no relief there is nothing to shade differently."""
        interior = _values(Hillshade().apply(_flat()))[1:-1, 1:-1]
        assert np.ptp(interior) == pytest.approx(0.0, abs=1e-6)
