"""Tests for `TemporalReducer` and the `temporal_reducer_for` resolver."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from earthlens.gee import reducers as reducers_module
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
    SensSlope,
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
            sensSlope=lambda: "sens-slope-reducer",
        )
    )
    monkeypatch.setattr(reducers_module, "ee", fake)
    return fake


class _TrendGraph:
    """What the trend / sens-slope Earth Engine call graph recorded."""

    def __init__(self, band_names):
        self.band_names = list(band_names)
        self.time_source = None
        self.time_divisor = None
        self.time_band_name = None
        self.reduce_input = None
        self.reducer = None
        self.slope_band = None
        self.final_rename = None


class _GraphBand:
    """The band `metadata(...)` returns, carrying the time-axis scaling."""

    def __init__(self, graph):
        self._graph = graph

    def divide(self, divisor):
        self._graph.time_divisor = divisor
        return self

    def rename(self, name):
        self._graph.time_band_name = name
        return self


class _GraphImage:
    """A stamped scene, or the per-band slope image after `reduce`."""

    def __init__(self, graph):
        self._graph = graph

    def metadata(self, field):  # noqa: N802 - mirrors the Earth Engine method
        self._graph.time_source = field
        return _GraphBand(self._graph)

    def addBands(self, band):  # noqa: N802 - mirrors the Earth Engine method
        return self

    def select(self, bands):
        self._graph.slope_band = list(bands)
        return self

    def rename(self, names):
        return self


class _GraphReducible:
    """The two-band `[time, value]` collection handed to the reducer."""

    def __init__(self, graph):
        self._graph = graph

    def reduce(self, reducer):
        self._graph.reducer = reducer
        return _GraphImage(self._graph)


class _GraphStacked:
    """The collection after the time band is mapped onto every scene."""

    def __init__(self, graph):
        self._graph = graph

    def select(self, bands):
        self._graph.reduce_input = list(bands)
        return _GraphReducible(self._graph)


class _GraphList:
    """Stands in for an `ee.List` and the server-side band-name list."""

    def __init__(self, graph, items):
        self._graph = graph
        self._items = list(items)

    def add(self, item):
        self._items.append(item)
        return self

    def map(self, func):
        func("VALUE")
        return _GraphList(self._graph, ["slope-image"])

    def __iter__(self):
        return iter(self._items)


class _GraphComposite:
    """The collection `fromImages` builds, flattened back to the value bands."""

    def __init__(self, graph):
        self._graph = graph

    def toBands(self):  # noqa: N802 - mirrors the Earth Engine method
        return self

    def rename(self, names):
        self._graph.final_rename = list(names)
        return self


class _GraphCollection:
    """The source collection the trend path reduces."""

    def __init__(self, graph):
        self._graph = graph

    def first(self):
        return self

    def bandNames(self):  # noqa: N802 - mirrors the Earth Engine method
        return _GraphList(self._graph, self._graph.band_names)

    def map(self, func):
        func(_GraphImage(self._graph))
        return _GraphStacked(self._graph)


def _trend_fake_ee(graph):
    """An `ee` stand-in that records the trend path's call graph into `graph`."""
    return SimpleNamespace(
        Image=lambda image: (
            image if isinstance(image, _GraphImage) else _GraphImage(graph)
        ),
        String=lambda value: value,
        List=lambda items: _GraphList(graph, items),
        ImageCollection=SimpleNamespace(
            fromImages=lambda images: _GraphComposite(graph)
        ),
        Reducer=SimpleNamespace(
            linearFit=lambda: "linear-fit-reducer",
            sensSlope=lambda: "sens-slope-reducer",
        ),
    )


class TestTrendStampsTimeBand:
    """The EE trend path fits a slope over time, not band-against-band."""

    def test_linear_trend_stamps_time_and_fits_time_value(self, monkeypatch):
        """`trend` adds a day-scaled time band and reduces `[time, value]`."""
        graph = _TrendGraph(["B4", "B3"])
        monkeypatch.setattr(reducers_module, "ee", _trend_fake_ee(graph))
        LinearTrend().apply_ee(_GraphCollection(graph))
        assert graph.time_source == "system:time_start"
        assert graph.time_divisor == 1000 * 60 * 60 * 24
        assert graph.time_band_name == "time"
        assert graph.reduce_input == ["time", "VALUE"]
        assert graph.reducer == "linear-fit-reducer"
        assert graph.slope_band == ["scale"]
        assert graph.final_rename == ["B4", "B3"]

    def test_sens_slope_stamps_time_and_selects_the_slope_band(self, monkeypatch):
        """`sens_slope` uses `sensSlope` and selects its `slope` output band."""
        graph = _TrendGraph(["B4", "B3"])
        monkeypatch.setattr(reducers_module, "ee", _trend_fake_ee(graph))
        SensSlope().apply_ee(_GraphCollection(graph))
        assert graph.time_source == "system:time_start"
        assert graph.reduce_input == ["time", "VALUE"]
        assert graph.reducer == "sens-slope-reducer"
        assert graph.slope_band == ["slope"]
        assert graph.final_rename == ["B4", "B3"]


