"""Tests for `earthlens.gee.cloud_masks` (M3)."""

from __future__ import annotations

import pytest

from earthlens.gee.cloud_masks import (
    _QA_PIXEL_CLEAR_BIT,
    _S2_SCL_MASKED_CLASSES,
    landsat_sr,
    sentinel2_scl,
)


class _FakeMaskedImage:
    def __init__(self, mask):
        self.mask = mask


class _FakeQaBand:
    def __init__(self, recorder):
        self._recorder = recorder

    def bitwiseAnd(self, bit):  # noqa: N802
        self._recorder["bit"] = bit
        return f"clear<{bit}>"


class _FakeImage:
    def __init__(self):
        self.recorder: dict = {"selects": [], "bit": None, "mask": None}

    def select(self, band):
        self.recorder["selects"].append(band)
        return _FakeQaBand(self.recorder)

    def updateMask(self, mask):  # noqa: N802
        self.recorder["mask"] = mask
        return _FakeMaskedImage(mask)


class TestLandsatSr:
    """Tests for `landsat_sr`."""

    def test_clear_bit_is_position_six(self):
        """The Clear bit constant matches USGS's `1 << 6`."""
        assert _QA_PIXEL_CLEAR_BIT == 64

    def test_masks_with_clear_bit(self):
        """The image is masked by `qa.bitwiseAnd(1 << 6)`."""
        image = _FakeImage()
        out = landsat_sr(image)
        assert image.recorder["selects"] == ["QA_PIXEL"]
        assert image.recorder["bit"] == _QA_PIXEL_CLEAR_BIT
        assert image.recorder["mask"] == f"clear<{_QA_PIXEL_CLEAR_BIT}>"
        assert isinstance(out, _FakeMaskedImage)

    @pytest.mark.parametrize("sensor", ["LS7", "LS8", "LS9", "auto"])
    def test_accepts_supported_sensors(self, sensor):
        """Each supported sensor name produces the same masked image."""
        landsat_sr(_FakeImage(), sensor=sensor)

    def test_rejects_unknown_sensor(self):
        """An unrecognised sensor name raises `ValueError` listing the valid ones."""
        with pytest.raises(ValueError, match="sensor must be one of"):
            landsat_sr(_FakeImage(), sensor="S2")


class _FakeMask:
    """A boolean mask term; `.And(other)` composes into a nested expr."""

    def __init__(self, expr):
        self.expr = expr

    def And(self, other):  # noqa: N802
        return _FakeMask(("and", self.expr, other.expr))


class _FakeSclBand:
    def __init__(self, recorder):
        self._recorder = recorder

    def neq(self, value):
        self._recorder["neq"].append(value)
        return _FakeMask(("neq", value))


class _FakeS2Image:
    def __init__(self):
        self.recorder: dict = {"selects": [], "neq": [], "mask": None}

    def select(self, band):
        self.recorder["selects"].append(band)
        return _FakeSclBand(self.recorder)

    def updateMask(self, mask):  # noqa: N802
        self.recorder["mask"] = mask
        return _FakeMaskedImage(mask)


class TestSentinel2Scl:
    """Tests for `sentinel2_scl`."""

    def test_masked_classes_are_shadow_cloud_cirrus(self):
        """The dropped SCL classes are shadow (3), cloud (8/9), cirrus (10)."""
        assert _S2_SCL_MASKED_CLASSES == (3, 8, 9, 10)

    def test_masks_out_each_scl_class(self):
        """The image is masked by `AND(SCL != c)` over every dropped class."""
        image = _FakeS2Image()
        out = sentinel2_scl(image)
        assert image.recorder["selects"] == ["SCL"]
        assert image.recorder["neq"] == list(_S2_SCL_MASKED_CLASSES)
        # Left-to-right AND: ((((SCL!=3) & SCL!=8) & SCL!=9) & SCL!=10).
        assert out.mask.expr == (
            "and",
            ("and", ("and", ("neq", 3), ("neq", 8)), ("neq", 9)),
            ("neq", 10),
        )
        assert isinstance(out, _FakeMaskedImage)
