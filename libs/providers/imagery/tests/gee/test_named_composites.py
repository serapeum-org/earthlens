"""Tests for the Landsat / Sentinel-2 named-algorithm composites (Task 6.2)."""

from __future__ import annotations

import functools
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from earthlens.gee import _helpers as helpers_module
from earthlens.gee import cloud_masks as cloud_masks_module
from earthlens.gee import reducers as reducers_module
from earthlens.gee.cloud_masks import ImageMask, Sentinel2Cdi
from earthlens.gee.configs import EngineConfig
from earthlens.gee.reducers import (
    REDUCERS,
    LandsatComposite,
    Mean,
    Median,
    Sentinel2Composite,
    TemporalReducer,
    temporal_reducer_for,
)


def _refuse_every_threshold(value: float) -> float:
    """Stand in for the shared CDI check, refusing whatever it is given."""
    raise ValueError(f"patched check refuses {value!r}")


class _FakeCdiBand:
    """The single-band CDI image, recording the threshold comparison."""

    def gte(self, threshold):
        return ("cdi-gte", threshold)


class _FakeEe:
    """An `ee` stand-in whose named algorithms record their inputs."""

    def __init__(self):
        self.simple_composite_kwargs = None
        self.cdi_images: list = []
        self.Algorithms = SimpleNamespace(
            Landsat=SimpleNamespace(simpleComposite=self._simple_composite),
            Sentinel2=SimpleNamespace(CDI=self._cdi),
        )

    def _simple_composite(self, **kwargs):
        self.simple_composite_kwargs = kwargs
        return "landsat-composite-image"

    def _cdi(self, image):
        self.cdi_images.append(image)
        return _FakeCdiBand()


class _FakeImage:
    """Recorder image whose `updateMask` output shares the source properties."""

    def __init__(self, properties=None):
        self.properties = (
            properties if properties is not None else {"system:time_start": 1718236800}
        )
        self.mask = None
        self.masked_from = None

    def updateMask(self, mask):  # noqa: N802 - mirrors the Earth Engine method
        self.mask = mask
        derived = _FakeImage(properties=self.properties)
        derived.masked_from = self
        return derived


class _FakeMapped:
    """The collection after the CDI mask is mapped; records the reducer call."""

    def __init__(self):
        self.called: list[str] = []

    def _record(self, item):
        self.called.append(item)
        return f"image-{item}"

    def __getattr__(self, item):
        return functools.partial(self._record, item)


class _FakeS2Collection:
    """Records the function `.map` was handed and returns the mapped stage."""

    def __init__(self):
        self.mapped_with = None
        self.mapped = _FakeMapped()

    def map(self, func):
        self.mapped_with = func
        return self.mapped


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the reducers and cloud-mask modules' `ee` with the recorder."""
    fake = _FakeEe()
    monkeypatch.setattr(reducers_module, "ee", fake)
    monkeypatch.setattr(cloud_masks_module, "ee", fake)
    return fake


class TestLandsatComposite:
    """Tests for `LandsatComposite`."""

    def test_is_a_temporal_reducer(self):
        """The composite plugs into the `TemporalReducer` seam."""
        assert isinstance(LandsatComposite(), TemporalReducer)

    def test_defaults_reach_the_named_algorithm(self, fake_ee):
        """The documented `simpleComposite` defaults are passed through."""
        image = LandsatComposite()("the-collection")
        assert image == "landsat-composite-image"
        assert fake_ee.simple_composite_kwargs == {
            "collection": "the-collection",
            "percentile": 50,
            "cloudScoreRange": 10.0,
            "maxDepth": 40,
            "asFloat": False,
        }

    def test_config_reaches_the_named_algorithm(self, fake_ee):
        """Every field maps onto its `simpleComposite` keyword."""
        composite = LandsatComposite(
            percentile=75, cloud_score_range=5, max_depth=20, as_float=True
        )
        composite("the-collection")
        assert fake_ee.simple_composite_kwargs == {
            "collection": "the-collection",
            "percentile": 75,
            "cloudScoreRange": 5.0,
            "maxDepth": 20,
            "asFloat": True,
        }

    def test_compose_is_the_call_alias(self, fake_ee):
        """`compose(collection)` runs the same named algorithm."""
        assert LandsatComposite(as_float=True).compose("c") == "landsat-composite-image"
        assert fake_ee.simple_composite_kwargs["asFloat"] is True

    def test_apply_ee_delegates_to_the_call(self, fake_ee):
        """`apply_ee` is the `CompositeEngine` seam and runs the algorithm."""
        assert LandsatComposite().apply_ee("c") == "landsat-composite-image"
        assert fake_ee.simple_composite_kwargs["collection"] == "c"

    def test_reduce_stack_has_no_client_side_form(self):
        """The named algorithm exists only server-side."""
        composite = LandsatComposite()
        with pytest.raises(ValueError, match="named algorithm"):
            composite.reduce_stack("stack", ["2020-01-01"])

    def test_cannot_reduce_a_region(self):
        """There is no `ee.Reducer` equivalent to sample with."""
        composite = LandsatComposite()
        with pytest.raises(ValueError, match="region"):
            composite.as_ee_reducer()

    def test_not_in_the_registry(self):
        """The composites resolve as instances, never by registry name."""
        assert "landsat_composite" not in REDUCERS
        assert "sentinel2_composite" not in REDUCERS

    def test_instance_passes_the_temporal_resolver(self):
        """`temporal_reducer_for` returns the instance unchanged."""
        composite = LandsatComposite()
        assert temporal_reducer_for(composite) is composite

    @pytest.mark.parametrize("value", [-1, 101])
    def test_percentile_out_of_range_refused(self, value):
        """A percentile outside 0-100 is refused at construction."""
        with pytest.raises(ValidationError):
            LandsatComposite(percentile=value)

    def test_rejects_unknown_fields(self):
        """A typo'd keyword is refused at construction."""
        with pytest.raises(ValidationError):
            LandsatComposite(cloudScoreRange=5)

    def test_is_frozen(self):
        """A constructed composite is immutable."""
        composite = LandsatComposite()
        with pytest.raises(ValidationError):
            composite.percentile = 75

    def test_str_names_the_class(self):
        """`str()` matches `repr()` so log lines say which reducer this is."""
        composite = LandsatComposite(percentile=75)
        assert str(composite) == repr(composite)
        assert "LandsatComposite" in str(composite)


