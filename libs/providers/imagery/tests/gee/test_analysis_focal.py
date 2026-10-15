"""Tests for the focal (neighbourhood) operations."""

from __future__ import annotations

import numpy as np
import pytest
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.focal import FocalApply, FocalMean, FocalMedian, FocalStd

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _raster(values: np.ndarray, no_data: float | None = None) -> Dataset:
    """Build a one-band raster, optionally declaring a no-data value."""
    if no_data is None:
        return Dataset.from_array(arr=values.astype("float32"), geo_ref=GEO)
    return Dataset.from_array(
        arr=values.astype("float32"), geo_ref=GEO, no_data_value=no_data
    )


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as float64."""
    return np.asarray(ds.read_array(), dtype="float64")


def _spike() -> np.ndarray:
    """A constant field with one outlier, which mean and median treat differently."""
    values = np.full((5, 5), 10.0)
    values[2, 2] = 100.0
    return values


def _window_span(values_1d) -> float:  # noqa: ANN001 - the flat window pyramids passes
    """Return the range of a window, ignoring no-data cells."""
    return float(np.nanmax(values_1d) - np.nanmin(values_1d))


class TestTheNamedReducers:
    """Mean, standard deviation and median over a square window."""

    def test_the_mean_of_a_constant_field_is_the_constant(self):
        """Smoothing changes nothing where there is nothing to smooth."""
        result = _values(FocalMean().apply(_raster(np.full((5, 5), 7.0))))
        assert result[2, 2] == pytest.approx(7.0)

    def test_the_mean_smooths_a_spike_but_the_median_removes_it(self):
        """The two reducers differ exactly where an outlier sits."""
        source = _raster(_spike())
        mean = _values(FocalMean().apply(source))[2, 2]
        median = _values(FocalMedian().apply(source))[2, 2]
        assert mean > 10.0
        assert median == pytest.approx(10.0)

    def test_the_standard_deviation_is_zero_on_a_constant_field(self):
        """No variation in the window means no spread."""
        result = _values(FocalStd().apply(_raster(np.full((5, 5), 3.0))))
        assert result[2, 2] == pytest.approx(0.0)

    def test_a_larger_radius_reaches_further(self):
        """`radius` is honoured: a wider window dilutes the spike more."""
        source = _raster(_spike())
        narrow = _values(FocalMean(radius=1).apply(source))[2, 2]
        wide = _values(FocalMean(radius=2).apply(source))[2, 2]
        assert wide < narrow

    def test_a_radius_below_one_is_refused(self):
        """Radius 0 would reduce each cell to itself, which is not a window."""
        with pytest.raises(Exception, match="radius must be at least 1"):
            FocalMean(radius=0)

    def test_the_reducers_ignore_no_data_rather_than_propagating_it(self):
        """A void must not blank every window that merely touches it."""
        values = np.full((5, 5), 4.0)
        values[0, 0] = -9999.0
        result = _values(FocalMean().apply(_raster(values, no_data=-9999.0)))
        # The cell diagonally adjacent to the void still has eight valid
        # neighbours, so a NaN-aware mean reports the constant.
        assert result[1, 1] == pytest.approx(4.0)

    def test_the_output_keeps_the_source_grid(self):
        """A focal result is a raster on the same footprint."""
        source = _raster(np.full((5, 5), 1.0))
        result = FocalMean().apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )


class TestFocalApply:
    """An arbitrary reducer over the same window machinery."""

    def test_a_custom_reducer_runs_over_each_window(self):
        """The span of a constant field is zero; of the spike's window, 90."""
        source = _raster(_spike())
        result = _values(FocalApply(func=_window_span).apply(source))
        assert result[2, 2] == pytest.approx(90.0)
        assert result[0, 4] == pytest.approx(0.0)

    def test_the_name_says_which_callable_ran(self):
        """A chain's name should identify the reducer, not just the wrapper."""
        assert FocalApply(func=_window_span).name == "FocalApply(_window_span)"

    def test_the_callable_cannot_be_swapped_after_construction(self):
        """The operation stays a frozen value object despite holding a function."""
        operation = FocalApply(func=_window_span)
        with pytest.raises(Exception, match="frozen"):
            operation.func = np.nanmax

    def test_it_composes_with_a_named_reducer(self):
        """Focal operations chain like any other."""
        chain = FocalMean() >> FocalApply(func=_window_span)
        result = _values(chain.apply(_raster(np.full((5, 5), 2.0))))
        assert result[2, 2] == pytest.approx(0.0)
