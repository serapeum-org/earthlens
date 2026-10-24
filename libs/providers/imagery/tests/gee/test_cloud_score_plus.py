"""Tests for the `earthlens.gee.cloud_masks.CloudScorePlus` mask (Task 6.3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from earthlens.gee import cloud_masks as cloud_masks_module
from earthlens.gee.cloud_masks import CloudScorePlus, ImageMask
from earthlens.gee.configs import EngineConfig

_CS_ID = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"


class _FakeScoreBand:
    """Records the `gte` threshold applied to the linked score band."""

    def __init__(self, recorder):
        self._recorder = recorder

    def gte(self, threshold):
        self._recorder["threshold"] = threshold
        return ("gte", threshold)


class _FakeLinkedImage:
    """The `linkCollection` output — shares the source's properties."""

    def __init__(self, source):
        self.properties = source.properties
        self.recorder = source.recorder
        self.masked_from = None

    def select(self, band):
        self.recorder["selects"].append(band)
        return _FakeScoreBand(self.recorder)

    def updateMask(self, mask):  # noqa: N802
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

    def linkCollection(self, collection, bands):  # noqa: N802
        self.recorder["link"] = (collection, list(bands))
        return _FakeLinkedImage(self)


@pytest.fixture(scope="function")
def fake_ee(monkeypatch):
    """Replace the module's `ee.ImageCollection` with a recording stand-in."""
    fake = SimpleNamespace(ImageCollection=lambda asset_id: ("ic", asset_id))
    monkeypatch.setattr(cloud_masks_module, "ee", fake)
    return fake


class TestCloudScorePlus:
    """Tests for `CloudScorePlus`."""

    def test_defaults(self):
        """The defaults are the `cs` band, Google's 0.6 threshold, and the CS+ id."""
        mask = CloudScorePlus()
        assert mask.band == "cs"
        assert mask.threshold == 0.6
        assert mask.cs_collection == _CS_ID

    def test_is_an_image_mask(self):
        """The mask subclasses `ImageMask`, so it is a `cloud_mask=` drop-in."""
        assert isinstance(CloudScorePlus(), ImageMask)

    def test_links_score_band_and_thresholds(self, fake_ee):
        """The CS+ collection is linked by the score band and masked by `gte`."""
        image = _FakeS2Image()
        out = CloudScorePlus()(image)
        assert image.recorder["link"] == (("ic", _CS_ID), ["cs"])
        assert image.recorder["selects"] == ["cs"]
        assert image.recorder["threshold"] == 0.6
        assert image.recorder["mask"] == ("gte", 0.6)
        assert out.masked_from is not None

    def test_cs_cdf_band_and_custom_threshold(self, fake_ee):
        """`band="cs_cdf"` links that band and masks by the given threshold."""
        image = _FakeS2Image()
        CloudScorePlus(band="cs_cdf", threshold=0.5)(image)
        assert image.recorder["link"] == (("ic", _CS_ID), ["cs_cdf"])
        assert image.recorder["selects"] == ["cs_cdf"]
        assert image.recorder["mask"] == ("gte", 0.5)

    def test_custom_collection_id(self, fake_ee):
        """An overridden `cs_collection` is the linked collection."""
        image = _FakeS2Image()
        CloudScorePlus(cs_collection="MY/CS")(image)
        assert image.recorder["link"] == (("ic", "MY/CS"), ["cs"])

    def test_masked_image_keeps_properties(self, fake_ee):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeS2Image()
        out = CloudScorePlus()(image)
        assert out.properties is image.properties

    @pytest.mark.parametrize("threshold", [0, 1])
    def test_boundary_thresholds_accepted(self, threshold):
        """The inclusive bounds of `[0, 1]` construct fine."""
        assert CloudScorePlus(threshold=threshold).threshold == threshold

    @pytest.mark.parametrize("threshold", [-0.01, 1.01])
    def test_out_of_range_threshold_rejected(self, threshold):
        """A threshold outside `[0, 1]` raises at construction."""
        with pytest.raises(ValidationError, match=r"threshold must be in \[0, 1\]"):
            CloudScorePlus(threshold=threshold)

    def test_unknown_band_rejected(self):
        """A band outside `cs` / `cs_cdf` raises at construction."""
        with pytest.raises(ValidationError):
            CloudScorePlus(band="probability")

    def test_instance_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=CloudScorePlus()).needs_server_side_compute