class TestSentinel2Cdi:
    """Tests for the `Sentinel2Cdi` mask."""

    def test_is_an_image_mask(self):
        """The CDI mask is an ordinary `cloud_mask=` drop-in."""
        assert isinstance(Sentinel2Cdi(), ImageMask)

    def test_masks_by_the_cdi_threshold(self, fake_ee):
        """The scene is masked where CDI falls below the threshold."""
        image = _FakeImage()
        out = Sentinel2Cdi()(image)
        assert fake_ee.cdi_images == [image]
        assert image.mask == ("cdi-gte", -0.5)
        assert out.masked_from is image

    def test_threshold_is_configurable(self, fake_ee):
        """A custom threshold reaches the comparison."""
        image = _FakeImage()
        Sentinel2Cdi(threshold=0.0)(image)
        assert image.mask == ("cdi-gte", 0.0)

    @pytest.mark.parametrize("value", [-1.5, 1.5])
    def test_threshold_outside_cdi_range_refused(self, value):
        """A threshold outside the `[-1, 1]` CDI range is refused."""
        with pytest.raises(ValidationError, match="-1"):
            Sentinel2Cdi(threshold=value)

    def test_masked_image_keeps_properties(self, fake_ee):
        """The output derives via `updateMask` only, so properties survive."""
        image = _FakeImage(properties={"system:time_start": 42})
        out = Sentinel2Cdi()(image)
        assert out.properties is image.properties

    def test_forces_server_side_compute(self):
        """An instance on `cloud_mask` flips `needs_server_side_compute`."""
        assert EngineConfig(cloud_mask=Sentinel2Cdi()).needs_server_side_compute


