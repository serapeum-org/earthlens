"""Tests for the sensor-specific spectral transform: TasseledCap."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from pyramids.base.georeference import GeoReference
from pyramids.dataset import Dataset

from earthlens.gee.analysis import TasseledCap

_errors = pytest.importorskip("pyramids_eo.errors")
TransformError = _errors.TransformError
UnknownSensorError = _errors.UnknownSensorError

GEO = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)
OLI_BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]
_ROLE_VALUE = {
    "blue": 0.1,
    "green": 0.2,
    "red": 0.3,
    "nir": 0.4,
    "swir1": 0.5,
    "swir2": 0.6,
}


def _reflectance(
    size: int = 2, *, named: bool = True, order: list[str] | None = None
) -> Dataset:
    """A 6-band OLI reflectance stack, a distinct constant per band role."""
    order = order or OLI_BANDS
    arr = np.stack(
        [np.full((size, size), _ROLE_VALUE[name], dtype="float32") for name in order]
    )
    ds = Dataset.from_array(arr=arr, geo_ref=GEO, no_data_value=None)
    if named:
        ds.band_names = order
    return ds


class TestTasseledCap:
    """Rotating a reflectance stack into its tasseled-cap components."""

    def test_the_default_is_brightness_greenness_wetness(self):
        """With no `components` the classic three come back, named, in order."""
        tc = TasseledCap(sensor="landsat8_oli")(_reflectance())
        assert tc.band_names == ["brightness", "greenness", "wetness"]
        assert tc.band_count == 3
        assert tc.epsg == 4326

    def test_a_component_subset_keeps_the_requested_order(self):
        """Asking for two components yields a two-band raster in that order."""
        tc = TasseledCap(sensor="landsat8_oli", components=("wetness", "brightness"))(
            _reflectance()
        )
        assert tc.band_names == ["wetness", "brightness"]

    def test_named_bands_are_reordered_to_the_sensor_order(self):
        """A shuffled-but-named stack gives the same result as the canonical one."""
        canonical = TasseledCap(sensor="landsat8_oli")(_reflectance())
        shuffled = TasseledCap(sensor="landsat8_oli")(
            _reflectance(order=["swir2", "blue", "nir", "green", "swir1", "red"])
        )
        assert np.asarray(shuffled.read_array()) == pytest.approx(
            np.asarray(canonical.read_array())
        )

    def test_an_unknown_sensor_is_refused(self):
        """A sensor with no bundled table surfaces pyramids-eo's error."""
        with pytest.raises(UnknownSensorError):
            TasseledCap(sensor="sentinel99")(_reflectance())

    def test_an_unknown_component_is_refused(self):
        """A component the sensor does not define is refused."""
        with pytest.raises(TransformError):
            TasseledCap(sensor="landsat8_oli", components=("sparkle",))(_reflectance())

    def test_a_wrong_band_count_is_refused(self):
        """An unnamed stack of the wrong band count cannot be the sensor's."""
        three_band = Dataset.from_array(
            arr=np.ones((3, 2, 2), dtype="float32"), geo_ref=GEO, no_data_value=None
        )
        with pytest.raises(TransformError):
            TasseledCap(sensor="landsat8_oli")(three_band)

    def test_it_is_frozen(self):
        """The sensor is part of the frozen value object."""
        op = TasseledCap(sensor="landsat8_oli")
        with pytest.raises(ValidationError, match="frozen"):
            op.sensor = "landsat5_tm"

    def test_an_unexpected_field_is_refused_at_construction(self):
        """`extra='forbid'` rejects a field that is not the operation's."""
        with pytest.raises(ValidationError):
            TasseledCap(sensor="landsat8_oli", bands=(0, 1))

    def test_it_does_not_mutate_the_source(self):
        """apply returns a new raster; the reflectance input is unchanged."""
        ds = _reflectance()
        before = ds.read_array().copy()
        TasseledCap(sensor="landsat8_oli")(ds)
        assert np.array_equal(ds.read_array(), before)

    def test_voids_are_carried_through_as_nan(self):
        """A pixel that is no-data in a band is NaN in every component."""
        arr = np.stack(
            [np.full((2, 2), (b + 1) * 0.1, dtype="float32") for b in range(6)]
        )
        arr[0, 0, 0] = -9999.0
        ds = Dataset.from_array(arr=arr, geo_ref=GEO, no_data_value=-9999.0)
        ds.band_names = OLI_BANDS
        out = np.asarray(TasseledCap(sensor="landsat8_oli")(ds).read_array())
        assert np.isnan(out[:, 0, 0]).all()
        assert np.isfinite(out[:, 1, 1]).all()

    def test_a_dn_scale_stack_warns(self):
        """A stack far above the reflectance range is warned about, not rejected."""
        arr = np.full((6, 2, 2), 12000.0, dtype="float32")
        ds = Dataset.from_array(arr=arr, geo_ref=GEO, no_data_value=None)
        ds.band_names = OLI_BANDS
        with pytest.warns(UserWarning, match="reflectance"):
            TasseledCap(sensor="landsat8_oli")(ds)

    def test_it_composes_in_a_chain(self):
        """A spectral transform is still an operation, so it chains."""
        from earthlens.gee.analysis import Reclassify

        chain = TasseledCap(sensor="landsat8_oli", components=("brightness",)) >> (
            Reclassify(bins=((0.0, float("inf"), 1),))
        )
        result = chain.apply(_reflectance())
        assert result.band_count == 1
