"""Tests for the patch-analysis operations."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.patches import ConnectedComponents, Sieve

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _mask(values: np.ndarray) -> Dataset:
    """Build a one-band integer mask raster."""
    return Dataset.from_array(arr=values.astype("int16"), geo_ref=GEO, no_data_value=-1)


def _two_patches() -> Dataset:
    """A 2x2 block in one corner and a single cell in the other."""
    values = np.zeros((5, 5), dtype="int16")
    values[0:2, 0:2] = 1
    values[4, 4] = 1
    return _mask(values)


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as int64."""
    return np.asarray(ds.read_array()).astype("int64")


class TestConnectedComponents:
    """Labelling contiguous regions."""

    def test_separate_patches_get_separate_labels(self):
        """Two disjoint regions are two components."""
        labels = _values(ConnectedComponents().apply(_two_patches()))
        assert labels.max() == 2
        assert labels[0, 0] == 1
        assert labels[4, 4] == 2

    def test_cells_in_one_patch_share_a_label(self):
        """The 2x2 block is one region, not four."""
        labels = _values(ConnectedComponents().apply(_two_patches()))
        assert len(set(labels[0:2, 0:2].ravel().tolist())) == 1

    def test_out_of_range_cells_are_zero(self):
        """Background is label 0, so a maximum counts the regions."""
        labels = _values(ConnectedComponents().apply(_two_patches()))
        assert labels[2, 2] == 0

    def test_the_bounds_select_which_values_are_in_range(self):
        """Labelling class 2 only ignores class 1 entirely."""
        values = np.zeros((3, 3), dtype="int16")
        values[0, 0] = 1
        values[2, 2] = 2
        labels = _values(
            ConnectedComponents(lower_bound=2, upper_bound=2).apply(_mask(values))
        )
        assert labels[2, 2] == 1
        assert labels[0, 0] == 0

    def test_inverted_bounds_are_refused(self):
        """Bounds that exclude everything are a mistake, not a configuration."""
        with pytest.raises(ValidationError, match="no cell could be in range"):
            ConnectedComponents(lower_bound=5, upper_bound=1)

    def test_the_output_keeps_the_source_grid(self):
        """A label raster sits on the same footprint."""
        source = _two_patches()
        result = ConnectedComponents().apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )

    def test_the_dtype_is_configurable(self):
        """Labels are integers; the width is the caller's to choose."""
        result = ConnectedComponents(dtype="int16").apply(_two_patches())
        assert np.asarray(result.read_array()).dtype == np.int16


class TestSieve:
    """Dropping patches below a size threshold."""

    def test_a_patch_below_the_threshold_is_removed(self):
        """The single stray cell goes; the 2x2 block stays."""
        kept = _values(Sieve(min_patch_cells=2).apply(_two_patches()))
        assert kept[0, 0] == 1
        assert kept[4, 4] == 0

    def test_a_higher_threshold_removes_more(self):
        """Raising the minimum eventually takes the block too."""
        kept = _values(Sieve(min_patch_cells=8).apply(_two_patches()))
        assert kept.max() == 0

    def test_connectedness_is_configurable(self):
        """Diagonal neighbours count only under 8-connectedness."""
        diagonal = np.zeros((5, 5), dtype="int16")
        diagonal[1, 1] = 1
        diagonal[2, 2] = 1
        four = _values(Sieve(min_patch_cells=2, connectedness=4).apply(_mask(diagonal)))
        eight = _values(
            Sieve(min_patch_cells=2, connectedness=8).apply(_mask(diagonal))
        )
        assert four.max() == 0
        assert eight.max() == 1

    def test_a_threshold_below_two_is_refused(self):
        """A one-cell minimum keeps every patch, so it is a no-op."""
        with pytest.raises(ValidationError, match="at least 2"):
            Sieve(min_patch_cells=1)

    def test_an_unknown_connectedness_is_refused(self):
        """GDAL takes 4 or 8; anything else is a typo."""
        with pytest.raises(ValidationError, match="connectedness"):
            Sieve(connectedness=6)

    def test_it_composes_after_labelling(self):
        """Label then sieve is the motivating sequence."""
        chain = ConnectedComponents() >> Sieve(min_patch_cells=2)
        assert chain.apply(_two_patches()).band_count == 1


class TestANonZeroBand:
    """Round-1 finding M14: an entire documented path had no test."""

    def test_labelling_reads_the_band_it_was_asked_for(self):
        """`cluster` reads band 0, so another band has to be selected first.

        Band 0 here is empty and band 1 holds two patches: labelling band 1
        must find them, which labelling band 0 cannot.
        """
        empty = np.zeros((3, 3), dtype="float32")
        patches = np.array([[1, 1, 0], [0, 0, 0], [0, 0, 1]], dtype="float32")
        stack = Dataset.from_array(
            arr=np.stack([empty, patches]), geo_ref=GEO, no_data_value=-1.0
        )
        labelled = ConnectedComponents(band=1, lower_bound=0.5, upper_bound=1.5)
        assert int(np.asarray(labelled.apply(stack).read_array()).max()) == 2
        # Band 0 is empty, so labelling it finds nothing. This used to crash:
        # with `band=0` the whole multi-band array reached `Dataset.cluster`,
        # which takes no band argument, and it failed inside its own loop.
        on_band_zero = ConnectedComponents(lower_bound=0.5, upper_bound=1.5)
        assert int(np.asarray(on_band_zero.apply(stack).read_array()).max()) == 0

    def test_the_selected_band_keeps_the_source_grid(self):
        """Selecting a band must not move the raster."""
        stack = Dataset.from_array(
            arr=np.stack([np.zeros((3, 3), dtype="float32")] * 2),
            geo_ref=GEO,
            no_data_value=-1.0,
        )
        result = ConnectedComponents(band=1).apply(stack)
        assert (result.rows, result.columns, result.epsg) == (3, 3, stack.epsg)
