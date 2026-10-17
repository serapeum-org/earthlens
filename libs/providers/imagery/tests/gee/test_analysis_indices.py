"""Tests for the normalized-difference index operations."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import OperationChain
from earthlens.gee.analysis.indices import Ndbi, Ndvi, Ndwi, NormalizedDifference

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _two_band(first: float, second: float, shape: tuple[int, int] = (2, 2)) -> Dataset:
    """Build a two-band raster whose bands are constant."""
    stack = np.stack(
        [
            np.full(shape, first, dtype="float32"),
            np.full(shape, second, dtype="float32"),
        ]
    )
    return Dataset.from_array(arr=stack, geo_ref=GEO, no_data_value=np.nan)


def _values(ds: Dataset) -> np.ndarray:
    """Read a raster's first band as float64."""
    return np.asarray(ds.read_array(), dtype="float64")


class TestNormalizedDifference:
    """The shared formula and its edge cases."""

    def test_computes_the_normalized_difference(self):
        """`(0.6 - 0.2) / (0.6 + 0.2)` is 0.5."""
        result = NormalizedDifference(a=0, b=1).apply(_two_band(0.6, 0.2))
        assert _values(result)[0, 0] == pytest.approx(0.5)

    def test_is_antisymmetric_in_its_bands(self):
        """Swapping the bands negates the index."""
        forward = _values(NormalizedDifference(a=0, b=1).apply(_two_band(0.6, 0.2)))
        reverse = _values(NormalizedDifference(a=1, b=0).apply(_two_band(0.6, 0.2)))
        assert forward[0, 0] == pytest.approx(-reverse[0, 0])

    def test_output_has_one_band_on_the_source_grid(self):
        """The index is a single band placed on the input's georeferencing."""
        source = _two_band(0.6, 0.2, shape=(4, 3))
        result = NormalizedDifference().apply(source)
        assert (result.band_count, result.rows, result.columns) == (1, 4, 3)
        assert result.epsg == source.epsg

    def test_a_zero_denominator_becomes_no_data_rather_than_infinity(self):
        """Both bands zero means the index is undefined, not zero."""
        result = NormalizedDifference().apply(_two_band(0.0, 0.0))
        values = _values(result)
        assert not np.isfinite(values).any(), values

    def test_a_zero_denominator_does_not_warn(self):
        """The undefined cells are masked before the division, not after."""
        with np.errstate(all="raise"):
            NormalizedDifference().apply(_two_band(0.0, 0.0))

    def test_a_missing_band_is_named_in_the_error(self):
        """Asking for band 5 of a two-band raster says so."""
        with pytest.raises(IndexError, match=r"band 5 of a raster with 2 band"):
            NormalizedDifference(a=0, b=5).apply(_two_band(0.6, 0.2))

    def test_the_source_is_left_alone(self):
        """Computing an index does not touch the raster it read."""
        source = _two_band(0.6, 0.2)
        before = np.asarray(source.read_array(), dtype="float64").copy()
        NormalizedDifference().apply(source)
        assert np.array_equal(np.asarray(source.read_array(), dtype="float64"), before)


class TestTheNamedPresets:
    """Each preset is the shared formula over conventionally-named bands."""

    @pytest.mark.parametrize(
        ("operation", "expected"),
        [
            (Ndvi(nir=0, red=1), 0.5),
            (Ndwi(green=0, nir=1), 0.5),
            (Ndbi(swir=0, nir=1), 0.5),
        ],
        ids=["ndvi", "ndwi", "ndbi"],
    )
    def test_each_preset_computes_the_difference_of_its_bands(
        self, operation, expected
    ):
        """A preset differs from the base only in its field names."""
        result = operation.apply(_two_band(0.6, 0.2))
        assert _values(result)[0, 0] == pytest.approx(expected)

    def test_a_preset_honours_its_band_selection(self):
        """`Ndvi(nir=1, red=0)` reads the bands the other way round."""
        result = Ndvi(nir=1, red=0).apply(_two_band(0.6, 0.2))
        assert _values(result)[0, 0] == pytest.approx(-0.5)

    def test_presets_report_their_own_name(self):
        """A chain's name should say NDVI, not NormalizedDifference."""
        assert Ndvi().name == "Ndvi"
        assert (Ndvi() >> Ndwi()).name == "Ndvi >> Ndwi"

    def test_a_preset_rejects_the_base_class_fields(self):
        """`Ndvi(a=0)` is a typo, not a shortcut to the shared formula."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Ndvi(a=0)

    def test_an_index_composes_in_a_chain(self):
        """The output of an index is a raster, so it feeds the next operation."""
        chain = OperationChain.of(Ndvi(nir=0, red=1))
        assert _values(chain.apply(_two_band(0.6, 0.2)))[0, 0] == pytest.approx(0.5)
