"""End-to-end integration tests for the GEE temporal stack.

A single module that exercises the reducers, `CompositeEngine` and
`CadenceBucketer` together on synthetic stacks of known per-pixel values, so the
client-side reduction is checked numerically against `numpy` without a live
Earth Engine round trip. The per-module tests (`test_temporal_reducer.py`,
`test_composite.py`, `test_backend.py`) cover each piece in isolation and on
constant-valued rasters; this one varies values per pixel and across time, which
is what turns the parity checks into real fits.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from earthlens.base import date_windows
from earthlens.gee._composite import CadenceBucketer, CompositeEngine
from earthlens.gee.reducers import temporal_reducer_for
from earthlens.gee.work import RasterWorkUnit

pytestmark = pytest.mark.gee

#: Unevenly spaced acquisition times, so a recovered trend is a genuine
#: least-squares fit rather than an artefact of equal spacing.
_TIMES = ["2020-01-01", "2020-02-01", "2020-04-01", "2020-07-01"]

#: Per-pixel intercept and slope (value-units per day) injected into the stack;
#: `trend` / `sens_slope` must recover `_SLOPE` exactly.
_BASE = np.array([[10.0, 20.0], [0.0, 5.0], [100.0, -5.0]])
_SLOPE = np.array([[0.1, -0.2], [0.05, 0.3], [-0.01, 0.4]])


def _linear_arrays() -> list[np.ndarray]:
    """Return one per-pixel scene per timestep, each `_BASE + _SLOPE * day`."""
    index = pd.DatetimeIndex(pd.to_datetime(_TIMES))
    days = (index - index[0]).days.to_numpy().astype(float)
    return [_BASE + _SLOPE * day for day in days]


def _write_stack(folder, arrays: list[np.ndarray]):
    """Write each array to its own GeoTIFF and open them as one stack.

    pyramids `reduce_time` (which `reduce_stack` delegates to) reads a file- or
    zarr-backed collection, so the synthetic scenes are written to disk first.
    """
    from pyramids.dataset import Dataset, DatasetCollection
    from pyramids.dataset.dataset import GeoReference

    folder.mkdir(parents=True, exist_ok=True)
    geo = GeoReference(top_left_corner=(0.0, 3.0), cell_size=1.0, epsg=4326)
    paths = []
    for i, array in enumerate(arrays):
        path = str(folder / f"t{i}.tif")
        Dataset.from_array(array, geo_ref=geo, no_data_value=-9999.0, path=path)
        paths.append(path)
    return DatasetCollection.from_files(paths)


class _FakeImage:
    """The reduced image a convenience reducer returns, remembering its source."""

    def __init__(self, collection):
        self.collection = collection


class _FakeCollection:
    """A fake `ee.ImageCollection` recording the `filterDate` window per bucket."""

    def __init__(self, window=None):
        self.window = window

    def filterDate(self, start, end):  # noqa: N802 - mirrors the Earth Engine API
        return _FakeCollection(window=(start, end))

    def mean(self):
        return _FakeImage(self)


def _mean_engine(cadence: str) -> CompositeEngine:
    """A mean-reducing `CompositeEngine` at the given cadence."""
    return CompositeEngine(
        reducer=temporal_reducer_for("mean"), cadence=CadenceBucketer(cadence)
    )


class TestReducerStackParity:
    """`reduce_stack` matches the `numpy` ground truth on a per-pixel stack."""

    @pytest.mark.parametrize(
        ("spec", "expected_fn"),
        [
            ("mean", lambda stack: np.nanmean(stack, axis=0)),
            ("median", lambda stack: np.nanmedian(stack, axis=0)),
            ("min", lambda stack: np.nanmin(stack, axis=0)),
            ("max", lambda stack: np.nanmax(stack, axis=0)),
            ("sum", lambda stack: np.nansum(stack, axis=0)),
        ],
    )
    def test_convenience_reducer_matches_numpy(self, tmp_path, spec, expected_fn):
        """Each band-preserving reducer equals its `numpy` counterpart per pixel."""
        arrays = _linear_arrays()
        stack = _write_stack(tmp_path, arrays)
        reduced = temporal_reducer_for(spec).reduce_stack(stack, _TIMES)
        expected = expected_fn(np.stack(arrays, axis=0))
        np.testing.assert_allclose(np.asarray(reduced.read_array()), expected)

    def test_p90_matches_numpy_percentile(self, tmp_path):
        """A `p90` composite equals `np.nanpercentile(stack, 90)` per pixel."""
        arrays = _linear_arrays()
        stack = _write_stack(tmp_path, arrays)
        reduced = temporal_reducer_for("p90").reduce_stack(stack, _TIMES)
        expected = np.nanpercentile(np.stack(arrays, axis=0), 90, axis=0)
        np.testing.assert_allclose(
            np.asarray(reduced.read_array()), expected, equal_nan=True
        )

    def test_percentile_spec_and_shorthand_agree(self, tmp_path):
        """`percentile:90` and its `p90` shorthand reduce to the same array."""
        arrays = _linear_arrays()
        spec = temporal_reducer_for("percentile:90").reduce_stack(
            _write_stack(tmp_path / "spec_dir", arrays), _TIMES
        )
        short = temporal_reducer_for("p90").reduce_stack(
            _write_stack(tmp_path / "short_dir", arrays), _TIMES
        )
        np.testing.assert_allclose(
            np.asarray(spec.read_array()), np.asarray(short.read_array())
        )

    def test_trend_recovers_the_injected_slope(self, tmp_path):
        """`trend` recovers the per-pixel slope in value-units per day."""
        stack = _write_stack(tmp_path, _linear_arrays())
        reduced = temporal_reducer_for("trend").reduce_stack(stack, _TIMES)
        np.testing.assert_allclose(np.asarray(reduced.read_array()), _SLOPE, atol=1e-6)

    def test_sens_slope_recovers_the_injected_slope(self, tmp_path):
        """`sens_slope` recovers the same per-pixel slope, robustly."""
        stack = _write_stack(tmp_path, _linear_arrays())
        reduced = temporal_reducer_for("sens_slope").reduce_stack(stack, _TIMES)
        np.testing.assert_allclose(np.asarray(reduced.read_array()), _SLOPE, atol=1e-6)


class TestCompositeEngineEndToEnd:
    """`CompositeEngine` buckets a window and yields one reduced unit per bucket."""

    @pytest.mark.parametrize(
        ("cadence", "start", "end", "count"),
        [
            ("raw", dt.datetime(2020, 6, 1), dt.datetime(2020, 9, 1), 1),
            ("daily", dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 4), 3),
            ("monthly", dt.datetime(2020, 6, 1), dt.datetime(2020, 9, 1), 3),
            ("yearly", dt.datetime(2020, 1, 1), dt.datetime(2023, 1, 1), 3),
        ],
    )
    def test_each_cadence_yields_one_unit_per_bucket(self, cadence, start, end, count):
        """raw / daily / monthly / yearly split into the expected bucket count."""
        units = list(
            _mean_engine(cadence).composite_ee(
                _FakeCollection(), start, end, is_image_collection=True
            )
        )
        assert len(units) == count
        assert all(isinstance(unit, RasterWorkUnit) for unit in units)

    def test_static_image_is_one_whole_window_bucket(self, tmp_path):
        """A static (non-collection) dataset is one unfiltered bucket at any cadence."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 9, 1)
        units = list(
            _mean_engine("monthly").composite_ee(
                _FakeCollection(), start, end, is_image_collection=False
            )
        )
        assert len(units) == 1
        assert units[0].image.collection.window is None

    def test_periodic_buckets_filter_each_window_and_pin_the_end(self):
        """Each periodic bucket filters its own window; the last end is the request end."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 15)
        units = list(
            _mean_engine("monthly").composite_ee(
                _FakeCollection(), start, end, is_image_collection=True
            )
        )
        assert [unit.image.collection.window for unit in units] == [
            ("2020-06-01", "2020-07-01"),
            ("2020-07-01", "2020-07-15"),
        ]
        assert units[-1].bucket_end == end


class TestCadenceBucketerEndToEnd:
    """`CadenceBucketer` resolves cadences and refuses unknown ones."""

    @pytest.mark.parametrize(
        ("cadence", "freq"), [("daily", "D"), ("monthly", "MS"), ("yearly", "YS")]
    )
    def test_bucket_edges_match_date_windows(self, cadence, freq):
        """The bucket edges are exactly `date_windows(..., inclusive='left')`."""
        start, end = dt.datetime(2020, 1, 1), dt.datetime(2020, 4, 1)
        edges = list(CadenceBucketer(cadence).windows(start, end))
        assert edges == list(date_windows(start, end, freq, inclusive="left"))

    def test_raw_is_the_single_whole_window(self):
        """`raw` is one whole-window bucket, not a period split."""
        assert CadenceBucketer("raw").is_whole_window is True

    def test_unknown_cadence_raises_did_you_mean(self):
        """A mistyped cadence is refused with a suggestion, not defaulted to daily."""
        with pytest.raises(ValueError, match="Did you mean 'monthly'"):
            CadenceBucketer("monthy")
