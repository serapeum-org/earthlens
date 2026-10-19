"""Tests for `TemporalReducer` and the `temporal_reducer_for` resolver."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from earthlens.gee.reducers import (
    LinearTrend,
    Max,
    Mean,
    Median,
    Min,
    Mode,
    Mosaic,
    Percentile,
    Reducer,
    Sum,
    TemporalReducer,
    temporal_reducer_for,
)

_CONVENIENCE = {
    "mean": Mean,
    "median": Median,
    "min": Min,
    "max": Max,
    "mode": Mode,
    "mosaic": Mosaic,
    "sum": Sum,
}


class _FakeCollection:
    """Records the convenience method dispatched to, and the generic reduce."""

    def __init__(self, band_names=("B4", "B3")):
        self.called: list[str] = []
        self.reduced_with = None
        self._band_names = list(band_names)

    def first(self):
        return self

    def bandNames(self):  # noqa: N802 - mirrors the Earth Engine method name
        return self._band_names

    def reduce(self, reducer):
        self.reduced_with = reducer
        return _FakeReduced(self)

    def __getattr__(self, item):
        def _call():
            self.called.append(item)
            return f"image-{item}"

        return _call


class _FakeReduced:
    """The image `reduce` returns, tracking the rename back to original bands."""

    def __init__(self, collection):
        self.collection = collection
        self.renamed_to = None

    def rename(self, names):
        self.renamed_to = names
        return self


def _stack(tmp_path, values):
    """Build a file-backed `DatasetCollection` of constant-valued rasters."""
    from pyramids.dataset import Dataset, DatasetCollection
    from pyramids.dataset.dataset import GeoReference

    geo = GeoReference(top_left_corner=(0.0, 2.0), cell_size=1.0, epsg=4326)
    paths = []
    for i, value in enumerate(values):
        path = str(tmp_path / f"t{i}.tif")
        Dataset.from_array(
            np.full((2, 2), value), geo_ref=geo, no_data_value=-9999.0, path=path
        )
        paths.append(path)
    return DatasetCollection.from_files(paths)


_TIMES = ["2020-01-01", "2020-02-01", "2020-03-01"]


@pytest.fixture
def fake_ee_reducer(monkeypatch):
    """Patch the reducers module's `ee` binding so no EE init is needed."""
    from earthlens.gee import reducers as reducers_module

    fake = SimpleNamespace(
        Reducer=SimpleNamespace(
            percentile=lambda qs: f"percentile-reducer-{qs}",
            linearFit=lambda: "linear-fit-reducer",
        )
    )
    monkeypatch.setattr(reducers_module, "ee", fake)
    return fake


class TestTemporalReducerHierarchy:
    """The convenience and parameterised reducers are `TemporalReducer`s."""

    @pytest.mark.parametrize("cls", list(_CONVENIENCE.values()))
    def test_convenience_reducers_are_temporal(self, cls):
        """Each band-name-preserving reducer is a `TemporalReducer`."""
        assert isinstance(cls(), TemporalReducer)

    def test_percentile_and_trend_are_temporal(self):
        """`Percentile` and `LinearTrend` are `TemporalReducer`s too."""
        assert isinstance(Percentile(q=90), TemporalReducer)
        assert isinstance(LinearTrend(), TemporalReducer)


class TestApplyEe:
    """`apply_ee` collapses an `ee.ImageCollection` to one `ee.Image`."""

    @pytest.mark.parametrize("name", [n for n in _CONVENIENCE if n != "mosaic"])
    def test_zero_arg_names_dispatch_to_convenience_method(self, name):
        """The seven names call the collection's own band-preserving method."""
        collection = _FakeCollection()
        image = temporal_reducer_for(name).apply_ee(collection)
        assert image == f"image-{name}"
        assert collection.called == [name]

    def test_apply_ee_matches_calling_the_reducer(self):
        """`apply_ee` is the same dispatch the old `reducer(collection)` used."""
        collection = _FakeCollection()
        assert Mean().apply_ee(collection) == Mean()(_FakeCollection())

    def test_percentile_restores_original_band_names(self, fake_ee_reducer):
        """A percentile reduces generically, then renames bands back."""
        collection = _FakeCollection(band_names=["SR_B4", "SR_B3"])
        reduced = Percentile(q=90).apply_ee(collection)
        assert collection.reduced_with == "percentile-reducer-[90.0]"
        assert reduced.renamed_to == ["SR_B4", "SR_B3"]

    def test_trend_uses_linear_fit(self, fake_ee_reducer):
        """`trend` reduces with `ee.Reducer.linearFit()` (no rename)."""
        collection = _FakeCollection()
        reduced = LinearTrend().apply_ee(collection)
        assert collection.reduced_with == "linear-fit-reducer"
        assert reduced.renamed_to is None


