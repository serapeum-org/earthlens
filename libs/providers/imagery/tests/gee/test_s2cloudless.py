"""Tests for the `earthlens.gee.cloud_masks.S2Cloudless` mask.

`ee.ImageCollection` is faked and the Sentinel-2 image is a recorder, so each
test asserts which collection is linked, which band is thresholded, and the
`lte` polarity (probability, higher = cloudier → keep the low pixels).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from earthlens.gee import cloud_masks as cloud_masks_module
from earthlens.gee.cloud_masks import ImageMask, S2Cloudless

_PROB_ID = "COPERNICUS/S2_CLOUD_PROBABILITY"


class _FakeProbBand:
    """Records the `lte` threshold applied to the linked probability band."""

    def __init__(self, recorder):
        self._recorder = recorder

    def lte(self, threshold):
        self._recorder["threshold"] = threshold
        return ("lte", threshold)


class _FakeLinkedImage:
    """The `linkCollection` output — shares the source's properties."""

    def __init__(self, source):
        self.properties = source.properties
        self.recorder = source.recorder
        self.masked_from = None

    def select(self, band):
        self.recorder["selects"].append(band)
        return _FakeProbBand(self.recorder)

    def updateMask(self, mask):  # noqa: N802 - mirrors ee API
        self.recorder["mask"] = mask
        derived = _FakeLinkedImage(self)
        derived.masked_from = self
        return derived


class _FakeS2Image:
    """Recorder standing in for a Sentinel-2 `ee.Image`."""

    def __init__(self):
        self.properties = {"system:time_start": 1718236800}
        self.recorder: dict = {
            "link": None,
            "selects": [],
            "threshold": None,
            "mask": None,
        }

    def linkCollection(self, collection, bands):  # noqa: N802 - mirrors ee API
        self.recorder["link"] = (collection, list(bands))
        return _FakeLinkedImage(self)


@pytest.fixture
def fake_ee(monkeypatch):
    """Replace the module's `ee.ImageCollection` with a recording stand-in."""
    fake = SimpleNamespace(ImageCollection=lambda asset_id: ("ic", asset_id))
    monkeypatch.setattr(cloud_masks_module, "ee", fake)
    return fake


class TestS2Cloudless:
    """Tests for `S2Cloudless`."""

    def test_defaults(self):
        """The defaults are a 40% probability cutoff and the s2cloudless id."""
        mask = S2Cloudless()
        assert mask.threshold == 40.0
        assert mask.probability_collection == _PROB_ID

    def test_is_an_image_mask(self):
        """The mask subclasses `ImageMask`, so it is a `cloud_mask=` drop-in."""
        assert isinstance(S2Cloudless(), ImageMask)

    def test_links_probability_band_and_keeps_the_low_pixels(self, fake_ee):
        """The probability collection is linked and masked by `lte` (keep clear)."""
        image = _FakeS2Image()
        out = S2Cloudless()(image)
        assert image.recorder["link"] == (("ic", _PROB_ID), ["probability"])
        assert image.recorder["selects"] == ["probability"]
        assert image.recorder["threshold"] == 40.0
        assert image.recorder["mask"] == ("lte", 40.0), (
            "s2cloudless keeps pixels at/below the probability threshold"
        )
        assert out.masked_from is not None

    def test_custom_threshold(self, fake_ee):
        """A tighter cutoff masks by that probability."""
        image = _FakeS2Image()
        S2Cloudless(threshold=20)(image)
        assert image.recorder["mask"] == ("lte", 20.0)

    def test_custom_collection_id(self, fake_ee):
        """An overridden `probability_collection` is the linked collection."""
        image = _FakeS2Image()
        S2Cloudless(probability_collection="MY/PROB")(image)
        assert image.recorder["link"] == (("ic", "MY/PROB"), ["probability"])

    def test_masked_image_keeps_properties(self, fake_ee):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeS2Image()
        out = S2Cloudless()(image)
        assert out.properties is image.properties

    @pytest.mark.parametrize("threshold", [0, 100])
    def test_boundary_thresholds_accepted(self, threshold):
        """The inclusive bounds of `[0, 100]` construct fine."""
        assert S2Cloudless(threshold=threshold).threshold == threshold

    def test_threshold_above_100_is_refused(self):
        """A probability over 100 is out of range and refused."""
        with pytest.raises(ValidationError, match=r"\[0, 100\]"):
            S2Cloudless(threshold=150)

    def test_a_bool_threshold_is_refused(self):
        """`threshold=True` is not the number 1 (NoBool guard)."""
        with pytest.raises(ValidationError, match="got the boolean"):
            S2Cloudless(threshold=True)
