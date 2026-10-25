"""Tests for the expanded cloud-mask library (Task 6.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.cloud_masks import (
    _LS_CLOUD_CONF_SHIFT,
    _LS_CONF_PAIR_MASK,
    _LS_SHADOW_CONF_SHIFT,
    _MODIS_CLOUD_STATE_MASK,
    _S2_QA60_CIRRUS_BIT,
    _S2_QA60_CLOUD_BIT,
    ImageMask,
    LandsatQaRefinedMask,
    ModisState1kmMask,
    Sentinel2Qa60Mask,
)
from earthlens.gee.configs import EngineConfig


class _Term:
    """A band expression recording the bit and comparison calls made on it."""

    def __init__(self, expr):
        self.expr = expr

    def bitwiseAnd(self, bits):  # noqa: N802 - mirrors the Earth Engine method
        return _Term(("bitand", self.expr, bits))

    def rightShift(self, count):  # noqa: N802 - mirrors the Earth Engine method
        return _Term(("shr", self.expr, count))

    def eq(self, value):
        return _Term(("eq", self.expr, value))

    def lte(self, value):
        return _Term(("lte", self.expr, value))

    def And(self, other):  # noqa: N802 - mirrors the Earth Engine method
        return _Term(("and", self.expr, other.expr))


class _FakeImage:
    """Recorder image whose `updateMask` output shares the source properties."""

    def __init__(self, properties=None):
        self.properties = (
            properties if properties is not None else {"system:time_start": 1718236800}
        )
        self.selects: list[str] = []
        self.mask = None
        self.masked_from = None

    def select(self, band):
        self.selects.append(band)
        return _Term(("band", band))

    def updateMask(self, mask):  # noqa: N802 - mirrors the Earth Engine method
        self.mask = mask
        derived = _FakeImage(properties=self.properties)
        derived.masked_from = self
        return derived


class TestSentinel2Qa60Mask:
    """Tests for `Sentinel2Qa60Mask`."""

    def test_is_an_image_mask(self):
        """The QA60 mask is a frozen `ImageMask` drop-in."""
        assert isinstance(Sentinel2Qa60Mask(), ImageMask)

    def test_masks_cloud_and_cirrus_bits(self):
        """The mask requires both the opaque-cloud and cirrus bits clear."""
        image = _FakeImage()
        out = Sentinel2Qa60Mask()(image)
        assert image.selects == ["QA60"]
        assert image.mask.expr == (
            "and",
            ("eq", ("bitand", ("band", "QA60"), _S2_QA60_CLOUD_BIT), 0),
            ("eq", ("bitand", ("band", "QA60"), _S2_QA60_CIRRUS_BIT), 0),
        )
        assert out.masked_from is image

    def test_bit_positions_are_ten_and_eleven(self):
        """The QA60 cloud bit is 10 and the cirrus bit is 11."""
        assert _S2_QA60_CLOUD_BIT == 1 << 10
        assert _S2_QA60_CIRRUS_BIT == 1 << 11

    def test_rejects_unknown_fields(self):
        """A typo'd keyword is refused at construction."""
        with pytest.raises(ValidationError):
            Sentinel2Qa60Mask(threshold=0.5)

    def test_masked_image_keeps_properties(self):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = Sentinel2Qa60Mask()(image)
        assert out.properties is image.properties

    def test_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=Sentinel2Qa60Mask()).needs_server_side_compute


class TestLandsatQaRefinedMask:
    """Tests for `LandsatQaRefinedMask`."""

    def test_reads_both_confidence_bit_pairs(self):
        """The mask thresholds the cloud and shadow confidence bit-pairs."""
        image = _FakeImage()
        out = LandsatQaRefinedMask()(image)
        assert image.selects == ["QA_PIXEL"]
        assert image.mask.expr == (
            "and",
            (
                "lte",
                (
                    "bitand",
                    ("shr", ("band", "QA_PIXEL"), _LS_CLOUD_CONF_SHIFT),
                    _LS_CONF_PAIR_MASK,
                ),
                2,
            ),
            (
                "lte",
                (
                    "bitand",
                    ("shr", ("band", "QA_PIXEL"), _LS_SHADOW_CONF_SHIFT),
                    _LS_CONF_PAIR_MASK,
                ),
                2,
            ),
        )
        assert out.masked_from is image

    def test_bit_pair_positions(self):
        """Cloud confidence sits at bits 8-9 and shadow confidence at 10-11."""
        assert _LS_CLOUD_CONF_SHIFT == 8
        assert _LS_SHADOW_CONF_SHIFT == 10
        assert _LS_CONF_PAIR_MASK == 0b11

    def test_thresholds_are_applied(self):
        """Custom confidence ceilings reach the comparison terms."""
        image = _FakeImage()
        LandsatQaRefinedMask(max_cloud_confidence=1, max_shadow_confidence=3)(image)
        cloud_term, shadow_term = image.mask.expr[1], image.mask.expr[2]
        assert cloud_term[2] == 1
        assert shadow_term[2] == 3

    def test_defaults(self):
        """The defaults keep up-to-medium confidence and an `auto` sensor."""
        mask = LandsatQaRefinedMask()
        assert mask.sensor == "auto"
        assert mask.max_cloud_confidence == 2
        assert mask.max_shadow_confidence == 2

    @pytest.mark.parametrize("value", [-1, 4])
    @pytest.mark.parametrize("field", ["max_cloud_confidence", "max_shadow_confidence"])
    def test_confidence_out_of_range_refused(self, field, value):
        """A confidence ceiling outside 0-3 is refused at construction."""
        with pytest.raises(ValidationError):
            LandsatQaRefinedMask(**{field: value})

    @pytest.mark.parametrize("sensor", ["LS7", "LS8", "LS9", "auto"])
    def test_accepts_supported_sensors(self, sensor):
        """Each supported sensor name constructs."""
        assert LandsatQaRefinedMask(sensor=sensor).sensor == sensor

    def test_rejects_unknown_sensor_at_construction(self):
        """An unrecognised sensor raises the long-standing message."""
        with pytest.raises(ValidationError, match="sensor must be one of"):
            LandsatQaRefinedMask(sensor="S2")

    def test_masked_image_keeps_properties(self):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = LandsatQaRefinedMask()(image)
        assert out.properties is image.properties

    def test_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=LandsatQaRefinedMask()).needs_server_side_compute


class TestModisState1kmMask:
    """Tests for `ModisState1kmMask`."""

    def test_keeps_the_clear_cloud_state(self):
        """Only pixels whose 2-bit cloud state is `0` (clear) are kept."""
        image = _FakeImage()
        out = ModisState1kmMask()(image)
        assert image.selects == ["state_1km"]
        assert image.mask.expr == (
            "eq",
            ("bitand", ("band", "state_1km"), _MODIS_CLOUD_STATE_MASK),
            0,
        )
        assert out.masked_from is image

    def test_cloud_state_field_is_the_low_bit_pair(self):
        """The MODIS cloud state lives in bits 0-1 of `state_1km`."""
        assert _MODIS_CLOUD_STATE_MASK == 0b11

    def test_rejects_unknown_fields(self):
        """A typo'd keyword is refused at construction."""
        with pytest.raises(ValidationError):
            ModisState1kmMask(band="state_1km")

    def test_masked_image_keeps_properties(self):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = ModisState1kmMask()(image)
        assert out.properties is image.properties

    def test_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=ModisState1kmMask()).needs_server_side_compute