class TestReduceStack:
    """`reduce_stack` collapses a pyramids scene stack client-side."""

    def test_median_over_the_stack(self, tmp_path):
        """The median of [1, 5, 9] per pixel is 5."""
        stack = _stack(tmp_path, [1.0, 5.0, 9.0])
        reduced = temporal_reducer_for("median").reduce_stack(stack, _TIMES)
        assert np.asarray(reduced.read_array()).ravel()[0] == pytest.approx(5.0)

    def test_percentile_over_the_stack(self, tmp_path):
        """The 90th percentile of [1, 5, 9] per pixel is 8.2."""
        stack = _stack(tmp_path, [1.0, 5.0, 9.0])
        reduced = temporal_reducer_for("p90").reduce_stack(stack, _TIMES)
        assert np.asarray(reduced.read_array()).ravel()[0] == pytest.approx(8.2)

    def test_trend_over_the_stack(self, tmp_path):
        """A rising stack has a positive per-pixel slope."""
        stack = _stack(tmp_path, [1.0, 5.0, 9.0])
        reduced = temporal_reducer_for("trend").reduce_stack(stack, _TIMES)
        assert np.asarray(reduced.read_array()).ravel()[0] > 0

    def test_single_timestep_stack(self, tmp_path):
        """A one-scene stack still reduces to one whole-window dataset."""
        stack = _stack(tmp_path, [7.0])
        reduced = temporal_reducer_for("mean").reduce_stack(stack, ["2020-01-01"])
        assert np.asarray(reduced.read_array()).ravel()[0] == pytest.approx(7.0)

    def test_mosaic_has_no_client_side_form(self, tmp_path):
        """`mosaic` refuses a client-side stack reduction."""
        stack = _stack(tmp_path, [1.0, 5.0])
        with pytest.raises(ValueError, match="last-wins"):
            Mosaic().reduce_stack(stack, ["2020-01-01", "2020-02-01"])


class TestTemporalReducerFor:
    """The resolver accepts names, specs, the shorthand, and instances."""

    @pytest.mark.parametrize("name", list(_CONVENIENCE))
    def test_resolves_each_convenience_name(self, name):
        """Each of the seven names resolves to its `TemporalReducer`."""
        reducer = temporal_reducer_for(name)
        assert isinstance(reducer, TemporalReducer)
        assert reducer.name == name

    def test_resolves_percentile_spec(self):
        """A `percentile:<q>` spec resolves to a `Percentile`."""
        reducer = temporal_reducer_for("percentile:25")
        assert isinstance(reducer, Percentile)
        assert reducer.q == 25.0

    def test_resolves_percentile_shorthand(self):
        """A `p<q>` shorthand resolves to a `Percentile`."""
        assert temporal_reducer_for("p90").q == 90.0
        assert temporal_reducer_for("p100").q == 100.0

    def test_resolves_trend(self):
        """`trend` resolves to a `LinearTrend`, which is not in the registry."""
        assert isinstance(temporal_reducer_for("trend"), LinearTrend)

    def test_passes_through_an_instance(self):
        """An already-built `TemporalReducer` is returned unchanged."""
        reducer = Percentile(q=42)
        assert temporal_reducer_for(reducer) is reducer

    @pytest.mark.parametrize("spec", ["p0", "p150"])
    def test_rejects_out_of_range_shorthand(self, spec):
        """The shorthand needs `0 < q <= 100`."""
        with pytest.raises(ValueError, match="0 < q <= 100"):
            temporal_reducer_for(spec)

    def test_rejects_region_only_name(self):
        """A region-only name has no time-axis form."""
        with pytest.raises(ValueError, match="region"):
            temporal_reducer_for("stdDev")

    def test_rejects_region_only_instance(self):
        """A region-only instance is refused with the same reason."""
        from earthlens.gee.reducers import First

        with pytest.raises(ValueError, match="region"):
            temporal_reducer_for(First())

    def test_rejects_unknown_name(self):
        """An unknown name raises through `resolve_reducer`."""
        with pytest.raises(ValueError, match="unsupported reducer"):
            temporal_reducer_for("average")

    def test_rejects_non_string_non_reducer(self):
        """A value that is neither a name nor a `Reducer` is a `TypeError`."""
        with pytest.raises(TypeError, match="name or a Reducer"):
            temporal_reducer_for(5)

    def test_region_reducer_is_a_reducer_but_not_temporal(self):
        """The refused instance really is a `Reducer`, just not a temporal one."""
        from earthlens.gee.reducers import StdDev

        assert isinstance(StdDev(), Reducer)
        assert not isinstance(StdDev(), TemporalReducer)
