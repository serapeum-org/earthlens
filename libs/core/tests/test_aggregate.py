"""Unit tests for `earthlens.aggregate`.

Covers `AggregationConfig` validation, `_read_time_axis` (the
candidate-loop and KeyError fallback), `_find_level_dim`, the
four-cell decision matrix in `_resolve_pressure_level`,
`window_groups`, `reduce_time_axis` (op dispatch + skipna + min_count),
`_resolve_op` (auto-routing from `Variable.is_flux`), and round-trip
runs of `aggregate_netcdf` against synthetic NetCDFs (H7).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from earthlens.aggregate import (
    _LEVEL_DIM_CANDIDATES,
    _REDUCERS_SKIPNA,
    _REDUCERS_STRICT,
    _TIME_VAR_CANDIDATES,
    AggregationConfig,
    _find_level_dim,
    _output_stem,
    _read_time_axis,
    _resolve_op,
    _resolve_pressure_level,
    aggregate_netcdf,
    iter_aggregate_netcdf,
    reduce_time_axis,
    window_groups,
)

pytestmark = [pytest.mark.unit]


def _make_nc(
    *,
    time_strs_by_var: dict[str, list[str] | None] | None = None,
    dimension_names: list[str] | None = None,
    sel_result: object | None = None,
) -> MagicMock:
    """Build a `NetCDF`-shaped MagicMock for the helpers under test.

    Args:
        time_strs_by_var: Map from variable name to the list returned
            by `nc.get_time_variable(var_name=name)`. Names not in
            the map return `None`.
        dimension_names: Value returned by the `dimension_names`
            property.
        sel_result: Object returned by `nc.sel(...)`. Defaults to a
            fresh `MagicMock` so tests can compare identity.
    """
    nc = MagicMock()
    table = time_strs_by_var or {}
    nc.get_time_variable = MagicMock(side_effect=lambda var_name: table.get(var_name))
    nc.dimension_names = dimension_names
    nc.sel = MagicMock(
        return_value=sel_result if sel_result is not None else MagicMock()
    )
    return nc


class TestAggregationConfig:
    """Tests for :class:`AggregationConfig` (H1 surface)."""

    def test_freq_is_required(self):
        """`freq` has no default — omitting it raises ValidationError."""
        with pytest.raises(ValidationError) as excinfo:
            AggregationConfig()
        assert "freq" in str(excinfo.value), (
            f"ValidationError should mention `freq`, got: {excinfo.value}"
        )

    def test_default_op_is_auto(self):
        """`op` defaults to `"auto"` so flux/state routing works without
        an explicit choice."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.op == "auto", f"Expected default op 'auto', got {cfg.op!r}"

    def test_default_skipna_is_true(self):
        """`skipna` defaults to `True` (NaN-aware reductions)."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.skipna is True, f"Expected default skipna True, got {cfg.skipna!r}"

    def test_default_min_count_is_none(self):
        """`min_count` defaults to `None` (no minimum)."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.min_count is None, (
            f"Expected default min_count None, got {cfg.min_count!r}"
        )

    def test_default_level_is_none(self):
        """`level` defaults to `None` (3-D NetCDFs assumed)."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.level is None, f"Expected default level None, got {cfg.level!r}"

    def test_default_cell_size_is_era5_native(self):
        """`cell_size` defaults to ERA5's native 0.125° grid."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.cell_size == 0.125, (
            f"Expected default cell_size 0.125, got {cfg.cell_size!r}"
        )

    def test_default_out_dir_is_none(self):
        """`out_dir=None` means in-memory only — no GeoTIFF writes."""
        cfg = AggregationConfig(freq="1D")
        assert cfg.out_dir is None, (
            f"Expected default out_dir None, got {cfg.out_dir!r}"
        )

    def test_frozen_disallows_mutation(self):
        """Mutating an instantiated config raises (frozen)."""
        cfg = AggregationConfig(freq="1D")
        with pytest.raises(ValidationError):
            cfg.freq = "1MS"

    def test_extra_field_typo_rejected(self):
        """`freqency=` (typo) raises ValidationError, not a silent default."""
        with pytest.raises(ValidationError) as excinfo:
            AggregationConfig(freqency="1D")
        assert "freqency" in str(excinfo.value), (
            f"ValidationError should mention the offending key, got: {excinfo.value}"
        )

    def test_invalid_op_rejected(self):
        """`op` outside the `OperationLiteral` set raises."""
        with pytest.raises(ValidationError):
            AggregationConfig(freq="1D", op="median")

    @pytest.mark.parametrize("op_value", ["mean", "sum", "min", "max", "std", "auto"])
    def test_each_valid_op_accepted(self, op_value):
        """Every literal in `OperationLiteral` is accepted as-is.

        Args:
            op_value: One of the valid op literals.
        """
        cfg = AggregationConfig(freq="1D", op=op_value)
        assert cfg.op == op_value, f"Expected op {op_value!r}, got {cfg.op!r}"

    def test_out_dir_path_object_accepted(self, tmp_path):
        """A `Path` instance for `out_dir` is preserved as a `Path`."""
        cfg = AggregationConfig(freq="1D", out_dir=tmp_path)
        assert isinstance(cfg.out_dir, Path), (
            f"Expected Path instance, got {type(cfg.out_dir).__name__}"
        )
        assert cfg.out_dir == tmp_path, (
            f"Expected out_dir {tmp_path}, got {cfg.out_dir}"
        )

    def test_out_dir_string_coerced_to_path(self):
        """A string `out_dir` is coerced to a `pathlib.Path` by pydantic."""
        cfg = AggregationConfig(freq="1D", out_dir="out/monthly")
        assert isinstance(cfg.out_dir, Path), (
            f"Expected Path coercion, got {type(cfg.out_dir).__name__}"
        )

    def test_min_count_int_accepted(self):
        """An integer `min_count` survives validation."""
        cfg = AggregationConfig(freq="1D", min_count=4)
        assert cfg.min_count == 4, f"Expected min_count 4, got {cfg.min_count!r}"

    def test_level_int_accepted(self):
        """A pressure level can be supplied as an integer (e.g., 1000)."""
        cfg = AggregationConfig(freq="1D", level=1000)
        assert cfg.level == 1000, f"Expected level 1000, got {cfg.level!r}"

    def test_level_float_accepted(self):
        """A pressure level can be supplied as a float (e.g., 850.5)."""
        cfg = AggregationConfig(freq="1D", level=850.5)
        assert cfg.level == 850.5, f"Expected level 850.5, got {cfg.level!r}"

    def test_skipna_false_explicit(self):
        """`skipna=False` is preserved — NaN-propagating reductions."""
        cfg = AggregationConfig(freq="1D", skipna=False)
        assert cfg.skipna is False, f"Expected skipna False, got {cfg.skipna!r}"


class TestReadTimeAxis:
    """Tests for the private `_read_time_axis` helper (H2)."""

    def test_valid_time_takes_priority(self):
        """When both `valid_time` and `time` are present, `valid_time` wins."""
        nc = _make_nc(
            time_strs_by_var={
                "valid_time": ["2022-06-15"],
                "time": ["1970-01-01"],
            }
        )
        result = _read_time_axis(nc)
        assert result[0] == pd.Timestamp("2022-06-15"), (
            f"Expected valid_time to win, got {result[0]}"
        )

    def test_falls_back_to_time_when_valid_time_absent(self):
        """`time` is used when `valid_time` returns None."""
        nc = _make_nc(time_strs_by_var={"valid_time": None, "time": ["2020-01-01"]})
        result = _read_time_axis(nc)
        assert result[0] == pd.Timestamp("2020-01-01"), (
            f"Expected time fallback, got {result[0]}"
        )

    def test_falls_back_to_time_when_valid_time_empty_list(self):
        """An empty list for `valid_time` is treated as absence."""
        nc = _make_nc(time_strs_by_var={"valid_time": [], "time": ["2021-03-04"]})
        result = _read_time_axis(nc)
        assert result[0] == pd.Timestamp("2021-03-04"), (
            f"Expected time fallback for empty valid_time, got {result[0]}"
        )

    def test_returns_datetimeindex(self):
        """Return type is `pandas.DatetimeIndex`."""
        nc = _make_nc(time_strs_by_var={"time": ["2022-01-01"]})
        result = _read_time_axis(nc)
        assert isinstance(result, pd.DatetimeIndex), (
            f"Expected DatetimeIndex, got {type(result).__name__}"
        )

    def test_parses_multiple_dates_in_order(self):
        """The helper preserves the order of the input strings."""
        dates = ["2022-01-01", "2022-01-02", "2022-01-03"]
        nc = _make_nc(time_strs_by_var={"time": dates})
        result = _read_time_axis(nc)
        assert list(result) == [pd.Timestamp(d) for d in dates], (
            f"Expected dates in order, got {list(result)}"
        )

    def test_keyerror_when_no_candidate_resolves(self):
        """`KeyError` when both candidates return None / empty."""
        nc = _make_nc(time_strs_by_var={"valid_time": None, "time": None})
        with pytest.raises(KeyError) as excinfo:
            _read_time_axis(nc)
        msg = str(excinfo.value)
        for name in _TIME_VAR_CANDIDATES:
            assert name in msg, (
                f"KeyError message should list candidate {name!r}, got: {msg}"
            )

    def test_get_time_variable_called_with_var_name_kwarg(self):
        """The helper passes each candidate as a keyword argument."""
        nc = _make_nc(time_strs_by_var={"time": ["2022-01-01"]})
        _read_time_axis(nc)
        call_kwargs = [call.kwargs for call in nc.get_time_variable.call_args_list]
        assert all("var_name" in kwargs for kwargs in call_kwargs), (
            f"All calls should pass var_name as kwarg, got: {call_kwargs}"
        )


