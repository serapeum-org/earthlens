"""Tests for the widened composite reducer vocabulary (#1214).

Covers the reducers that gained a server-side composite form — `stdDev`,
`variance`, `count` (reduce-and-rename) — and the new parameterised
`qualityMosaic`. `ee.Reducer` and the collection are faked so each test
asserts the exact server-side call, and the EEDAI decline is checked so
`engine="auto"` routes these to Earth Engine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("ee")

from earthlens.gee import reducers as reducers_module  # noqa: E402
from earthlens.gee.reducers import (  # noqa: E402
    Count,
    QualityMosaic,
    StdDev,
    TemporalReducer,
    Variance,
    collection_reducers,
    temporal_reducer_for,
)


class _FakeReduced:
    """The `collection.reduce(...)` output; records the rename-back."""

    def __init__(self, recorder):
        self._recorder = recorder

    def rename(self, names):
        self._recorder["renamed_to"] = names
        return ("reduced", names)


class _FakeFirst:
    """`collection.first()` — carries the original band names."""

    def bandNames(self):  # noqa: N802 - mirrors ee API
        return ["B4", "B3"]


class _FakeCollection:
    """Records the `reduce` reducer and the `qualityMosaic` band."""

    def __init__(self):
        self.recorder: dict = {}

    def first(self):
        return _FakeFirst()

    def reduce(self, reducer):
        self._recorder_reduce(reducer)
        return _FakeReduced(self.recorder)

    def _recorder_reduce(self, reducer):
        self.recorder["reduced_with"] = reducer

    def qualityMosaic(self, band):  # noqa: N802 - mirrors ee API
        self.recorder["quality_band"] = band
        return ("quality-mosaic", band)


@pytest.fixture
def fake_ee(monkeypatch):
    """Fake `ee.Reducer.<name>()` so `as_ee_reducer` yields a sentinel."""
    reducer_ns = SimpleNamespace(
        stdDev=lambda: ("ee.Reducer", "stdDev"),
        variance=lambda: ("ee.Reducer", "variance"),
        count=lambda: ("ee.Reducer", "count"),
    )
    monkeypatch.setattr(reducers_module, "ee", SimpleNamespace(Reducer=reducer_ns))


class TestReduceRenameComposites:
    """`stdDev` / `variance` / `count` reduce, then rename bands back."""

    @pytest.mark.parametrize(
        ("reducer", "factory"),
        [(StdDev(), "stdDev"), (Variance(), "variance"), (Count(), "count")],
    )
    def test_reduces_with_the_factory_and_renames_back(self, fake_ee, reducer, factory):
        """The composite is `collection.reduce(ee.Reducer.X()).rename(bands)`."""
        collection = _FakeCollection()
        out = reducer(collection)
        assert collection.recorder["reduced_with"] == ("ee.Reducer", factory), (
            f"{factory} should reduce with ee.Reducer.{factory}()"
        )
        assert collection.recorder["renamed_to"] == ["B4", "B3"], (
            "the _<reducer> band suffixes must be renamed back to the originals"
        )
        assert out == ("reduced", ["B4", "B3"])

    @pytest.mark.parametrize("name", ["stdDev", "variance", "count"])
    def test_are_in_the_collection_vocabulary(self, name):
        """Each now reports as collection-capable, so `GEE(reducer=)` accepts it."""
        assert name in collection_reducers()

    @pytest.mark.parametrize("name", ["stdDev", "variance", "count"])
    def test_resolve_to_temporal_reducers(self, name):
        """`temporal_reducer_for` returns them instead of refusing."""
        assert isinstance(temporal_reducer_for(name), TemporalReducer)

    @pytest.mark.parametrize("reducer", [StdDev(), Variance(), Count()])
    def test_have_no_client_side_reduce_stack(self, reducer):
        """The EEDAI client-side path has no form for these."""
        with pytest.raises(ValueError, match="no client-side equivalent"):
            reducer.reduce_stack(object(), ["2020-01-01"])


class TestQualityMosaic:
    """The greenest-pixel composite picks each pixel's best quality band."""

    def test_composites_by_the_quality_band(self, fake_ee):
        """`QualityMosaic(quality_band=b)` calls `collection.qualityMosaic(b)`."""
        collection = _FakeCollection()
        out = QualityMosaic(quality_band="NDVI")(collection)
        assert collection.recorder["quality_band"] == "NDVI"
        assert out == ("quality-mosaic", "NDVI")

    def test_resolves_from_the_spec(self):
        """`qualityMosaic:<band>` resolves to a QualityMosaic on that band."""
        reducer = temporal_reducer_for("qualityMosaic:NDVI")
        assert isinstance(reducer, QualityMosaic)
        assert reducer.quality_band == "NDVI"

    def test_spec_without_a_band_is_refused(self):
        """A bare `qualityMosaic:` names no band, so it is refused."""
        with pytest.raises(ValueError, match="needs a quality band"):
            temporal_reducer_for("qualityMosaic:")

    def test_has_no_region_reducer_form(self):
        """A quality mosaic is a composite, not an `ee.Reducer` region form."""
        with pytest.raises(ValueError, match="not a region reducer"):
            QualityMosaic(quality_band="NDVI").as_ee_reducer()

    def test_has_no_client_side_reduce_stack(self):
        """There is no client-side quality mosaic over a scene stack."""
        with pytest.raises(ValueError, match="no client-side equivalent"):
            QualityMosaic(quality_band="NDVI").reduce_stack(object(), ["2020-01-01"])


class TestEedaiDecline:
    """The reader declines the new composites so engine=auto runs them on EE."""

    @pytest.mark.parametrize("name", ["stdDev", "variance", "count", "qualityMosaic"])
    def test_declined_for_the_reader(self, name):
        """Each new composite is in the EEDAI unsupported-reducer set."""
        from earthlens.gee.backend import _EEDAI_UNSUPPORTED_REDUCERS

        assert name in _EEDAI_UNSUPPORTED_REDUCERS
