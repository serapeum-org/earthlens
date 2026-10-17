"""Tests for the companion-raster operations: PanSharpen and MatchHistogram."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import MatchHistogram, PanSharpen

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)


def _geo(cell_size: float) -> GeoReference:
    """A grid at the shared origin with the given cell size."""
    return GeoReference(top_left_corner=(31.0, 30.0), cell_size=cell_size, epsg=4326)


def _multispectral(bands: int = 3, size: int = 16, seed: int = 0) -> Dataset:
    """A coarse multispectral raster of `bands` bands."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 4000, size=(bands, size, size)).astype("uint16")
    return Dataset.from_array(arr=arr, geo_ref=_geo(0.02), no_data_value=None)


def _pan(size: int = 64, seed: int = 1) -> Dataset:
    """A fine single-band panchromatic raster over the same footprint."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 4000, size=(size, size)).astype("uint16")
    return Dataset.from_array(arr=arr, geo_ref=_geo(0.005), no_data_value=None)


def _band(mean: float, std: float, seed: int, no_data: float | None = None) -> Dataset:
    """An 8x8 single-band raster drawn from a normal distribution."""
    rng = np.random.default_rng(seed)
    arr = rng.normal(mean, std, (8, 8)).astype("float32")
    return Dataset.from_array(arr=arr, geo_ref=GEO, no_data_value=no_data)


class TestPanSharpen:
    """Fusing a multispectral raster with a finer pan band."""

    def test_the_output_is_at_the_pan_resolution(self):
        """A 16x16 multispectral sharpened by a 64x64 pan comes out 64x64."""
        fused = PanSharpen(pan=_pan()).apply(_multispectral())
        assert (fused.rows, fused.columns) == (64, 64)
        assert fused.band_count == 3
        assert fused.epsg == 4326

    def test_a_band_subset_sharpens_only_those_bands(self):
        """Naming two bands yields a two-band fused raster, in that order."""
        fused = PanSharpen(pan=_pan(), bands=(0, 2), weights=(0.6, 0.4)).apply(
            _multispectral()
        )
        assert fused.band_count == 2

    def test_weights_must_match_the_selected_band_count(self):
        """pyramids rejects a weights/bands length mismatch; surfaced unchanged."""
        with pytest.raises(ValueError):
            PanSharpen(pan=_pan(), bands=(0, 1), weights=(1.0,)).apply(_multispectral())

    def test_an_unknown_algorithm_is_refused_at_construction(self):
        """Only Brovey exists, and the literal is checked when the op is built."""
        with pytest.raises(ValidationError, match="algorithm"):
            PanSharpen(pan=_pan(), algorithm="hpf")

    def test_it_is_frozen_despite_holding_a_raster(self):
        """The companion raster travels as part of the value object."""
        operation = PanSharpen(pan=_pan())
        with pytest.raises(ValidationError, match="frozen"):
            operation.resampling = "nearest"

    def test_it_does_not_mutate_the_source(self):
        """apply returns a new raster; the multispectral input is unchanged."""
        ms = _multispectral()
        before = ms.read_array().copy()
        PanSharpen(pan=_pan()).apply(ms)
        assert np.array_equal(ms.read_array(), before)


class TestMatchHistogram:
    """Remapping a band onto a reference acquisition's distribution."""

    def test_a_dim_band_shifts_onto_a_bright_reference(self):
        """A mean-50 source matched to a mean-200 reference moves toward 200."""
        matched = MatchHistogram(reference=_band(200, 30, seed=2)).apply(
            _band(50, 10, seed=1)
        )
        assert float(np.asarray(matched.read_array()).mean()) > 150.0

    def test_no_data_is_preserved_not_matched(self):
        """A void stays the source's sentinel rather than being remapped."""
        arr = np.random.default_rng(3).normal(50, 10, (8, 8)).astype("float32")
        arr[0, 0] = -9999.0
        src = Dataset.from_array(arr=arr, geo_ref=GEO, no_data_value=-9999.0)
        matched = MatchHistogram(reference=_band(200, 30, seed=4)).apply(src)
        assert float(np.asarray(matched.read_array())[0, 0]) == -9999.0
        assert matched.no_data_value[0] == -9999.0

    def test_matching_to_itself_is_close_to_identity(self):
        """A band matched to its own distribution barely moves."""
        source = _band(100, 20, seed=5)
        matched = MatchHistogram(reference=source).apply(source)
        assert np.asarray(matched.read_array()) == pytest.approx(
            np.asarray(source.read_array()), abs=1.0
        )

    def test_it_is_frozen_despite_holding_a_raster(self):
        """The reference raster is part of the frozen value object."""
        operation = MatchHistogram(reference=_band(200, 30, seed=6))
        with pytest.raises(ValidationError, match="frozen"):
            operation.band = 1

    def test_it_composes_in_a_chain(self):
        """A companion-raster op is still an operation, so it chains."""
        from earthlens.gee.analysis import Reclassify

        chain = MatchHistogram(reference=_band(200, 30, seed=7)) >> Reclassify(
            bins=((150.0, float("inf"), 1),)
        )
        result = chain.apply(_band(50, 10, seed=8))
        assert result.band_count == 1