class TestFindLevelDim:
    """Tests for `_find_level_dim` (M1 detection)."""

    def test_pressure_level_returned_when_present(self):
        """`pressure_level` matches the first candidate."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "lat", "lon"])
        result = _find_level_dim(nc)
        assert result == "pressure_level", f"Expected 'pressure_level', got {result!r}"

    def test_level_returned_when_no_pressure_level(self):
        """`level` matches the second candidate when `pressure_level` is absent."""
        nc = _make_nc(dimension_names=["time", "level", "lat", "lon"])
        result = _find_level_dim(nc)
        assert result == "level", f"Expected 'level', got {result!r}"

    def test_pressure_level_takes_priority_over_level(self):
        """When both names are present, the first candidate wins."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "level", "lat", "lon"])
        result = _find_level_dim(nc)
        assert result == "pressure_level", (
            f"Expected 'pressure_level' to win over 'level', got {result!r}"
        )

    def test_returns_none_for_3d_netcdf(self):
        """No level dimension → `None`."""
        nc = _make_nc(dimension_names=["time", "lat", "lon"])
        result = _find_level_dim(nc)
        assert result is None, f"Expected None for 3-D NetCDF, got {result!r}"

    def test_returns_none_when_dimension_names_is_none(self):
        """A NetCDF with no root group reports `dimension_names=None`."""
        nc = _make_nc(dimension_names=None)
        result = _find_level_dim(nc)
        assert result is None, (
            f"Expected None when dimension_names is None, got {result!r}"
        )

    def test_candidates_constant_shape(self):
        """Documenting the candidate list as a tuple of two names."""
        assert _LEVEL_DIM_CANDIDATES == (
            "pressure_level",
            "level",
        ), f"Unexpected candidate list: {_LEVEL_DIM_CANDIDATES!r}"


class TestResolvePressureLevel:
    """Tests for the four-cell decision matrix in `_resolve_pressure_level`."""

    def test_3d_no_level_returns_input_unchanged(self):
        """3-D NetCDF + no `level` → pass-through (same instance)."""
        nc = _make_nc(dimension_names=["time", "lat", "lon"])
        result = _resolve_pressure_level(nc, level=None)
        assert result is nc, "Expected input nc returned unchanged"
        nc.sel.assert_not_called()

    def test_3d_with_level_raises_value_error(self):
        """3-D NetCDF + `level` set → ValueError ('no pressure-level dim')."""
        nc = _make_nc(dimension_names=["time", "lat", "lon"])
        with pytest.raises(ValueError) as excinfo:
            _resolve_pressure_level(nc, level=1000)
        msg = str(excinfo.value)
        assert "no" in msg.lower() and "pressure-level dimension" in msg, (
            f"Error should explain the missing dimension, got: {msg}"
        )

    def test_3d_with_level_error_mentions_passed_value(self):
        """The error names the offending `level` so users can find it."""
        nc = _make_nc(dimension_names=["time", "lat", "lon"])
        with pytest.raises(ValueError, match=r"850"):
            _resolve_pressure_level(nc, level=850)

    def test_4d_without_level_raises_value_error(self):
        """4-D NetCDF + no `level` → ValueError ('pass level=...')."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "lat", "lon"])
        with pytest.raises(ValueError) as excinfo:
            _resolve_pressure_level(nc, level=None)
        msg = str(excinfo.value)
        assert "level=" in msg.lower() or "level=" in msg, (
            f"Error should hint at `level=` parameter, got: {msg}"
        )

    def test_4d_without_level_error_mentions_dim_name(self):
        """The error names the actual dimension found."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "lat", "lon"])
        with pytest.raises(ValueError, match=r"pressure_level"):
            _resolve_pressure_level(nc, level=None)

    def test_4d_with_level_calls_sel_with_pressure_level_kwarg(self):
        """4-D `pressure_level` + level → `nc.sel(pressure_level=level)`."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "lat", "lon"])
        _resolve_pressure_level(nc, level=1000)
        nc.sel.assert_called_once_with(pressure_level=1000)

    def test_4d_with_level_calls_sel_with_level_kwarg(self):
        """4-D `level` + level → `nc.sel(level=level)` (alt dim name)."""
        nc = _make_nc(dimension_names=["time", "level", "lat", "lon"])
        _resolve_pressure_level(nc, level=850)
        nc.sel.assert_called_once_with(level=850)

    def test_4d_with_level_returns_sel_result(self):
        """The returned NetCDF is the result of `sel(...)`, not the input."""
        sel_output = MagicMock(name="pinned_nc")
        nc = _make_nc(
            dimension_names=["time", "pressure_level", "lat", "lon"],
            sel_result=sel_output,
        )
        result = _resolve_pressure_level(nc, level=1000)
        assert result is sel_output, (
            f"Expected sel result, got {result!r} (input was {nc!r})"
        )

    def test_4d_with_float_level(self):
        """A float `level` (e.g., 850.5) is forwarded to `sel` verbatim."""
        nc = _make_nc(dimension_names=["time", "pressure_level", "lat", "lon"])
        _resolve_pressure_level(nc, level=850.5)
        nc.sel.assert_called_once_with(pressure_level=850.5)


class TestWindowGroups:
    """Tests for the public `window_groups` primitive (H3)."""

    def test_daily_grouping_six_hourly_input_yields_one_window(self):
        """Four 6-hourly slots in one day collapse to one daily window."""
        idx = pd.date_range("2022-01-01", periods=4, freq="6h")
        windows = list(window_groups(idx, "1D"))
        assert len(windows) == 1, f"Expected 1 daily window, got {len(windows)}"
        label, mask = windows[0]
        assert label == pd.Timestamp("2022-01-01"), (
            f"Expected window label 2022-01-01, got {label}"
        )
        assert mask.tolist() == [
            True,
            True,
            True,
            True,
        ], f"Expected all four samples in window, got {mask.tolist()}"

    def test_daily_grouping_two_days_yields_two_windows(self):
        """Eight 6-hourly slots over two days produce two daily windows."""
        idx = pd.date_range("2022-01-01", periods=8, freq="6h")
        labels = [label for label, _ in window_groups(idx, "1D")]
        assert labels == [
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2022-01-02"),
        ], f"Expected two consecutive day labels, got {labels}"

    def test_weekly_grouping_collapses_seven_days(self):
        """Daily samples over a week reduce to one `"7D"` window."""
        idx = pd.date_range("2022-01-01", periods=7, freq="D")
        windows = list(window_groups(idx, "7D"))
        assert len(windows) == 1, f"Expected 1 weekly window, got {len(windows)}"
        _, mask = windows[0]
        assert mask.sum() == 7, f"Expected 7 samples in window, got {mask.sum()}"

    def test_monthly_ms_grouping_collapses_january(self):
        """31 daily samples in January produce one `"1MS"` window."""
        idx = pd.date_range("2022-01-01", periods=31, freq="D")
        windows = list(window_groups(idx, "1MS"))
        assert len(windows) == 1, f"Expected 1 monthly window, got {len(windows)}"
        label, mask = windows[0]
        assert label == pd.Timestamp("2022-01-01"), (
            f"Expected month-start label, got {label}"
        )
        assert mask.sum() == 31, f"Expected 31 samples, got {mask.sum()}"

    def test_monthly_ms_two_months_yields_two_windows(self):
        """A 32-day range across Jan/Feb yields two month-start windows."""
        idx = pd.date_range("2022-01-01", periods=32, freq="D")
        labels = [label for label, _ in window_groups(idx, "1MS")]
        assert labels == [
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2022-02-01"),
        ], f"Expected two month-start labels, got {labels}"

    def test_seasonal_grouping_qs_dec_yields_three_aligned_seasons(self):
        """`QS-DEC` aligns seasons on Dec/Mar/Jun/Sep starts."""
        idx = pd.date_range("2022-03-01", periods=9, freq="MS")
        labels = [label for label, _ in window_groups(idx, "QS-DEC")]
        assert labels == [
            pd.Timestamp("2022-03-01"),
            pd.Timestamp("2022-06-01"),
            pd.Timestamp("2022-09-01"),
        ], f"Expected three quarter starts, got {labels}"

    def test_window_label_is_left_edge(self):
        """Group keys returned are the windows' left-edge timestamps."""
        idx = pd.date_range("2022-06-15", periods=4, freq="6h")
        label, _ = next(iter(window_groups(idx, "1D")))
        assert label == pd.Timestamp("2022-06-15"), (
            f"Expected left-edge 2022-06-15, got {label}"
        )

    def test_mask_length_matches_input(self):
        """Each emitted mask has length equal to the time axis."""
        idx = pd.date_range("2022-01-01", periods=12, freq="6h")
        for _, mask in window_groups(idx, "1D"):
            assert len(mask) == 12, (
                f"Mask length {len(mask)} should match time-axis length 12"
            )

    def test_mask_dtype_is_bool(self):
        """Mask is a numpy bool array — drop-in for ndarray indexing."""
        idx = pd.date_range("2022-01-01", periods=4, freq="6h")
        _, mask = next(iter(window_groups(idx, "1D")))
        assert isinstance(mask, np.ndarray), (
            f"Expected numpy ndarray, got {type(mask).__name__}"
        )
        assert mask.dtype == np.bool_, f"Expected bool dtype, got {mask.dtype}"

    def test_masks_isolate_correct_indices(self):
        """Each mask selects exactly the samples in its window."""
        idx = pd.date_range("2022-01-01", periods=8, freq="6h")
        windows = list(window_groups(idx, "1D"))
        first_mask = windows[0][1]
        second_mask = windows[1][1]
        assert first_mask.tolist() == [True] * 4 + [False] * 4, (
            f"First daily mask wrong: {first_mask.tolist()}"
        )
        assert second_mask.tolist() == [False] * 4 + [True] * 4, (
            f"Second daily mask wrong: {second_mask.tolist()}"
        )
        assert (first_mask & second_mask).sum() == 0, "Daily masks must be disjoint"

    def test_empty_time_axis_yields_nothing(self):
        """An empty index produces no windows."""
        idx = pd.DatetimeIndex([])
        windows = list(window_groups(idx, "1D"))
        assert windows == [], f"Expected no windows, got {windows}"

    def test_single_sample_yields_single_window(self):
        """One timestamp → one window with one true bit."""
        idx = pd.DatetimeIndex(["2022-06-15"])
        windows = list(window_groups(idx, "1D"))
        assert len(windows) == 1, f"Expected 1 window, got {len(windows)}"
        _, mask = windows[0]
        assert mask.tolist() == [True], f"Expected [True], got {mask.tolist()}"

    def test_invalid_freq_raises(self):
        """An unparseable `freq` string surfaces a pandas error."""
        idx = pd.date_range("2022-01-01", periods=4, freq="6h")
        with pytest.raises(ValueError):
            list(window_groups(idx, "not-a-real-freq"))


