"""Tests for the reclassification and unit-conversion operations."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.reclassify import ConvertUnits, Reclassify

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)

#: Flat, gentle, moderate and steep, so three bins leave one value unmatched
#: when the bins do not cover the whole range.
SLOPES = np.array([[0.0, 5.0, 15.0, 40.0]], dtype="float32")

SLOPE_CLASSES = ((0.0, 10.0, 1), (10.0, 30.0, 2), (30.0, float("inf"), 3))


def _raster(values: np.ndarray, units: str | None = None) -> Dataset:
    """Build a one-band raster, optionally declaring its units."""
    ds = Dataset.from_array(
        arr=values.astype("float32"), geo_ref=GEO, no_data_value=-9999.0
    )
    if units is not None:
        ds.band_units = [units]
    return ds


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as float64."""
    return np.asarray(ds.read_array(), dtype="float64")


class TestReclassify:
    """Binning continuous values into classes."""

    def test_each_value_lands_in_its_bin(self):
        """Three slope classes over four values."""
        result = _values(Reclassify(bins=SLOPE_CLASSES).apply(_raster(SLOPES)))
        assert result.ravel().tolist() == [1.0, 1.0, 2.0, 3.0]

    def test_bins_are_half_open_so_edges_do_not_overlap(self):
        """A value on a shared edge belongs to the upper bin only."""
        edge = _raster(np.array([[10.0]], dtype="float32"))
        result = _values(Reclassify(bins=SLOPE_CLASSES).apply(edge))
        assert result[0, 0] == pytest.approx(2.0)

    def test_an_unmatched_value_becomes_no_data(self):
        """An uncovered value is a visible hole, not a guessed class."""
        result = _values(
            Reclassify(bins=((30.0, float("inf"), 9),)).apply(_raster(SLOPES))
        )
        assert result[0, 3] == pytest.approx(9.0)
        assert np.isnan(result[0, 0])

    def test_later_bins_win_where_they_overlap(self):
        """Overlapping bins are allowed; the order resolves them, predictably."""
        overlapping = ((0.0, 50.0, 1), (10.0, 50.0, 2))
        result = _values(Reclassify(bins=overlapping).apply(_raster(SLOPES)))
        assert result.ravel().tolist() == [1.0, 1.0, 2.0, 2.0]

    def test_an_empty_bin_list_is_refused(self):
        """Every value would become no-data, which is never the intent."""
        with pytest.raises(ValidationError, match="bins is empty"):
            Reclassify(bins=())

    def test_an_inverted_bin_is_refused(self):
        """A bin that matches nothing is a mistake, not a configuration."""
        with pytest.raises(ValidationError, match="matches nothing"):
            Reclassify(bins=((30.0, 10.0, 1),))

    def test_the_dtype_is_configurable(self):
        """Class values are integers; storing them as such is the point."""
        result = Reclassify(bins=SLOPE_CLASSES, dtype="uint8").apply(_raster(SLOPES))
        assert np.asarray(result.read_array()).dtype == np.uint8

    def test_the_output_keeps_the_source_grid(self):
        """A classified raster sits on the same footprint."""
        source = _raster(SLOPES)
        result = Reclassify(bins=SLOPE_CLASSES).apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )

    def test_the_source_is_left_alone(self):
        """Reclassifying reads the raster; it does not rewrite it."""
        source = _raster(SLOPES)
        before = _values(source).copy()
        Reclassify(bins=SLOPE_CLASSES).apply(source)
        assert np.array_equal(_values(source), before)

    def test_it_composes_after_a_terrain_operation(self):
        """Slope classes are the motivating case, so the chain must work."""
        from earthlens.gee.analysis.terrain import Slope

        elevation = _raster(np.tile(np.arange(6, 0, -1, dtype="float32"), (6, 1)))
        chain = Slope() >> Reclassify(bins=((0.0, 1.0, 1), (1.0, float("inf"), 2)))
        assert chain.apply(elevation).band_count == 1


class TestConvertUnits:
    """Delegating unit conversion to pyramids' table."""

    def test_kelvin_converts_to_celsius(self):
        """300 K is 26.85 degrees Celsius, which pyramids computes."""
        result = ConvertUnits(target="celsius").apply(
            _raster(np.full((2, 2), 300.0, dtype="float32"), units="K")
        )
        assert _values(result)[0, 0] == pytest.approx(26.85, abs=1e-6)

    def test_the_result_declares_the_new_unit(self):
        """A converted raster says what its values now are."""
        result = ConvertUnits(target="celsius").apply(
            _raster(np.full((2, 2), 300.0, dtype="float32"), units="K")
        )
        assert result.band_units == ["celsius"]

    def test_the_name_says_which_unit_was_asked_for(self):
        """A chain's name should name the target unit."""
        assert ConvertUnits(target="celsius").name == "ConvertUnits(celsius)"

    def test_an_unknown_unit_is_pyramids_error_to_raise(self):
        """This wrapper carries no unit table, so it does not pre-judge labels."""
        with pytest.raises(Exception, match="(?i)unit"):
            ConvertUnits(target="bananas").apply(
                _raster(np.full((2, 2), 300.0, dtype="float32"), units="K")
            )
