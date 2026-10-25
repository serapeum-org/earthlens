"""Tests for the `earthlens.gee.cloud_masks` callable classes (Task 6.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.cloud_masks import (
    _QA_PIXEL_CLEAR_BIT,
    _S2_SCL_MASKED_CLASSES,
    ImageMask,
    LandsatSrMask,
    Sentinel2SclMask,
    landsat_sr,
    sentinel2_scl,
)
from earthlens.gee.configs import EngineConfig


class _FakeMaskTerm:
    """A boolean mask term; `.And(other)` composes into a nested expr."""

    def __init__(self, expr):
        self.expr = expr

    def And(self, other):  # noqa: N802
        return _FakeMaskTerm(("and", self.expr, other.expr))


class _FakeBand:
    """Records the `bitwiseAnd` / `neq` calls made against a selected band."""

    def __init__(self, recorder):
        self._recorder = recorder

    def bitwiseAnd(self, bit):  # noqa: N802
        self._recorder["bit"] = bit
        return ("clear", bit)

    def neq(self, value):
        self._recorder["neq"].append(value)
        return _FakeMaskTerm(("neq", value))


class _FakeImage:
    """Recorder image whose `updateMask` output shares the source properties."""

    def __init__(self, properties=None):
        self.properties = (
            properties if properties is not None else {"system:time_start": 1718236800}
        )
        self.recorder: dict = {"selects": [], "bit": None, "neq": [], "mask": None}
        self.masked_from = None

    def select(self, band):
        self.recorder["selects"].append(band)
        return _FakeBand(self.recorder)

    def updateMask(self, mask):  # noqa: N802
        self.recorder["mask"] = mask
        derived = _FakeImage(properties=self.properties)
        derived.masked_from = self
        return derived


class TestImageMask:
    """Tests for the `ImageMask` base class."""

    def test_base_call_is_abstract(self):
        """Calling the base class directly raises `NotImplementedError`."""
        base = ImageMask()
        image = _FakeImage()
        with pytest.raises(NotImplementedError, match="ImageMask"):
            base(image)

    def test_is_frozen(self):
        """A constructed mask is immutable."""
        mask = LandsatSrMask()
        with pytest.raises(ValidationError):
            mask.sensor = "LS8"

    def test_rejects_unknown_fields(self):
        """`extra="forbid"` refuses a typo'd keyword at construction."""
        with pytest.raises(ValidationError):
            LandsatSrMask(sensro="LS8")

    def test_instance_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=LandsatSrMask()).needs_server_side_compute

    def test_equality_is_by_value(self):
        """Two masks with the same config compare equal."""
        # Bound to locals so the comparison reads as two distinct instances,
        # which is the point: a frozen pydantic model compares by value.
        first = LandsatSrMask(sensor="LS8")
        same = LandsatSrMask(sensor="LS8")
        other = LandsatSrMask(sensor="LS9")
        assert first is not same
        assert first == same
        assert first != other


class TestLandsatSrMask:
    """Tests for `LandsatSrMask`."""

    def test_masks_with_clear_bit(self):
        """The image is masked by `qa.bitwiseAnd(1 << 6)`."""
        image = _FakeImage()
        out = LandsatSrMask()(image)
        assert image.recorder["selects"] == ["QA_PIXEL"]
        assert image.recorder["bit"] == _QA_PIXEL_CLEAR_BIT
        assert image.recorder["mask"] == ("clear", _QA_PIXEL_CLEAR_BIT)
        assert out.masked_from is image

    def test_default_sensor_is_auto(self):
        """Omitting `sensor` defaults to `"auto"`."""
        assert LandsatSrMask().sensor == "auto"

    @pytest.mark.parametrize("sensor", ["LS7", "LS8", "LS9", "auto"])
    def test_accepts_supported_sensors(self, sensor):
        """Each supported sensor name constructs and masks identically."""
        image = _FakeImage()
        LandsatSrMask(sensor=sensor)(image)
        assert image.recorder["bit"] == _QA_PIXEL_CLEAR_BIT

    def test_rejects_unknown_sensor_at_construction(self):
        """An unrecognised sensor raises at construction with the known message."""
        with pytest.raises(ValidationError, match="sensor must be one of"):
            LandsatSrMask(sensor="S2")

    def test_masked_image_keeps_properties(self):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = LandsatSrMask()(image)
        assert out.properties is image.properties
        assert out.masked_from is image

    def test_wrapper_delegates_to_the_class(self):
        """`landsat_sr` records the same call graph as a class instance."""
        via_function, via_class = _FakeImage(), _FakeImage()
        landsat_sr(via_function, sensor="LS9")
        LandsatSrMask(sensor="LS9")(via_class)
        assert via_function.recorder == via_class.recorder

    def test_wrapper_preserves_the_value_error(self):
        """`landsat_sr` still raises `ValueError` naming the valid sensors."""
        image = _FakeImage()
        with pytest.raises(ValueError, match="sensor must be one of"):
            landsat_sr(image, sensor="S2")


class TestSentinel2SclMask:
    """Tests for `Sentinel2SclMask`."""

    def test_default_masked_classes(self):
        """Omitting `masked_classes` keeps the module's SCL drop set."""
        assert Sentinel2SclMask().masked_classes == _S2_SCL_MASKED_CLASSES

    def test_masks_out_each_scl_class(self):
        """The image is masked by `AND(SCL != c)` over every dropped class."""
        image = _FakeImage()
        out = Sentinel2SclMask()(image)
        assert image.recorder["selects"] == ["SCL"]
        assert image.recorder["neq"] == list(_S2_SCL_MASKED_CLASSES)
        assert image.recorder["mask"].expr == (
            "and",
            ("and", ("and", ("neq", 3), ("neq", 8)), ("neq", 9)),
            ("neq", 10),
        )
        assert out.masked_from is image

    def test_single_class_needs_no_and(self):
        """A one-class drop set masks by the bare `neq` term."""
        image = _FakeImage()
        Sentinel2SclMask(masked_classes=(9,))(image)
        assert image.recorder["neq"] == [9]
        assert image.recorder["mask"].expr == ("neq", 9)

    def test_empty_masked_classes_rejected(self):
        """An empty drop set is refused at construction."""
        with pytest.raises(ValidationError, match="at least 1"):
            Sentinel2SclMask(masked_classes=())

    def test_masked_image_keeps_properties(self):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = Sentinel2SclMask()(image)
        assert out.properties is image.properties

    def test_wrapper_delegates_to_the_class(self):
        """`sentinel2_scl` records the same call graph as a default instance."""
        via_function, via_class = _FakeImage(), _FakeImage()
        out_function = sentinel2_scl(via_function)
        out_class = Sentinel2SclMask()(via_class)
        assert via_function.recorder["selects"] == via_class.recorder["selects"]
        assert via_function.recorder["neq"] == via_class.recorder["neq"]
        assert out_function.recorder["mask"] is None
        assert via_function.recorder["mask"].expr == via_class.recorder["mask"].expr
        assert out_class.masked_from is via_class
