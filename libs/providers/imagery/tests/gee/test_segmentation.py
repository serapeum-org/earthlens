"""Tests for the LandTrendr and CCDC temporal-segmentation configs."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import segmentation as seg_module  # noqa: E402
from earthlens.gee.segmentation import (  # noqa: E402
    Ccdc,
    LandTrendr,
    SegmentationResult,
    TemporalSegmentation,
)


class _Rec:
    """A recording stand-in whose every method returns itself.

    Each call appends `(name, args, kwargs)` to the shared `log`, so a test can
    assert the Earth Engine call graph a config builds without a live round
    trip.
    """

    def __init__(self, log: list, tag: str = "image") -> None:
        self.log = log
        self.tag = tag

    def __getattr__(self, name: str):
        def _call(*args: Any, **kwargs: Any):
            self.log.append((name, args, kwargs))
            return self

        return _call


class _FakeCollection:
    """An input collection whose `map` applies the function to one fake image."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.mapped: list = []

    def map(self, fn):
        image = _Rec(self.log, tag="scene")
        self.mapped.append(fn(image))
        return self


class _FakeTemporalSegmentation:
    """Records the LandTrendr / Ccdc call the config dispatches to."""

    def __init__(self) -> None:
        self.landtrendr_calls: list = []
        self.ccdc_calls: list = []

    def LandTrendr(self, **kwargs):  # noqa: N802 - mirrors the ee API name
        self.landtrendr_calls.append(kwargs)
        return "landtrendr-image"

    def Ccdc(self, **kwargs):  # noqa: N802 - mirrors the ee API name
        self.ccdc_calls.append(kwargs)
        return "ccdc-image"


class _FakeAlgorithms:
    """Stands in for `ee.Algorithms`, exposing the `TemporalSegmentation` group."""

    def __init__(self, segmentation: _FakeTemporalSegmentation) -> None:
        self.TemporalSegmentation = segmentation


@pytest.fixture
def fake_ee(monkeypatch):
    """Replace the module's `ee` with a recorder for the algorithm calls."""
    algorithms = _FakeTemporalSegmentation()
    log: list = []
    monkeypatch.setattr(
        seg_module.ee, "Algorithms", _FakeAlgorithms(algorithms), raising=False
    )
    monkeypatch.setattr(
        seg_module.ee, "Image", lambda value: _Rec(log, tag="wrapped"), raising=False
    )
    return algorithms


class TestLandTrendrConfig:
    """The LandTrendr config carries Earth Engine's documented defaults."""

    def test_defaults_match_the_earth_engine_docs(self):
        """Each default is the value Earth Engine documents."""
        lt = LandTrendr(maxSegments=6)
        assert lt.spikeThreshold == 0.9
        assert lt.vertexCountOvershoot == 3
        assert lt.preventOneYearRecovery is False
        assert lt.recoveryThreshold == 0.25
        assert lt.pvalThreshold == 0.1
        assert lt.bestModelProportion == 0.75
        assert lt.minObservationsNeeded == 6

    def test_max_segments_is_required(self):
        """Earth Engine declares no default, so the field is required."""
        with pytest.raises(ValidationError):
            LandTrendr()

    def test_out_of_range_thresholds_are_refused(self):
        """A probability outside 0-1 fails closed at construction."""
        with pytest.raises(ValidationError):
            LandTrendr(maxSegments=6, pvalThreshold=1.5)

    def test_the_config_is_frozen(self):
        """It is a value object; a parameter cannot be reassigned."""
        with pytest.raises(ValidationError):
            LandTrendr(maxSegments=6).maxSegments = 3

    def test_unknown_parameters_are_refused(self):
        """A typo'd Earth Engine parameter name is caught, not ignored."""
        with pytest.raises(ValidationError):
            LandTrendr(maxSegments=6, spikethreshold=0.5)


class TestLandTrendrCallGraph:
    """`segment()` builds the documented Earth Engine call."""

    def test_the_parameters_are_passed_by_their_ee_names(self, fake_ee):
        """Every field reaches the algorithm under Earth Engine's own spelling."""
        log: list = []
        LandTrendr(maxSegments=4, invert=False).segment(_FakeCollection(log))
        (kwargs,) = fake_ee.landtrendr_calls
        assert kwargs["maxSegments"] == 4
        assert kwargs["spikeThreshold"] == 0.9
        assert kwargs["recoveryThreshold"] == 0.25
        assert set(kwargs) == {
            "timeSeries",
            "maxSegments",
            "spikeThreshold",
            "vertexCountOvershoot",
            "preventOneYearRecovery",
            "recoveryThreshold",
            "pvalThreshold",
            "bestModelProportion",
            "minObservationsNeeded",
        }

    def test_the_result_names_the_algorithm_and_array_band(self, fake_ee):
        """The output is wrapped, not handed back as a raw image."""
        result = LandTrendr(maxSegments=4, invert=False).segment(_FakeCollection([]))
        assert isinstance(result, SegmentationResult)
        assert result.algorithm == "LandTrendr"
        assert result.array_band == "LandTrendr"
        assert result.image == "landtrendr-image"