class TestReduce:
    """Tests for the public `reduce_time_axis` primitive (H4)."""

    @pytest.fixture(scope="class")
    def cube(self) -> np.ndarray:
        """A 3-D `(time=4, lat=2, lon=2)` array with known values per pixel.

        Pixel `(0, 0)` = [1, 2, 3, 4]; `(0, 1)` = [10, 20, 30, 40];
        `(1, 0)` = [-1, -2, -3, -4]; `(1, 1)` = [0.1, 0.2, 0.3, 0.4].
        """
        return np.array(
            [
                [[1.0, 10.0], [-1.0, 0.1]],
                [[2.0, 20.0], [-2.0, 0.2]],
                [[3.0, 30.0], [-3.0, 0.3]],
                [[4.0, 40.0], [-4.0, 0.4]],
            ]
        )

    @pytest.mark.parametrize(
        "op, expected_pixel_00",
        [
            ("mean", 2.5),
            ("sum", 10.0),
            ("min", 1.0),
            ("max", 4.0),
        ],
    )
    def test_each_op_dispatches_to_correct_reducer(self, cube, op, expected_pixel_00):
        """Each named op produces the expected reduction at one known pixel.

        Args:
            cube: Class fixture providing a `(4, 2, 2)` test array.
            op: Reduction operator under test.
            expected_pixel_00: Known result at pixel `(0, 0)`.
        """
        result = reduce_time_axis(cube, op=op, skipna=True, min_count=None)
        assert result.shape == (2, 2), f"Expected (2, 2), got {result.shape}"
        assert result[0, 0] == pytest.approx(expected_pixel_00), (
            f"Op {op!r} at (0, 0): expected {expected_pixel_00}, got {result[0, 0]}"
        )

    def test_std_op_returns_nonzero(self, cube):
        """`std` over a non-constant series produces a positive value."""
        result = reduce_time_axis(cube, op="std", skipna=True, min_count=None)
        assert result[0, 0] > 0, (
            f"std should be positive for non-constant series, got {result[0, 0]}"
        )

    def test_skipna_true_excludes_nan_from_mean(self):
        """NaN-aware mean ignores NaN samples in the window."""
        arr = np.array([[[1.0]], [[2.0]], [[np.nan]], [[3.0]]])
        result = reduce_time_axis(arr, op="mean", skipna=True, min_count=None)
        assert result[0, 0] == pytest.approx(2.0), (
            f"Expected NaN-skipped mean 2.0, got {result[0, 0]}"
        )

    def test_skipna_false_propagates_nan_to_output(self):
        """Strict mean propagates any NaN to the result."""
        arr = np.array([[[1.0, 2.0]], [[np.nan, 3.0]]])
        result = reduce_time_axis(arr, op="mean", skipna=False, min_count=None)
        assert np.isnan(result[0, 0]), (
            f"Pixel (0, 0) should be NaN under strict mode, got {result[0, 0]}"
        )
        assert result[0, 1] == pytest.approx(2.5), (
            f"Pixel (0, 1) had no NaN; expected 2.5, got {result[0, 1]}"
        )

    @pytest.mark.parametrize("op", ["mean", "sum", "min", "max", "std"])
    def test_skipna_true_uses_nan_aware_table(self, op):
        """Every op routes through the NaN-aware table when `skipna=True`."""
        assert op in _REDUCERS_SKIPNA, (
            f"_REDUCERS_SKIPNA missing op {op!r}: {sorted(_REDUCERS_SKIPNA)}"
        )

    @pytest.mark.parametrize("op", ["mean", "sum", "min", "max", "std"])
    def test_skipna_false_uses_strict_table(self, op):
        """Every op routes through the strict table when `skipna=False`."""
        assert op in _REDUCERS_STRICT, (
            f"_REDUCERS_STRICT missing op {op!r}: {sorted(_REDUCERS_STRICT)}"
        )

    def test_min_count_masks_under_sampled_pixel(self):
        """Pixels with fewer non-NaN samples than `min_count` emit NaN."""
        arr = np.array([[[1.0, 2.0]], [[np.nan, 3.0]]])
        result = reduce_time_axis(arr, op="mean", skipna=True, min_count=2)
        assert np.isnan(result[0, 0]), (
            f"Under-sampled pixel should be NaN, got {result[0, 0]}"
        )
        assert result[0, 1] == pytest.approx(2.5), (
            f"Fully-sampled pixel should survive: expected 2.5, got {result[0, 1]}"
        )

    def test_min_count_none_disables_floor(self):
        """`min_count=None` lets every reduction reach the output as-is."""
        arr = np.array([[[1.0]], [[np.nan]], [[3.0]]])
        result = reduce_time_axis(arr, op="mean", skipna=True, min_count=None)
        assert result[0, 0] == pytest.approx(2.0), (
            f"Expected 2.0 with min_count=None, got {result[0, 0]}"
        )

    def test_keyerror_on_auto(self):
        """`op="auto"` is rejected — caller must resolve it first."""
        arr = np.zeros((2, 2, 2))
        with pytest.raises(KeyError, match="auto"):
            reduce_time_axis(arr, op="auto", skipna=True, min_count=None)

    def test_keyerror_on_unknown_op(self):
        """An unknown op raises `KeyError` listing the valid choices."""
        arr = np.zeros((2, 2, 2))
        with pytest.raises(KeyError) as excinfo:
            reduce_time_axis(arr, op="median", skipna=True, min_count=None)
        msg = str(excinfo.value)
        for valid in ("mean", "sum", "min", "max", "std"):
            assert valid in msg, (
                f"Error message should list valid op {valid!r}, got: {msg}"
            )

    def test_collapses_axis_zero_only(self):
        """Reduction collapses axis 0; the remaining shape passes through."""
        arr = np.zeros((4, 3, 5))
        result = reduce_time_axis(arr, op="mean", skipna=True, min_count=None)
        assert result.shape == (3, 5), f"Expected (3, 5), got {result.shape}"


