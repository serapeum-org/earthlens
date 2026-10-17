"""Tests for the distance-to-target operation."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.proximity import Proximity

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _mask(values: np.ndarray) -> Dataset:
    """Build a one-band integer mask raster."""
    return Dataset.from_array(arr=values.astype("int16"), geo_ref=GEO, no_data_value=-1)


def _centre_target() -> Dataset:
    """A 3x3 mask whose only target is the middle cell."""
    values = np.zeros((3, 3), dtype="int16")
    values[1, 1] = 1
    return _mask(values)


def _two_classes() -> Dataset:
    """A row with a 1 at one end and a 2 at the other, for target selection."""
    values = np.zeros((1, 5), dtype="int16")
    values[0, 0] = 1
    values[0, 4] = 2
    return _mask(values)


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as float64."""
    return np.asarray(ds.read_array(), dtype="float64")


class TestProximity:
    """Distance from each cell to the nearest target."""

    def test_a_target_cell_is_at_distance_zero(self):
        """The target is its own nearest target."""
        assert _values(Proximity(distance_units="PIXEL").apply(_centre_target()))[
            1, 1
        ] == pytest.approx(0.0)

    def test_an_adjacent_cell_is_one_pixel_away(self):
        """Pixel units count cells, which makes the expectation exact."""
        result = _values(Proximity(distance_units="PIXEL").apply(_centre_target()))
        assert result[0, 1] == pytest.approx(1.0)

    def test_a_diagonal_cell_is_further_than_an_orthogonal_one(self):
        """Distances are Euclidean, not city-block."""
        result = _values(Proximity(distance_units="PIXEL").apply(_centre_target()))
        assert result[0, 0] > result[0, 1]

    def test_geographic_units_differ_from_pixel_units(self):
        """`distance_units` reaches pyramids; degrees are not cell counts."""
        source = _centre_target()
        in_pixels = _values(Proximity(distance_units="PIXEL").apply(source))
        in_degrees = _values(Proximity(distance_units="GEO").apply(source))
        assert not np.allclose(in_pixels, in_degrees)

    def test_target_values_select_which_cells_count(self):
        """Measuring to class 2 only gives a different field than to class 1."""
        source = _two_classes()
        to_one = _values(
            Proximity(target_values=(1,), distance_units="PIXEL").apply(source)
        )
        to_two = _values(
            Proximity(target_values=(2,), distance_units="PIXEL").apply(source)
        )
        assert to_one[0, 0] == pytest.approx(0.0)
        assert to_two[0, 4] == pytest.approx(0.0)
        assert to_one[0, 4] > to_two[0, 4]

    def test_every_non_zero_cell_is_a_target_by_default(self):
        """With no selection, both classes count, which is GDAL's own default."""
        result = _values(Proximity(distance_units="PIXEL").apply(_two_classes()))
        assert result[0, 0] == pytest.approx(0.0)
        assert result[0, 4] == pytest.approx(0.0)

    def test_max_distance_stops_measuring_past_the_cap(self):
        """Beyond the cap GDAL writes 65535 rather than a real distance."""
        source = _two_classes()
        uncapped = _values(
            Proximity(target_values=(1,), distance_units="PIXEL").apply(source)
        )
        capped = _values(
            Proximity(
                target_values=(1,), distance_units="PIXEL", max_distance=2.0
            ).apply(source)
        )
        assert uncapped[0, 4] == pytest.approx(4.0)
        assert capped[0, 2] == pytest.approx(2.0)
        assert capped[0, 4] == pytest.approx(65535.0)

    def test_nodata_replaces_the_beyond_cap_fill(self):
        """A caller can make the uncapped cells say "no distance" instead."""
        capped = _values(
            Proximity(
                target_values=(1,),
                distance_units="PIXEL",
                max_distance=2.0,
                nodata=-1.0,
            ).apply(_two_classes())
        )
        assert capped[0, 4] == pytest.approx(-1.0)

    def test_the_output_keeps_the_source_grid(self):
        """A distance field is a raster on the same footprint."""
        source = _centre_target()
        result = Proximity().apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )

    def test_an_unknown_unit_is_rejected_at_construction(self):
        """The literal is validated when the operation is built."""
        with pytest.raises(ValidationError, match="distance_units"):
            Proximity(distance_units="metres")

    def test_the_target_selection_stays_immutable(self):
        """A tuple field keeps the operation frozen and hashable."""
        operation = Proximity(target_values=(1, 2))
        with pytest.raises(ValidationError, match="frozen"):
            operation.target_values = (3,)