class TestSentinel2Composite:
    """Tests for `Sentinel2Composite`."""

    def test_is_a_temporal_reducer(self):
        """The composite plugs into the `TemporalReducer` seam."""
        assert isinstance(Sentinel2Composite(), TemporalReducer)

    def test_maps_the_cdi_mask_then_reduces(self, fake_ee):
        """The collection is CDI-masked per image, then reduced."""
        collection = _FakeS2Collection()
        image = Sentinel2Composite()(collection)
        assert isinstance(collection.mapped_with, Sentinel2Cdi)
        assert collection.mapped_with.threshold == -0.5
        assert collection.mapped.called == ["median"]
        assert image == "image-median"

    def test_threshold_reaches_the_mapped_mask(self, fake_ee):
        """A custom CDI threshold is carried into the mapped mask."""
        collection = _FakeS2Collection()
        Sentinel2Composite(threshold=0.25)(collection)
        assert collection.mapped_with.threshold == 0.25

    def test_the_mapped_mask_masks_one_scene_like_the_standalone(self, fake_ee):
        """The function handed to `.map` behaves as `Sentinel2Cdi` on a scene."""
        collection = _FakeS2Collection()
        Sentinel2Composite()(collection)
        scene = _FakeImage()
        collection.mapped_with(scene)
        assert scene.mask == ("cdi-gte", -0.5)

    def test_reducer_accepts_a_spelling(self, fake_ee):
        """The inner reducer resolves through `temporal_reducer_for`."""
        composite = Sentinel2Composite(reducer="mean")
        assert isinstance(composite.reducer, Mean)
        collection = _FakeS2Collection()
        composite(collection)
        assert collection.mapped.called == ["mean"]

    def test_reducer_accepts_an_instance(self):
        """An already-built inner reducer is kept as-is."""
        inner = Median()
        assert Sentinel2Composite(reducer=inner).reducer is inner

    def test_region_only_inner_reducer_refused(self):
        """A region-only inner reducer has no time-axis form."""
        with pytest.raises(ValidationError, match="region"):
            Sentinel2Composite(reducer="stdDev")

    @pytest.mark.parametrize("value", [-2, 2])
    def test_threshold_outside_cdi_range_refused(self, value):
        """A threshold outside the `[-1, 1]` CDI range is refused."""
        with pytest.raises(ValidationError, match="-1"):
            Sentinel2Composite(threshold=value)

    def test_compose_is_the_call_alias(self, fake_ee):
        """`compose(collection)` maps the mask and reduces, like the call."""
        collection = _FakeS2Collection()
        assert Sentinel2Composite().compose(collection) == "image-median"

    def test_apply_ee_delegates_to_the_call(self, fake_ee):
        """`apply_ee` is the `CompositeEngine` seam."""
        collection = _FakeS2Collection()
        assert Sentinel2Composite().apply_ee(collection) == "image-median"

    def test_reduce_stack_has_no_client_side_form(self):
        """The CDI algorithm exists only server-side."""
        composite = Sentinel2Composite()
        with pytest.raises(ValueError, match="named algorithm"):
            composite.reduce_stack("stack", ["2020-01-01"])

    def test_cannot_reduce_a_region(self):
        """There is no `ee.Reducer` equivalent to sample with."""
        composite = Sentinel2Composite()
        with pytest.raises(ValueError, match="region"):
            composite.as_ee_reducer()

    def test_instance_passes_the_temporal_resolver(self):
        """`temporal_reducer_for` returns the instance unchanged."""
        composite = Sentinel2Composite()
        assert temporal_reducer_for(composite) is composite

    def test_rejects_unknown_fields(self):
        """A typo'd keyword is refused at construction."""
        with pytest.raises(ValidationError):
            Sentinel2Composite(treshold=0.0)


class TestNamedCompositesStayOffTheClientPath:
    """The composites decline every client-side seam the reader could take."""

    @pytest.mark.parametrize(
        "composite", [LandsatComposite(), Sentinel2Composite()], ids=type
    )
    def test_reduce_stack_refusal_names_the_remedy(self, composite):
        """The refusal points at Earth Engine rather than approximating."""
        stack = np.empty(0)
        with pytest.raises(ValueError, match="engine='ee'"):
            composite.reduce_stack(stack, [])


class TestSharedCdiThresholdCheck:
    """`Sentinel2Cdi` and `Sentinel2Composite` share one range check."""

    @pytest.mark.parametrize("value", [-1.5, 1.5, 2])
    def test_the_two_models_refuse_with_the_identical_message(self, value):
        """A shared helper is only worth moving if the text cannot drift."""
        with pytest.raises(ValidationError) as mask_error:
            Sentinel2Cdi(threshold=value)
        with pytest.raises(ValidationError) as composite_error:
            Sentinel2Composite(threshold=value)
        expected = f"threshold must be in [-1, 1], got {float(value)!r}"
        for error in (mask_error, composite_error):
            assert expected in str(error.value), (
                f"expected the shared text {expected!r}, got: {error.value}"
            )

    @pytest.mark.parametrize("value", [-1, 0.0, 1])
    def test_the_inclusive_bounds_are_accepted_by_both(self, value):
        """The range is inclusive, so both endpoints construct on either side."""
        assert Sentinel2Cdi(threshold=value).threshold == float(value)
        assert Sentinel2Composite(threshold=value).threshold == float(value)

    def test_both_models_route_through_the_one_helper(self, monkeypatch):
        """Patching the shared helper changes both, so neither kept a copy."""
        assert (
            cloud_masks_module.checked_cdi_threshold
            is helpers_module.checked_cdi_threshold
            is reducers_module.checked_cdi_threshold
        ), "both modules must hold the one `_helpers` function, not a copy"
        monkeypatch.setattr(
            cloud_masks_module, "checked_cdi_threshold", _refuse_every_threshold
        )
        monkeypatch.setattr(
            reducers_module, "checked_cdi_threshold", _refuse_every_threshold
        )
        with pytest.raises(ValidationError, match="patched"):
            Sentinel2Cdi(threshold=0.0)
        with pytest.raises(ValidationError, match="patched"):
            Sentinel2Composite(threshold=0.0)