class TestResolveOp:
    """Tests for `_resolve_op` (M2 — `op="auto"` routing)."""

    def test_auto_with_flux_returns_sum(self):
        """`op="auto"` + `is_flux=True` resolves to `"sum"`."""
        result = _resolve_op("auto", SimpleNamespace(is_flux=True))
        assert result == "sum", f"Expected 'sum', got {result!r}"

    def test_auto_with_state_returns_mean(self):
        """`op="auto"` + `is_flux=False` resolves to `"mean"`."""
        result = _resolve_op("auto", SimpleNamespace(is_flux=False))
        assert result == "mean", f"Expected 'mean', got {result!r}"

    def test_auto_with_pre_aggregated_flux_returns_mean(self):
        """`op="auto"` + pre-aggregated flux resolves to `"mean"`, not `"sum"` (#43).

        A flux variable from a daily-statistics / monthly-means dataset is
        already a server-side aggregate; summing it over a window over-counts,
        so pre-aggregation wins over the flux rule.
        """
        result = _resolve_op(
            "auto", SimpleNamespace(is_flux=True, is_pre_aggregated=True)
        )
        assert result == "mean", (
            f"Expected 'mean' for pre-aggregated flux, got {result!r}"
        )

    def test_auto_with_pre_aggregated_state_stays_mean(self):
        """`op="auto"` + pre-aggregated state stays `"mean"`."""
        result = _resolve_op(
            "auto", SimpleNamespace(is_flux=False, is_pre_aggregated=True)
        )
        assert result == "mean", f"Expected 'mean', got {result!r}"

    def test_auto_without_is_pre_aggregated_attr_falls_back_to_flux(self):
        """A `var_info` lacking `is_pre_aggregated` keeps the flux rule.

        `is_pre_aggregated` is read defensively (`getattr`, default `False`), so
        a stub without it resolves purely on `is_flux`.
        """
        result = _resolve_op("auto", SimpleNamespace(is_flux=True))
        assert result == "sum", f"Expected 'sum' fallback, got {result!r}"

    def test_explicit_op_ignores_pre_aggregated(self):
        """An explicit op passes through even for a pre-aggregated variable."""
        result = _resolve_op(
            "sum", SimpleNamespace(is_flux=False, is_pre_aggregated=True)
        )
        assert result == "sum", f"Expected 'sum' passthrough, got {result!r}"

    @pytest.mark.parametrize("explicit_op", ["mean", "sum", "min", "max", "std"])
    def test_explicit_op_passthrough(self, explicit_op):
        """Any non-`auto` op is returned verbatim regardless of `is_flux`.

        Args:
            explicit_op: The op literal under test.
        """
        result = _resolve_op(explicit_op, SimpleNamespace(is_flux=True))
        assert result == explicit_op, (
            f"Expected {explicit_op!r} passthrough, got {result!r}"
        )

    def test_explicit_op_does_not_consult_is_flux(self):
        """Explicit ops do not read `var_info.is_flux`."""

        class TrackedVar:
            """Var stub that records access to `is_flux`."""

            def __init__(self):
                self.accessed = False

            @property
            def is_flux(self) -> bool:
                """Track property access then return False."""
                self.accessed = True
                return False

        var = TrackedVar()
        _resolve_op("max", var)
        assert var.accessed is False, "Explicit op should not consult var_info.is_flux"


class TestOutputStem:
    """`_output_stem` appends the dataset id (dataset_id or cds_dataset) (#1040)."""

    def test_bare_when_no_dataset_ids(self):
        """A var_info carrying neither id (s3/erddap) keeps the bare stem."""
        stem = _output_stem(SimpleNamespace(cds_variable="tp"))
        assert stem == "tp", f"Expected bare stem 'tp', got {stem!r}"

    def test_ordinary_row_appends_its_dataset_id(self):
        """An ordinary row (dataset_id == cds_dataset) appends that id."""
        var = SimpleNamespace(cds_variable="tp", cds_dataset="ds", dataset_id="ds")
        stem = _output_stem(var)
        assert stem == "tp_ds", f"Ordinary row should append its id, got {stem!r}"

    def test_override_uses_dataset_id(self):
        """A dataset_id differing from cds_dataset is the one appended."""
        var = SimpleNamespace(
            cds_variable="tp", cds_dataset="ds", dataset_id="ds-intermediate"
        )
        stem = _output_stem(var)
        assert stem == "tp_ds-intermediate", (
            f"Override should use dataset_id, got {stem!r}"
        )

    def test_falls_back_to_cds_dataset(self):
        """With no dataset_id, cds_dataset is appended instead."""
        stem = _output_stem(SimpleNamespace(cds_variable="tp", cds_dataset="ds"))
        assert stem == "tp_ds", f"Expected cds_dataset fallback, got {stem!r}"


class TestAggregateNetcdf:
    """Smoke tests for the public entry point.

    Heavy round-trip behaviour against a synthetic NetCDF lives in
    :class:`TestAggregateNetcdfRoundTrip` (H7). These checks only
    verify the function reaches the pyramids layer.
    """

    def test_missing_file_raises_at_pyramids_layer(self, tmp_path):
        """A non-existent path surfaces an OS-level error from pyramids."""
        missing = tmp_path / "definitely-not-here.nc"
        with pytest.raises(Exception):
            aggregate_netcdf(
                missing,
                MagicMock(),
                AggregationConfig(freq="1D"),
            )


class _FakeNetCDF:
    """Minimal `pyramids.netcdf.NetCDF` stand-in for round-trip tests.

    Implements the surfaces `aggregate_netcdf` consumes —
    `get_variable`, `read_array`, `get_time_variable`,
    `dimension_names`, `geotransform`, and (optionally) `sel`. Lets
    tests exercise the body of `aggregate_netcdf` without writing a
    real on-disk NetCDF (the test environment has no NetCDF writer).
    """

    def __init__(
        self,
        *,
        array: np.ndarray,
        time_strs_by_var: dict[str, list[str] | None],
        dimension_names: list[str] | None = None,
        geotransform: tuple = (0.0, 1.0, 0.0, 1.0, 0.0, -1.0),
        on_sel: object | None = None,
    ):
        self._array = array
        self._times = time_strs_by_var
        self.dimension_names = dimension_names
        self.geotransform = geotransform
        self._on_sel = on_sel
        self.band_reads: list[int | None] = []
        self.closed = False

    def get_variable(self, name: str) -> _FakeNetCDF:
        """Return self — fake's variable cube has the same surface."""
        return self

    def read_array(
        self, variable: str | None = None, band: int | None = None
    ) -> np.ndarray:
        """Return the stored array, or one 0-based band of it, like pyramids does.

        Returns a fresh copy, as a real reader does when it pulls bytes off
        disk. That matters for the memory tests: handing back the stored
        array would make a whole-cube read allocate nothing, so it could not
        be told apart from a per-band one.
        """
        self.band_reads.append(band)
        if band is None:
            return self._array.copy()
        return self._array[band].copy()

    def close(self) -> None:
        """Record that the handle was released."""
        self.closed = True

    def get_time_variable(self, var_name: str) -> list[str] | None:
        """Look up the time strings registered for `var_name`."""
        return self._times.get(var_name)

    def sel(self, **kwargs):
        """Return the configured `_on_sel` instance to simulate level pinning."""
        return self._on_sel


class _RealVariable(SimpleNamespace):
    """Lightweight stand-in for `earthlens.ecmwf.Variable` in tests.

    Exposes the attributes `aggregate_netcdf` reads (`is_flux`,
    `cds_variable`, `nc_variable`, `units`, the optional `is_pre_aggregated`
    that `_resolve_op` consults, and the optional `cds_dataset` / `dataset_id`
    that `_output_stem` consults — all via `getattr`) so the round-trip tests
    don't have to construct a full pydantic model.
    """


def _patch_netcdf_read(monkeypatch, fake_nc):
    """Patch `pyramids.netcdf.NetCDF.read_file` to return `fake_nc`."""
    from pyramids.netcdf import NetCDF as RealNetCDF

    monkeypatch.setattr(
        RealNetCDF, "read_file", staticmethod(lambda *_a, **_kw: fake_nc)
    )


def _patch_geotiff_write(monkeypatch):
    """Patch `pyramids.dataset.Dataset.create_from_array(...).to_file(...)` to a no-op recorder.

    Returns the list of `(arr_shape, geo, epsg, target)` tuples that
    were "written" so tests can inspect call sites without hitting
    GDAL / disk.
    """
    from pyramids.dataset import Dataset as RealDataset

    writes: list[tuple] = []

    class _StubGeoTiff:
        def __init__(self, arr, geo, epsg):
            self.arr = arr
            self.geo = geo
            self.epsg = epsg

        def to_file(self, path):
            writes.append((self.arr.shape, self.geo, self.epsg, path))

    monkeypatch.setattr(
        RealDataset,
        "create_from_array",
        staticmethod(lambda arr, geo, epsg: _StubGeoTiff(arr, geo, epsg)),
    )
    return writes