class TestTemporalReducerHierarchy:
    """The convenience and parameterised reducers are `TemporalReducer`s."""

    @pytest.mark.parametrize("cls", list(_CONVENIENCE.values()))
    def test_convenience_reducers_are_temporal(self, cls):
        """Each band-name-preserving reducer is a `TemporalReducer`."""
        assert isinstance(cls(), TemporalReducer)

    def test_percentile_and_trend_are_temporal(self):
        """`Percentile`, `LinearTrend` and `SensSlope` are `TemporalReducer`s too."""
        assert isinstance(Percentile(q=90), TemporalReducer)
        assert isinstance(LinearTrend(), TemporalReducer)
        assert isinstance(SensSlope(), TemporalReducer)


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

    def test_trend_fits_a_time_band(self, monkeypatch):
        """`trend` fits `[time, value]` with `linearFit`, not band-against-band."""
        graph = _TrendGraph(["B4"])
        monkeypatch.setattr(reducers_module, "ee", _trend_fake_ee(graph))
        LinearTrend().apply_ee(_GraphCollection(graph))
        assert graph.reduce_input == ["time", "VALUE"]
        assert graph.reducer == "linear-fit-reducer"
        assert graph.slope_band == ["scale"]

    def test_sens_slope_fits_a_time_band(self, monkeypatch):
        """`sens_slope` fits `[time, value]` with `sensSlope`, selecting `slope`."""
        graph = _TrendGraph(["B4"])
        monkeypatch.setattr(reducers_module, "ee", _trend_fake_ee(graph))
        SensSlope().apply_ee(_GraphCollection(graph))
        assert graph.reduce_input == ["time", "VALUE"]
        assert graph.reducer == "sens-slope-reducer"
        assert graph.slope_band == ["slope"]


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

    def test_sens_slope_over_the_stack(self, tmp_path):
        """A rising stack has a positive per-pixel Theil-Sen slope."""
        stack = _stack(tmp_path, [1.0, 5.0, 9.0])
        reduced = temporal_reducer_for("sens_slope").reduce_stack(stack, _TIMES)
        assert np.asarray(reduced.read_array()).ravel()[0] > 0

    def test_sens_slope_matches_pyramids_reduce_time(self, tmp_path):
        """`reduce_stack` routes to pyramids' `op="sens_slope"` over one window."""
        import pandas as pd

        values = [1.0, 5.0, 2.0, 9.0]
        times = ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01"]
        left = tmp_path / "reduced"
        right = tmp_path / "expected"
        left.mkdir()
        right.mkdir()
        reduced = SensSlope().reduce_stack(_stack(left, values), times)

        index = pd.DatetimeIndex(pd.to_datetime(times))
        span_days = int((index.max() - index.min()).days) + 1
        expected = _stack(right, values).reduce_time(
            times, freq=f"{span_days}D", op="sens_slope", q=None
        )[0][1]
        np.testing.assert_allclose(
            np.asarray(reduced.read_array()), np.asarray(expected.read_array())
        )

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

    def test_resolves_sens_slope(self):
        """`sens_slope` resolves to a `SensSlope`, which is not in the registry."""
        reducer = temporal_reducer_for("sens_slope")
        assert isinstance(reducer, SensSlope)
        assert reducer.name == "sens_slope"

    def test_passes_through_an_instance(self):
        """An already-built `TemporalReducer` is returned unchanged."""
        reducer = Percentile(q=42)
        assert temporal_reducer_for(reducer) is reducer

    @pytest.mark.parametrize("spec", ["p0", "p150"])
    def test_rejects_out_of_range_shorthand(self, spec):
        """The shorthand needs `0 < q <= 100`."""
        with pytest.raises(ValueError, match="0 < q <= 100"):
            temporal_reducer_for(spec)

    @pytest.mark.parametrize("spec", ["percentile:0", "percentile:150"])
    def test_rejects_out_of_range_percentile_spec(self, spec):
        """The `percentile:<q>` spec rejects `q=0` / `q>100` at construction."""
        with pytest.raises(ValueError):
            temporal_reducer_for(spec)


class TestPercentileRange:
    """`Percentile` enforces `0 <= q <= 100`; the temporal path adds `0 < q`."""

    @pytest.mark.parametrize("q", [-1, 150])
    def test_out_of_range_is_refused_at_construction(self, q):
        """A negative percentile and `q>100` raise a `ValidationError`.

        `q=0` is accepted at construction — it is the valid region minimum; the
        temporal resolver is what refuses it (see `TestTemporalReducerFor`).
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Percentile(q=q)

    @pytest.mark.parametrize("spec", ["percentile:-1", "percentile:150"])
    def test_resolve_reducer_rejects_out_of_range_spec(self, spec):
        """`resolve_reducer` rejects below-zero and above-100, but allows `q=0`."""
        from earthlens.gee.reducers import resolve_reducer

        with pytest.raises(ValueError):
            resolve_reducer(spec)

    def test_resolve_reducer_allows_the_zero_percentile(self):
        """`resolve_reducer` keeps `percentile:0` — the region minimum."""
        from earthlens.gee.reducers import resolve_reducer

        assert resolve_reducer("percentile:0").q == 0.0

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
