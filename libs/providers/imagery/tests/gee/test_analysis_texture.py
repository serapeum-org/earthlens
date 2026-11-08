"""Tests for the grey-level co-occurrence texture operation."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis.texture import MEASURES, Texture

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _raster(values: np.ndarray) -> Dataset:
    """Build a one-band raster from `values`."""
    return Dataset.from_array(arr=values.astype("float32"), geo_ref=GEO)


def _flat() -> Dataset:
    """A field with no structure at all."""
    return _raster(np.full((9, 9), 5.0))


def _checkerboard() -> Dataset:
    """Alternating high and low cells: the maximum-contrast pattern."""
    return _raster((np.indices((9, 9)).sum(axis=0) % 2) * 100.0)


def _centre(ds: Dataset) -> float:
    """Read the middle cell, which has a full window on every side."""
    return float(np.asarray(ds.read_array(), dtype="float64")[4, 4])


class TestTheMeasures:
    """What each measure reports for structure and its absence."""

    def test_a_flat_field_has_no_contrast(self):
        """Every pair in the window is identical, so contrast is zero."""
        assert _centre(Texture(measure="contrast").apply(_flat())) == pytest.approx(0.0)

    def test_a_flat_field_is_perfectly_homogeneous(self):
        """Homogeneity is one when every pair sits on the matrix diagonal."""
        assert _centre(Texture(measure="homogeneity").apply(_flat())) == pytest.approx(
            1.0
        )

    def test_a_checkerboard_has_the_maximum_contrast(self):
        """With 16 levels the extremes are 15 apart, so contrast is 15 squared."""
        assert _centre(
            Texture(measure="contrast").apply(_checkerboard())
        ) == pytest.approx(225.0)

    def test_a_checkerboard_is_barely_homogeneous(self):
        """Every pair is as far off-diagonal as the quantisation allows."""
        assert _centre(Texture(measure="homogeneity").apply(_checkerboard())) < 0.01

    def test_entropy_rises_with_structure(self):
        """A flat window has one pair type; a checkerboard has two equally."""
        flat = _centre(Texture(measure="entropy").apply(_flat()))
        checker = _centre(Texture(measure="entropy").apply(_checkerboard()))
        assert flat == pytest.approx(0.0)
        assert checker == pytest.approx(1.0)

    def test_energy_falls_with_structure(self):
        """Energy is one for a single pair type and lower when pairs spread."""
        assert _centre(Texture(measure="energy").apply(_flat())) == pytest.approx(1.0)
        assert _centre(Texture(measure="energy").apply(_checkerboard())) < 1.0

    @pytest.mark.parametrize("measure", MEASURES)
    def test_every_advertised_measure_computes(self, measure):
        """`MEASURES` and what `apply` supports must not drift apart."""
        value = _centre(Texture(measure=measure).apply(_checkerboard()))
        assert np.isfinite(value)

    def test_dissimilarity_is_lower_than_contrast_on_the_same_window(self):
        """Dissimilarity weights the difference linearly, contrast squares it."""
        checker = _checkerboard()
        assert _centre(Texture(measure="dissimilarity").apply(checker)) < _centre(
            Texture(measure="contrast").apply(checker)
        )


class TestConfiguration:
    """The window, the quantisation, and what they refuse."""

    def test_a_larger_radius_sees_more_of_the_pattern(self):
        """`radius` reaches the focal machinery, where the pattern has a scale.

        A gradient is the right probe: a wider window spans more grey levels,
        so more pair types appear and entropy rises. A checkerboard would not
        show this at all - see the scale-invariance test below.
        """
        ramp = _raster(np.tile(np.arange(9, dtype="float32"), (9, 1)))
        narrow = _centre(Texture(measure="entropy", radius=1).apply(ramp))
        wide = _centre(Texture(measure="entropy", radius=3).apply(ramp))
        assert wide > narrow

    def test_a_checkerboard_reads_the_same_at_every_radius(self):
        """Its texture is scale-invariant, which is a property worth pinning.

        Every window of any size over a perfect checkerboard holds the same two
        pair types in the same proportion, so the measures cannot depend on the
        window size. A test that expected otherwise would be wrong about the
        mathematics rather than about the code.
        """
        checker = _checkerboard()
        values = [
            _centre(Texture(measure="contrast", radius=radius).apply(checker))
            for radius in (1, 2, 3)
        ]
        assert values == pytest.approx([225.0, 225.0, 225.0])

    def test_the_level_count_changes_the_quantisation(self):
        """Fewer grey levels put more values in the same bin."""
        ramp = _raster(np.tile(np.arange(9, dtype="float32"), (9, 1)))
        coarse = _centre(Texture(measure="contrast", levels=2).apply(ramp))
        fine = _centre(Texture(measure="contrast", levels=16).apply(ramp))
        assert coarse != pytest.approx(fine)

    def test_a_radius_below_one_is_refused(self):
        """A single cell has no neighbouring pair to count."""
        with pytest.raises(ValidationError, match="radius must be at least 1"):
            Texture(radius=0)

    def test_fewer_than_two_levels_is_refused(self):
        """One grey level makes every measure the same number."""
        with pytest.raises(ValidationError, match="levels must be at least 2"):
            Texture(levels=1)

    def test_an_unknown_measure_is_refused(self):
        """The literal is checked when the operation is built."""
        with pytest.raises(ValidationError, match="measure"):
            Texture(measure="variance")

    def test_the_name_says_which_measure_ran(self):
        """A chain's name should identify the measure."""
        assert Texture(measure="entropy").name == "Texture(entropy)"

    def test_the_output_keeps_the_source_grid(self):
        """A texture raster sits on the same footprint."""
        source = _flat()
        result = Texture().apply(source)
        assert (result.rows, result.columns, result.epsg) == (
            source.rows,
            source.columns,
            source.epsg,
        )


class TestTheUncoveredBranches:
    """Round-1 finding M14: the all-void paths had no test."""

    def test_a_window_of_only_voids_is_undefined(self):
        """With no finite pair there is no co-occurrence to measure."""
        voids = np.full((4, 4), -9999.0, dtype="float32")
        source = Dataset.from_array(arr=voids, geo_ref=GEO, no_data_value=-9999.0)
        result = np.asarray(
            Texture(measure="contrast", radius=1).apply(source).read_array()
        )
        assert np.isnan(result).all()

    def test_a_raster_with_no_finite_cell_still_returns_a_raster(self):
        """The quantisation has no range to work with, and must not raise."""
        voids = np.full((3, 3), -9999.0, dtype="float32")
        source = Dataset.from_array(arr=voids, geo_ref=GEO, no_data_value=-9999.0)
        result = Texture(measure="homogeneity", radius=1).apply(source)
        assert (result.rows, result.columns) == (3, 3)