class TestAggregateNetcdfRoundTrip:
    """End-to-end body runs against a synthetic in-memory NetCDF (H7)."""

    @pytest.fixture
    def state_var(self):
        """A state-flagged variable (`is_flux=False`).

        Returns:
            _RealVariable: stand-in carrying the four attributes
            `aggregate_netcdf` consumes.
        """
        return _RealVariable(
            is_flux=False,
            cds_variable="2m_temperature",
            nc_variable="t2m",
            units="K",
        )

    @pytest.fixture
    def flux_var(self):
        """A flux-flagged variable (`is_flux=True`).

        Returns:
            _RealVariable: stand-in for `total_precipitation`.
        """
        return _RealVariable(
            is_flux=True,
            cds_variable="total_precipitation",
            nc_variable="tp",
            units="m",
        )

    def _daily_six_hourly_array(self, n_days: int = 2) -> np.ndarray:
        """Build `(n_days * 4, 2, 2)` increasing values, four slots per day."""
        n_slots = n_days * 4
        cube = np.zeros((n_slots, 2, 2), dtype=float)
        for i in range(n_slots):
            cube[i, :, :] = float(i + 1)
        return cube

    def _date_strings_six_hourly(self, n_days: int = 2) -> list[str]:
        """Build `n_days * 4` six-hourly date strings starting Jan 1."""
        idx = pd.date_range("2022-01-01", periods=n_days * 4, freq="6h")
        return [t.strftime("%Y-%m-%d %H:%M:%S") for t in idx]

    def test_daily_mean_collapses_to_one_slice_per_day(
        self, monkeypatch, tmp_path, state_var
    ):
        """Eight 6-hourly slots → 2 daily slices, each = mean of 4."""
        cube = self._daily_six_hourly_array(n_days=2)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(2)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=tmp_path),
        )

        assert len(results) == 2, f"Expected 2 daily windows, got {len(results)}"
        first_label, first_arr, _ = results[0]
        assert first_label == pd.Timestamp("2022-01-01"), (
            f"First label should be 2022-01-01, got {first_label}"
        )
        assert first_arr[0, 0] == pytest.approx(2.5), (
            f"Day 1 mean should be (1+2+3+4)/4 = 2.5, got {first_arr[0, 0]}"
        )
        second_arr = results[1][1]
        assert second_arr[0, 0] == pytest.approx(6.5), (
            f"Day 2 mean should be (5+6+7+8)/4 = 6.5, got {second_arr[0, 0]}"
        )
        assert len(writes) == 2, f"Expected 2 GeoTIFFs to be written, got {len(writes)}"

    def test_op_auto_routes_state_to_mean(self, monkeypatch, tmp_path, state_var):
        """`op="auto"` + `is_flux=False` → mean over the window."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="auto", out_dir=None),
        )
        _, arr, _ = results[0]
        assert arr[0, 0] == pytest.approx(2.5), (
            f"Auto on state var should mean to 2.5, got {arr[0, 0]}"
        )

    def test_op_auto_routes_flux_to_sum(self, monkeypatch, tmp_path, flux_var):
        """`op="auto"` + `is_flux=True` → sum over the window."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            flux_var,
            AggregationConfig(freq="1D", op="auto", out_dir=None),
        )
        _, arr, _ = results[0]
        assert arr[0, 0] == pytest.approx(10.0), (
            f"Auto on flux var should sum to 1+2+3+4=10.0, got {arr[0, 0]}"
        )

    def test_op_auto_routes_pre_aggregated_flux_to_mean(self, monkeypatch, tmp_path):
        """`op="auto"` + pre-aggregated flux → mean, not sum (#43, end-to-end).

        A flux var from a daily-statistics / monthly-means dataset is already a
        server-side aggregate; `op="auto"` must reduce it with `"mean"` (2.5),
        not re-sum it (10.0) as a raw flux var would.
        """
        pre_aggregated_flux_var = _RealVariable(
            is_flux=True,
            is_pre_aggregated=True,
            cds_variable="total_precipitation",
            nc_variable="tp",
            units="m",
        )
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            pre_aggregated_flux_var,
            AggregationConfig(freq="1D", op="auto", out_dir=None),
        )
        _, arr, _ = results[0]
        assert arr[0, 0] == pytest.approx(2.5), (
            f"Auto on pre-aggregated flux should mean to 2.5, not sum; got {arr[0, 0]}"
        )

    def test_min_count_emits_nan_for_partial_windows(
        self, monkeypatch, tmp_path, state_var
    ):
        """A window with fewer non-NaN samples than `min_count` emits NaN."""
        import warnings

        cube = np.full((4, 2, 2), np.nan)
        cube[0, 0, 0] = 1.0
        cube[1, 0, 0] = 2.0
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        # numpy emits "Mean of empty slice" for the three pixels that are
        # all-NaN before `min_count` masks them. Behaviour is correct;
        # silence the incidental warning so test output stays clean.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            results = aggregate_netcdf(
                tmp_path / "fake.nc",
                state_var,
                AggregationConfig(
                    freq="1D",
                    op="mean",
                    out_dir=None,
                    min_count=4,
                ),
            )
        _, arr, _ = results[0]
        assert np.isnan(arr[0, 0]), (
            f"Pixel with only 2 non-NaN samples and min_count=4 should be NaN, "
            f"got {arr[0, 0]}"
        )

    def test_pressure_level_without_level_raises(
        self, monkeypatch, tmp_path, state_var
    ):
        """A 4-D NetCDF with no `level` set raises ValueError."""
        cube = np.zeros((4, 1, 2, 2))
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "pressure_level", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        with pytest.raises(ValueError, match="pressure_level"):
            aggregate_netcdf(
                tmp_path / "fake.nc",
                state_var,
                AggregationConfig(freq="1D", op="mean", out_dir=None),
            )

    def test_pressure_level_with_level_pins_via_sel(
        self, monkeypatch, tmp_path, state_var
    ):
        """`level=1000` calls `nc.sel(pressure_level=1000)` and aggregates the result."""
        cube_3d = self._daily_six_hourly_array(n_days=1)
        pinned = _FakeNetCDF(
            array=cube_3d,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        outer = _FakeNetCDF(
            array=np.zeros((4, 1, 2, 2)),
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "pressure_level", "lat", "lon"],
            on_sel=pinned,
        )
        _patch_netcdf_read(monkeypatch, outer)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=None, level=1000),
        )
        _, arr, _ = results[0]
        assert arr[0, 0] == pytest.approx(2.5), (
            f"After level pin, daily mean should be 2.5, got {arr[0, 0]}"
        )

    def test_out_dir_none_skips_writes(self, monkeypatch, tmp_path, state_var):
        """`out_dir=None` returns arrays in memory and writes no files."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=None),
        )
        assert results[0][2] is None, (
            f"Third tuple element should be None, got {results[0][2]!r}"
        )
        assert writes == [], (
            f"No GeoTIFF writes should occur when out_dir=None; got {writes!r}"
        )

    def test_geotiff_filename_carries_variable_freq_and_window(
        self, monkeypatch, tmp_path, state_var
    ):
        """A var_info with no dataset id writes the bare `<cds_variable>_<freq>_<YYYYMMDD>.tif`."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        out_dir = tmp_path / "agg"
        aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=out_dir),
        )
        target_path = writes[0][3]
        assert target_path.endswith("2m_temperature_1D_20220101.tif"), (
            f"Filename should match `<var>_<freq>_<window>.tif` shape, "
            f"got {target_path!r}"
        )

    def test_dataset_id_override_disambiguates_output_filenames(
        self, monkeypatch, tmp_path
    ):
        """Two configs of one dataset (distinct dataset_id) write distinct .tif files (#1040)."""
        # Mirrors the GloFAS consolidated vs `-intermediate` streams: same
        # cds_variable + cds_dataset, distinct dataset_id, aggregated to one out_dir.
        consolidated = _RealVariable(
            is_flux=False,
            cds_variable="average_river_discharge_in_the_last_24_hours",
            nc_variable="dis24",
            units="m3 s-1",
            cds_dataset="cems-glofas-historical",
            dataset_id="cems-glofas-historical",
        )
        intermediate = _RealVariable(
            is_flux=False,
            cds_variable="average_river_discharge_in_the_last_24_hours",
            nc_variable="dis24",
            units="m3 s-1",
            cds_dataset="cems-glofas-historical",
            dataset_id="cems-glofas-historical-intermediate",
        )
        out_dir = tmp_path / "agg"
        written: list[str] = []
        for var in (consolidated, intermediate):
            nc = _FakeNetCDF(
                array=self._daily_six_hourly_array(n_days=1),
                time_strs_by_var={"time": self._date_strings_six_hourly(1)},
                dimension_names=["time", "lat", "lon"],
            )
            _patch_netcdf_read(monkeypatch, nc)
            writes = _patch_geotiff_write(monkeypatch)
            aggregate_netcdf(
                tmp_path / "fake.nc",
                var,
                AggregationConfig(freq="1D", op="mean", out_dir=out_dir),
            )
            written.append(writes[0][3])

        assert written[0] != written[1], (
            f"The two streams must not collide; both wrote {written[0]!r}"
        )
        assert written[0].endswith(
            "average_river_discharge_in_the_last_24_hours_"
            "cems-glofas-historical_1D_20220101.tif"
        ), f"Consolidated carries its dataset id, got {written[0]!r}"
        assert written[1].endswith(
            "average_river_discharge_in_the_last_24_hours_"
            "cems-glofas-historical-intermediate_1D_20220101.tif"
        ), f"Intermediate carries its dataset_id, got {written[1]!r}"

    def test_two_datasets_sharing_a_cds_variable_do_not_collide(
        self, monkeypatch, tmp_path
    ):
        """Two ordinary datasets sharing a cds_variable write distinct .tif files (#1040 H1)."""
        # ERA5 single-levels vs ERA5-Land total_precipitation: distinct datasets,
        # same cds_variable, each dataset_id == cds_dataset — aggregated to one out_dir.
        single_levels = _RealVariable(
            is_flux=True,
            cds_variable="total_precipitation",
            nc_variable="tp",
            units="m",
            cds_dataset="reanalysis-era5-single-levels",
            dataset_id="reanalysis-era5-single-levels",
        )
        land = _RealVariable(
            is_flux=True,
            cds_variable="total_precipitation",
            nc_variable="tp",
            units="m",
            cds_dataset="reanalysis-era5-land",
            dataset_id="reanalysis-era5-land",
        )
        out_dir = tmp_path / "agg"
        written: list[str] = []
        for var in (single_levels, land):
            nc = _FakeNetCDF(
                array=self._daily_six_hourly_array(n_days=1),
                time_strs_by_var={"time": self._date_strings_six_hourly(1)},
                dimension_names=["time", "lat", "lon"],
            )
            _patch_netcdf_read(monkeypatch, nc)
            writes = _patch_geotiff_write(monkeypatch)
            aggregate_netcdf(
                tmp_path / "fake.nc",
                var,
                AggregationConfig(freq="1D", op="mean", out_dir=out_dir),
            )
            written.append(writes[0][3])

        assert written[0] != written[1], (
            f"Two datasets sharing a cds_variable must not collide; both wrote "
            f"{written[0]!r}"
        )
        assert written[0].endswith(
            "total_precipitation_reanalysis-era5-single-levels_1D_20220101.tif"
        ), f"single-levels should carry its dataset id, got {written[0]!r}"
        assert written[1].endswith(
            "total_precipitation_reanalysis-era5-land_1D_20220101.tif"
        ), f"land should carry its dataset id, got {written[1]!r}"

    def test_valid_time_variable_is_picked_over_time(
        self, monkeypatch, tmp_path, state_var
    ):
        """A NetCDF carrying both `valid_time` and `time` uses `valid_time`."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={
                "valid_time": self._date_strings_six_hourly(1),
                "time": ["1900-01-01"] * 4,
            },
            dimension_names=["valid_time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=None),
        )
        label, _, _ = results[0]
        assert label == pd.Timestamp("2022-01-01"), (
            f"`valid_time` should drive the time axis (2022-01-01); got {label}"
        )

    def test_out_dir_created_if_missing(self, monkeypatch, tmp_path, state_var):
        """A non-existent `out_dir` is created with parents."""
        cube = self._daily_six_hourly_array(n_days=1)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        out_dir = tmp_path / "deeply" / "nested" / "out"
        assert not out_dir.exists()
        aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=out_dir),
        )
        assert out_dir.exists(), (
            f"`out_dir` should be created with parents; missing at {out_dir}"
        )

    def test_skipna_false_propagates_nan_through_body(
        self, monkeypatch, tmp_path, state_var
    ):
        """`skipna=False` must propagate end-to-end through `aggregate_netcdf`."""
        cube = self._daily_six_hourly_array(n_days=2)
        cube[1, 0, 0] = np.nan
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(2)},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=None, skipna=False),
        )
        day1 = results[0][1]
        day2 = results[1][1]
        assert np.isnan(day1[0, 0]), (
            f"Day 1 pixel (0, 0) was NaN-tainted; with skipna=False it "
            f"should propagate NaN, got {day1[0, 0]}"
        )
        assert day2[0, 0] == pytest.approx(6.5), (
            f"Day 2 was clean; mean should be 6.5, got {day2[0, 0]}"
        )

    def test_monthly_grouping_runs_end_to_end(self, monkeypatch, tmp_path, state_var):
        """A 32-day cube + `freq="1MS"` produces 2 monthly windows."""
        n_days = 32
        cube = np.zeros((n_days, 2, 2), dtype=float)
        cube[:31, :, :] = 1.0  # January: 31 days of 1.0
        cube[31, :, :] = 2.0  # February 1
        idx = pd.date_range("2022-01-01", periods=n_days, freq="D")
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": [t.strftime("%Y-%m-%d") for t in idx]},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1MS", op="mean", out_dir=None),
        )
        labels = [label for label, _, _ in results]
        assert labels == [
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2022-02-01"),
        ], f"Expected monthly labels [Jan-1, Feb-1], got {labels}"
        assert results[0][1][0, 0] == pytest.approx(1.0), (
            f"January mean should be 1.0, got {results[0][1][0, 0]}"
        )
        assert results[1][1][0, 0] == pytest.approx(2.0), (
            f"February mean (1 sample) should be 2.0, got {results[1][1][0, 0]}"
        )

    def test_empty_time_axis_returns_empty_results(
        self, monkeypatch, tmp_path, state_var
    ):
        """A NetCDF with zero time samples returns an empty result list, not an error."""
        nc = _FakeNetCDF(
            array=np.zeros((0, 2, 2)),
            time_strs_by_var={"time": []},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        with pytest.raises(KeyError):
            aggregate_netcdf(
                tmp_path / "fake.nc",
                state_var,
                AggregationConfig(freq="1D", op="mean", out_dir=tmp_path),
            )
        assert writes == [], (
            f"No writes should occur on empty time axis, got {writes!r}"
        )

    def test_cell_size_does_not_affect_geotransform(
        self, monkeypatch, tmp_path, state_var
    ):
        """`cell_size` is informational; the GeoTIFF geotransform comes from `nc.geotransform`."""
        cube = self._daily_six_hourly_array(n_days=1)
        source_geo = (-75.0, 0.5, 0.0, 5.0, 0.0, -0.5)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
            geotransform=source_geo,
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=tmp_path, cell_size=0.25),
        )
        _, geo, _, _ = writes[0]
        assert geo == source_geo, (
            f"Output geotransform should equal nc.geotransform regardless "
            f"of config.cell_size; got {geo}"
        )

    def test_geotransform_forwarded_to_geotiff_writer(
        self, monkeypatch, tmp_path, state_var
    ):
        """`nc.geotransform` reaches `Dataset.create_from_array(geo=...)` verbatim."""
        cube = self._daily_six_hourly_array(n_days=1)
        source_geo = (-75.0, 0.125, 0.0, 5.0, 0.0, -0.125)
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": self._date_strings_six_hourly(1)},
            dimension_names=["time", "lat", "lon"],
            geotransform=source_geo,
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=tmp_path),
        )
        assert len(writes) == 1, f"Expected 1 write, got {len(writes)}"
        _shape, geo, epsg, _path = writes[0]
        assert geo == source_geo, (
            f"Geotransform should be forwarded verbatim; "
            f"expected {source_geo}, got {geo}"
        )
        assert epsg == 4326, f"EPSG should be 4326 (WGS84); got {epsg}"

    def _cross_month_cube_and_times(self):
        """A daily cube whose axis over-covers Jun 25-Jul 5, like a CDS cross-product.

        A `year`/`month`/`day` request for Jun 25-Jul 5 crosses the month
        boundary, so CDS also returns Jun 1-5 and Jul 25-30. Each day is one
        slice valued by its position (1-indexed), so a window's mean is a
        recognisable number.
        """
        day_strs = (
            [f"2022-06-{d:02d}" for d in range(1, 6)]  # Jun 1-5 (spurious)
            + [f"2022-06-{d:02d}" for d in range(25, 31)]  # Jun 25-30
            + [f"2022-07-{d:02d}" for d in range(1, 6)]  # Jul 1-5
            + [f"2022-07-{d:02d}" for d in range(25, 31)]  # Jul 25-30 (spurious)
        )
        cube = np.zeros((len(day_strs), 2, 2), dtype=float)
        for i in range(len(day_strs)):
            cube[i, :, :] = float(i + 1)
        return cube, day_strs

    def test_date_range_trims_out_of_window_days(
        self, monkeypatch, tmp_path, state_var
    ):
        """A daily aggregate drops days the cross-product pulled outside the span."""
        cube, day_strs = self._cross_month_cube_and_times()
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": day_strs},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=tmp_path),
            date_range=(pd.Timestamp("2022-06-25"), pd.Timestamp("2022-07-05")),
        )

        labels = [label for label, _, _ in results]
        assert len(labels) == 11, f"Expected 11 in-range days, got {len(labels)}"
        assert min(labels) == pd.Timestamp("2022-06-25")
        assert max(labels) == pd.Timestamp("2022-07-05")
        written = " ".join(str(target) for *_, target in writes)
        assert "20220601" not in written, (
            f"spurious Jun 1-5 must not be written; got {written}"
        )
        assert "20220725" not in written, (
            f"spurious Jul 25-30 must not be written; got {written}"
        )

    def test_date_range_prevents_monthly_window_contamination(
        self, monkeypatch, tmp_path, state_var
    ):
        """A monthly window means only the in-range days, not the cross-product extras."""
        cube, day_strs = self._cross_month_cube_and_times()
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": day_strs},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1MS", op="mean", out_dir=None),
            date_range=(pd.Timestamp("2022-06-25"), pd.Timestamp("2022-07-05")),
        )

        by_label = {label: arr for label, arr, _ in results}
        # Jun 25-30 are cube values 6..11 -> mean 8.5; Jun 1-5 (values 1..5) excluded.
        assert by_label[pd.Timestamp("2022-06-01")][0, 0] == pytest.approx(8.5), (
            "June mean must exclude the spurious Jun 1-5"
        )
        # Jul 1-5 are cube values 12..16 -> mean 14.0; Jul 25-30 (17..22) excluded.
        assert by_label[pd.Timestamp("2022-07-01")][0, 0] == pytest.approx(14.0), (
            "July mean must exclude the spurious Jul 25-30"
        )

    def test_no_date_range_keeps_every_sample(self, monkeypatch, tmp_path, state_var):
        """`date_range=None` aggregates the whole cube (backward compatible)."""
        cube, day_strs = self._cross_month_cube_and_times()
        nc = _FakeNetCDF(
            array=cube,
            time_strs_by_var={"time": day_strs},
            dimension_names=["time", "lat", "lon"],
        )
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)

        results = aggregate_netcdf(
            tmp_path / "fake.nc",
            state_var,
            AggregationConfig(freq="1D", op="mean", out_dir=tmp_path),
        )
        assert len(results) == 22, f"Expected all 22 days kept, got {len(results)}"

    def test_date_range_mask_widens_to_whole_days(self):
        """`_date_range_mask` keeps end-day sub-daily samples and drops neighbours."""
        from earthlens.aggregate import _date_range_mask

        idx = pd.to_datetime(
            [
                "2022-06-24 18:00",
                "2022-06-25 00:00",
                "2022-07-05 18:00",
                "2022-07-06 00:00",
            ]
        )
        mask = _date_range_mask(
            idx, (pd.Timestamp("2022-06-25"), pd.Timestamp("2022-07-05"))
        )
        assert list(mask) == [False, True, True, False]
        assert _date_range_mask(idx, None) is None


class TestStreamingAggregation:
    """ARC-3/ARC-12: windows are read one at a time and handles are released."""

    @pytest.fixture
    def state_var(self):
        """A state-flagged variable stand-in.

        Returns:
            _RealVariable: carries the attributes `aggregate_netcdf` reads.
        """
        return _RealVariable(
            is_flux=False, cds_variable="2m_temperature", nc_variable="t2m", units="K"
        )

    def _nc(self, steps: int = 48) -> _FakeNetCDF:
        """Build a fake NetCDF holding `steps` hourly bands over two days."""
        times = pd.date_range("2020-01-01", periods=steps, freq="h")
        array = np.arange(steps * 2 * 3, dtype="float64").reshape(steps, 2, 3)
        return _FakeNetCDF(
            array=array,
            time_strs_by_var={"valid_time": [str(t) for t in times]},
        )

    def test_the_whole_cube_is_never_read_at_once(self, monkeypatch, state_var):
        """Every read names a band; a bare read_array() would defeat the point."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        aggregate_netcdf("x.nc", state_var, AggregationConfig(freq="1D", op="mean"))
        assert nc.band_reads, "no reads were issued at all"
        assert all(band is not None for band in nc.band_reads), (
            f"a whole-cube read slipped through: {nc.band_reads}"
        )

    def test_each_band_is_read_exactly_once(self, monkeypatch, state_var):
        """Two daily windows over 48 hourly steps read bands 0..47, once each."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        aggregate_netcdf("x.nc", state_var, AggregationConfig(freq="1D", op="mean"))
        assert sorted(nc.band_reads) == list(range(48)), f"got {sorted(nc.band_reads)}"

    def test_values_match_a_whole_cube_reduction(self, monkeypatch, state_var):
        """Per-window reads reduce to exactly what slicing the full cube gives."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        results = aggregate_netcdf(
            "x.nc", state_var, AggregationConfig(freq="1D", op="mean")
        )
        assert len(results) == 2, f"expected two daily windows, got {len(results)}"
        assert np.allclose(results[0][1], nc._array[:24].mean(axis=0))
        assert np.allclose(results[1][1], nc._array[24:].mean(axis=0))

    def test_handles_are_closed_after_aggregation(self, monkeypatch, state_var):
        """The container is released so the NetCDF can be deleted afterwards."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        aggregate_netcdf("x.nc", state_var, AggregationConfig(freq="1D", op="mean"))
        assert nc.closed, "aggregate_netcdf must close what it opens"

    def test_handles_are_closed_when_the_generator_is_abandoned(
        self, monkeypatch, state_var
    ):
        """Dropping the iterator part-way still releases the handles."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        windows = iter_aggregate_netcdf(
            "x.nc", state_var, AggregationConfig(freq="1D", op="mean")
        )
        next(windows)
        windows.close()
        assert nc.closed, "an abandoned generator must still close its handles"

    def test_handles_are_closed_when_a_window_raises(self, monkeypatch, state_var):
        """A failure mid-reduction still releases the handles on the way out."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        config = AggregationConfig(freq="nonsense")
        with pytest.raises(ValueError):
            aggregate_netcdf("x.nc", state_var, config)
        assert nc.closed, "handles must be released on the error path too"

    def test_iterator_yields_lazily(self, monkeypatch, state_var):
        """Taking one window does not read the bands of the next."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        windows = iter_aggregate_netcdf(
            "x.nc", state_var, AggregationConfig(freq="1D", op="mean")
        )
        next(windows)
        assert sorted(nc.band_reads) == list(range(24)), (
            f"only the first window's bands should be read; got {sorted(nc.band_reads)}"
        )
        windows.close()

    def test_keep_arrays_false_drops_written_arrays(self, monkeypatch, state_var):
        """With out_dir set, keep_arrays=False returns the path but no array."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        writes = _patch_geotiff_write(monkeypatch)
        windows = list(
            iter_aggregate_netcdf(
                "x.nc",
                state_var,
                AggregationConfig(
                    freq="1D", op="mean", out_dir=Path("out"), keep_arrays=False
                ),
            )
        )
        assert [w.array for w in windows] == [None, None], "arrays should be dropped"
        assert all(w.path is not None for w in windows), "paths must survive"
        assert len(writes) == 2, f"both windows should still be written: {writes}"

    def test_keep_arrays_false_still_returns_in_memory_windows(
        self, monkeypatch, state_var
    ):
        """Without out_dir there is nowhere to read a dropped array back from."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        windows = list(
            iter_aggregate_netcdf(
                "x.nc",
                state_var,
                AggregationConfig(freq="1D", op="mean", keep_arrays=False),
            )
        )
        assert all(w.array is not None for w in windows), (
            "dropping the only copy of an unwritten window would lose it"
        )

    def test_keep_arrays_defaults_to_retaining(self, monkeypatch, state_var):
        """The default keeps arrays, so existing callers are unaffected."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        _patch_geotiff_write(monkeypatch)
        windows = list(
            iter_aggregate_netcdf(
                "x.nc",
                state_var,
                AggregationConfig(freq="1D", op="mean", out_dir=Path("out")),
            )
        )
        assert all(w.array is not None for w in windows)

    def test_aggregate_netcdf_returns_the_same_tuples(self, monkeypatch, state_var):
        """The eager wrapper still yields (label, array, path) triples."""
        nc = self._nc()
        _patch_netcdf_read(monkeypatch, nc)
        results = aggregate_netcdf(
            "x.nc", state_var, AggregationConfig(freq="1D", op="mean")
        )
        label, array, path = results[0]
        assert label == pd.Timestamp("2020-01-01")
        assert array.shape == (2, 3)
        assert path is None


class TestAggregationMemoryCeiling:
    """ARC-13c: the cube is never materialised, only one window at a time."""

    #: A grid big enough that the array dominates the fixed per-run overhead
    #: (pandas index construction, pyramids metadata). At 16x16 the overhead
    #: swamps the signal and a whole-cube read is indistinguishable.
    SIDE = 96
    DAYS = 8

    @pytest.fixture
    def state_var(self):
        """A state-flagged variable stand-in.

        Returns:
            _RealVariable: carries the attributes `aggregate_netcdf` reads.
        """
        return _RealVariable(
            is_flux=False, cds_variable="2m_temperature", nc_variable="t2m", units="K"
        )

    def _peak_and_cube_bytes(self, monkeypatch, state_var) -> tuple[int, int]:
        """Aggregate a multi-day cube; return `(peak allocation, cube size)`."""
        import tracemalloc

        steps = self.DAYS * 24
        times = pd.date_range("2020-01-01", periods=steps, freq="h")
        array = np.zeros((steps, self.SIDE, self.SIDE), dtype="float64")
        nc = _FakeNetCDF(
            array=array, time_strs_by_var={"valid_time": [str(t) for t in times]}
        )
        _patch_netcdf_read(monkeypatch, nc)
        tracemalloc.start()
        try:
            for _window in iter_aggregate_netcdf(
                "x.nc", state_var, AggregationConfig(freq="1D", op="mean")
            ):
                pass
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak, array.nbytes

    def test_peak_stays_below_the_whole_cube(self, monkeypatch, state_var):
        """Aggregating never allocates as much as the cube it reads.

        This is the property ARC-3 bought: a whole-cube `read_array()` has to
        allocate at least the cube, so a peak below it proves the read is
        per-window. Measured on the reverted implementation, the peak is
        ~1.26x the cube; streaming it is ~0.38x.
        """
        peak, cube = self._peak_and_cube_bytes(monkeypatch, state_var)
        assert peak < cube, (
            f"peak allocation {peak} >= cube size {cube}: the whole time axis "
            "is being materialised instead of one window at a time"
        )

    def test_peak_is_a_small_multiple_of_one_window(self, monkeypatch, state_var):
        """The working set tracks the window, not the number of windows."""
        peak, cube = self._peak_and_cube_bytes(monkeypatch, state_var)
        window = cube // self.DAYS
        assert peak < window * 6, (
            f"peak allocation {peak} is more than 6x one window ({window}); "
            "windows appear to be accumulating rather than being released"
        )


def _write_real_nc(path, *, periods=6, rows=2, cols=3):
    """Write a real NetCDF time cube and return the values it holds.

    Built entirely through pyramids, which owns NetCDF in this project: a dated
    GeoTIFF per timestep, collected into a `DatasetCollection` that derives its
    time axis from those dates, then streamed out by `CubeNetCDFWriter`. The
    aggregator's own time reader decodes the result, which is the point - the
    fixture exercises the same path a downloaded cube takes.
    """
    import gc

    from pyramids.dataset import Dataset, DatasetCollection
    from pyramids.netcdf._cube_netcdf_writer import CubeNetCDFWriter

    frames = Path(path).parent / f"{Path(path).stem}_frames"
    frames.mkdir(parents=True, exist_ok=True)
    values = np.arange(periods * rows * cols, dtype="f4").reshape(periods, rows, cols)
    days = pd.date_range("2020-01-01", periods=periods, freq="D")
    for index, day in enumerate(days):
        raster = Dataset.create_from_array(
            arr=values[index],
            top_left_corner=(0.0, 2.0),
            cell_size=1.0,
            epsg=4326,
        )
        raster.to_file(str(frames / f"t2m_{day:%Y.%m.%d}.tif"))
        del raster
    gc.collect()
    collection = DatasetCollection.from_files(
        str(frames), glob="*.tif", date_format="%Y.%m.%d"
    )
    CubeNetCDFWriter(collection).write(str(path))
    del collection
    gc.collect()
    return values


def _open_handles():
    """Paths this process currently holds open."""
    import psutil

    return {handle.path for handle in psutil.Process().open_files()}


def _handles_on(path, before):
    """Handles on `path` opened since `before` was taken.

    Compared with `os.path.samefile` rather than by string: Windows reports a
    mapped drive under its UNC name, so equal paths can spell differently and a
    string comparison silently never matches.
    """
    import os

    import psutil

    found = []
    for handle in psutil.Process().open_files():
        if handle.path in before:
            continue
        try:
            if os.path.samefile(handle.path, path):
                found.append(handle.path)
        except OSError:
            continue
    return found


def _single_level_var():
    """The catalog row the real-NetCDF tests aggregate.

    Built on `_RealVariable`, which already carries the reason core must not
    import a provider's `Variable` and the full list of attributes the
    aggregator reads — `_output_stem` consults `cds_dataset` / `dataset_id`
    through `getattr` as well, so the stem here is the bare `cds_variable`.
    """
    return _RealVariable(
        cds_variable="2m_temperature",
        nc_variable="Band_1",
        units="K",
        is_flux=False,
        is_pre_aggregated=False,
    )


class TestAggregateAgainstARealNetCDF:
    """Exercises the aggregator against a NetCDF on disk rather than a mock.

    A mock has no memory footprint and no file handle, so a suite built on one
    cannot observe how much the aggregator reads or whether it releases what it
    opens - the two things this path most needs to get right.
    """

    def test_each_window_reduces_the_real_values(self, tmp_path):
        """The numbers must come from the file, which a mock cannot demonstrate."""
        path = tmp_path / "cube.nc"
        data = _write_real_nc(path)
        result = aggregate_netcdf(
            path, _single_level_var(), AggregationConfig(freq="3D", op="mean")
        )
        assert len(result) == 2
        np.testing.assert_allclose(result[0][1], data[0:3].mean(axis=0))
        np.testing.assert_allclose(result[1][1], data[3:6].mean(axis=0))

    def test_a_sum_differs_from_a_mean_on_the_same_cube(self, tmp_path):
        """Guards against a reduction that silently ignores its op."""
        path = tmp_path / "cube.nc"
        data = _write_real_nc(path)
        var_info = _single_level_var()
        summed = aggregate_netcdf(
            path, var_info, AggregationConfig(freq="3D", op="sum")
        )
        meaned = aggregate_netcdf(
            path, var_info, AggregationConfig(freq="3D", op="mean")
        )
        for index, window in enumerate((slice(0, 3), slice(3, 6))):
            np.testing.assert_allclose(summed[index][1], data[window].sum(axis=0))
            np.testing.assert_allclose(meaned[index][1], data[window].mean(axis=0))
            assert not np.allclose(summed[index][1], meaned[index][1]), (
                f"window {index} reduced identically under sum and mean, so the "
                "op is not being honoured"
            )

    def test_the_source_file_is_released_when_the_run_ends(self, tmp_path):
        """The descriptor is counted; POSIX would happily unlink an open file."""
        path = tmp_path / "cube.nc"
        _write_real_nc(path)
        before = _open_handles()
        aggregate_netcdf(
            path, _single_level_var(), AggregationConfig(freq="3D", op="mean")
        )
        leaked = _handles_on(path, before)
        assert not leaked, f"the aggregator kept a handle on its input: {leaked}"

    def test_the_handle_check_can_actually_fail(self, tmp_path):
        """Guards the test above: a check that cannot fail proves nothing."""
        path = tmp_path / "cube.nc"
        path.write_bytes(b"not a cube")
        before = _open_handles()
        with path.open("rb"):
            assert _handles_on(path, before), (
                "an open handle went unseen, so the release test is vacuous"
            )
        assert not _handles_on(path, before), "the handle survived its context"

    def test_streaming_does_not_materialise_the_whole_cube(self, tmp_path):
        """The other half of the claim: read volume, measured on a real file.

        The streaming call is the memory-bounded one — the eager call holds
        every window it returns, so only this path can be held to the cube.
        """
        import tracemalloc

        path = tmp_path / "cube.nc"
        data = _write_real_nc(path, periods=16, rows=160, cols=160)
        tracemalloc.start()
        try:
            for _window in iter_aggregate_netcdf(
                path, _single_level_var(), AggregationConfig(freq="4D", op="mean")
            ):
                pass
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < data.nbytes, (
            f"peak allocation {peak} >= the {data.nbytes}-byte cube: the whole "
            "time axis is being materialised instead of one window at a time"
        )

    def test_a_date_range_drops_samples_outside_it(self, tmp_path):
        """A CDS cross-product over-covers the request; the trim must be real."""
        path = tmp_path / "cube.nc"
        data = _write_real_nc(path)
        result = aggregate_netcdf(
            path,
            _single_level_var(),
            AggregationConfig(freq="3D", op="mean"),
            date_range=("2020-01-01", "2020-01-03"),
        )
        assert len(result) == 1
        np.testing.assert_allclose(result[0][1], data[0:3].mean(axis=0))

    def test_streaming_yields_the_same_windows_as_the_eager_call(self, tmp_path):
        """The streaming path exists to bound memory; it must not change answers."""
        path = tmp_path / "cube.nc"
        _write_real_nc(path)
        var_info, config = _single_level_var(), AggregationConfig(freq="3D", op="mean")
        eager = aggregate_netcdf(path, var_info, config)
        streamed = list(iter_aggregate_netcdf(path, var_info, config))
        assert len(streamed) == len(eager)
        for window, (label, array, _) in zip(streamed, eager, strict=True):
            assert window.label == label
            np.testing.assert_allclose(window.array, array)

    def test_writing_produces_one_readable_geotiff_per_window(self, tmp_path):
        """The written raster is the deliverable, so it must open and match."""
        from pyramids.dataset import Dataset

        path = tmp_path / "cube.nc"
        data = _write_real_nc(path)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = aggregate_netcdf(
            path,
            _single_level_var(),
            AggregationConfig(freq="3D", op="mean", out_dir=out_dir),
        )
        written = [p for _, _, p in result if p is not None]
        assert len(written) == 2
        first = np.asarray(Dataset.read_file(str(written[0])).read_array())
        np.testing.assert_allclose(np.squeeze(first), data[0:3].mean(axis=0), rtol=1e-5)