class TestInvertSignConvention:
    """The disturbance-sign trap is explicit and negates the series when set."""

    def test_invert_true_negates_each_scene(self, fake_ee):
        """A vegetation index is multiplied by -1 before fitting."""
        log: list = []
        collection = _FakeCollection(log)
        LandTrendr(maxSegments=4, invert=True).segment(collection)
        multiplies = [args for name, args, _ in log if name == "multiply"]
        assert multiplies == [(-1,)]
        assert any(name == "copyProperties" for name, _, _ in log)

    def test_invert_false_leaves_the_series_untouched(self, fake_ee):
        """A wetness/burn index that already rises on disturbance is not negated."""
        log: list = []
        collection = _FakeCollection(log)
        LandTrendr(maxSegments=4, invert=False).segment(collection)
        assert collection.mapped == []
        assert [name for name, _, _ in log if name == "multiply"] == []

    def test_landtrendr_defaults_to_negating(self):
        """The common NDVI/NBR case is the default, so it is not a silent trap."""
        assert LandTrendr(maxSegments=6).invert is True

    def test_ccdc_defaults_to_not_negating(self):
        """CCDC change detection is direction-agnostic, so invert is off."""
        assert Ccdc().invert is False


class TestCcdc:
    """The CCDC config takes a parameter dictionary, splatted into the call."""

    def test_the_params_dict_reaches_the_algorithm(self, fake_ee):
        """Each key is forwarded verbatim alongside the collection."""
        params = {"breakpointBands": ["B3", "B4"], "minObservations": 6}
        Ccdc(params=params).segment(_FakeCollection([]))
        (kwargs,) = fake_ee.ccdc_calls
        assert kwargs["breakpointBands"] == ["B3", "B4"]
        assert kwargs["minObservations"] == 6
        assert "collection" in kwargs

    def test_the_result_is_wrapped_with_the_ccdc_array_band(self, fake_ee):
        """CCDC's array output is surfaced through the same result object."""
        result = Ccdc(params={"breakpointBands": ["B4"]}).segment(_FakeCollection([]))
        assert result.algorithm == "Ccdc"
        assert result.array_band == "tStart"
        assert result.image == "ccdc-image"

    def test_ccdc_honours_invert_when_set(self, fake_ee):
        """Parity: an explicitly inverted CCDC still negates the series."""
        log: list = []
        Ccdc(invert=True, params={"breakpointBands": ["B4"]}).segment(
            _FakeCollection(log)
        )
        assert [args for name, args, _ in log if name == "multiply"] == [(-1,)]


class TestSegmentationResult:
    """The result object's accessors and the documented client hand-off."""

    def test_array_selects_the_primary_band(self):
        """`array()` forwards to `image.select(array_band)`."""
        calls: list = []
        image = _Rec(calls)
        result = SegmentationResult(
            image=image, algorithm="LandTrendr", array_band="LandTrendr"
        )
        result.array()
        assert ("select", ("LandTrendr",), {}) in calls

    def test_band_selects_a_named_band(self):
        """`band(name)` forwards to `image.select(name)`."""
        calls: list = []
        image = _Rec(calls)
        result = SegmentationResult(image=image, algorithm="Ccdc", array_band="tStart")
        result.band("rmse")
        assert ("select", ("rmse",), {}) in calls

    def test_client_is_the_raw_image(self):
        """`.client` is the documented hand-off to the untouched `ee.Image`."""
        image = object()
        result = SegmentationResult(
            image=image, algorithm="LandTrendr", array_band="LandTrendr"
        )
        assert result.client is image


class TestBaseIsAbstract:
    """The base cannot be instantiated or used without a concrete `_run`."""

    def test_base_cannot_be_constructed(self):
        """`TemporalSegmentation` is abstract; a concrete subclass is required."""
        with pytest.raises(TypeError):
            TemporalSegmentation()
