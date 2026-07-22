"""Unit tests for the pure PVGIS helpers (`earthlens.pvgis._helpers`)."""

from __future__ import annotations

import pandas as pd
import pytest
import requests

from earthlens.base import SpatialExtent
from earthlens.pvgis import _helpers as h

from .conftest import FakeResponse, FakeSession

pytestmark = pytest.mark.pvgis


def _bbox(lat0: float, lat1: float, lon0: float, lon1: float) -> SpatialExtent:
    """Build a SpatialExtent from explicit edges."""
    return SpatialExtent.from_pairs(lat_lim=[lat0, lat1], lon_lim=[lon0, lon1])


class TestPointGrid:
    """Tests for `point_grid`."""

    def test_grid_count_and_corners(self):
        """A 2x2-degree bbox at 1-degree spacing yields a 3x3 grid."""
        grid = h.point_grid(_bbox(45.0, 47.0, 8.0, 10.0), 1.0)
        assert len(grid) == 9, f"expected 9 points, got {len(grid)}"
        assert grid[0] == (45.0, 8.0), f"first corner wrong: {grid[0]}"
        assert grid[-1] == (47.0, 10.0), f"last corner wrong: {grid[-1]}"

    def test_single_point_bbox(self):
        """A degenerate (collapsed) bbox yields exactly one coordinate."""
        assert h.point_grid(_bbox(45.0, 45.0, 8.0, 8.0), 0.1) == [(45.0, 8.0)]

    def test_latitude_major_order(self):
        """Points are emitted latitude-major (all lons for the south row first)."""
        grid = h.point_grid(_bbox(45.0, 46.0, 8.0, 9.0), 1.0)
        assert grid == [(45.0, 8.0), (45.0, 9.0), (46.0, 8.0), (46.0, 9.0)]

    def test_non_positive_spacing_raises(self):
        """A non-positive spacing raises ValueError."""
        with pytest.raises(ValueError, match="spacing_deg must be positive"):
            h.point_grid(_bbox(45.0, 46.0, 8.0, 9.0), 0.0)


class TestBuildUrl:
    """Tests for `build_url`."""

    def test_fixed_params_lead(self):
        """The URL leads with lat / lon / outputformat then the extra params."""
        url = h.build_url("seriescalc", 45.0, 8.0, {"startyear": 2020})
        assert url.startswith(f"{h.BASE}/seriescalc?"), f"bad base: {url}"
        assert "lat=45.0" in url and "lon=8.0" in url, url
        assert "outputformat=json" in url, url
        assert "startyear=2020" in url, url

    def test_no_extra_params(self):
        """An empty params dict still emits the three fixed params."""
        url = h.build_url("tmy", 10.0, 20.0, {})
        assert url == f"{h.BASE}/tmy?lat=10.0&lon=20.0&outputformat=json", url


class TestThrottledGet:
    """Tests for `throttled_get`."""

    def test_sleeps_to_honour_rate_limit(self):
        """A near-zero clock forces a throttle sleep before the request."""
        slept: list[float] = []
        session = FakeSession(FakeResponse({"ok": 1}))
        resp = h.throttled_get(
            session,
            "u",
            last_call=[0.0],
            sleep=slept.append,
            monotonic=lambda: 0.0,
        )
        assert resp.status_code == 200, "expected the 200 response back"
        assert slept and slept[0] > 0, (
            f"expected a positive throttle sleep, got {slept}"
        )

    def test_no_sleep_when_interval_elapsed(self):
        """A clock far past the last call skips the throttle sleep."""
        slept: list[float] = []
        session = FakeSession(FakeResponse({"ok": 1}))
        h.throttled_get(
            session,
            "u",
            last_call=[0.0],
            sleep=slept.append,
            monotonic=lambda: 100.0,
        )
        assert slept == [], f"expected no throttle sleep, got {slept}"

    def test_retries_429_then_succeeds(self):
        """A 429 is retried with exponential backoff until a non-429 arrives."""
        slept: list[float] = []
        session = FakeSession([FakeResponse(None, 429), FakeResponse({"ok": 1})])
        resp = h.throttled_get(
            session,
            "u",
            last_call=[0.0],
            sleep=slept.append,
            monotonic=lambda: 100.0,
        )
        assert resp.status_code == 200, "should return the first non-429 response"
        assert len(session.calls) == 2, f"expected 2 GETs, got {len(session.calls)}"
        assert slept == [1], f"expected one backoff sleep of 2**0, got {slept}"

    def test_exhausts_retries_and_raises(self):
        """All-429 responses exhaust the retries and raise via raise_for_status."""
        session = FakeSession([FakeResponse(None, 429)])
        with pytest.raises(requests.HTTPError):
            h.throttled_get(
                session,
                "u",
                last_call=[100.0],
                max_retries=3,
                sleep=lambda _s: None,
                monotonic=lambda: 100.0,
            )
        assert len(session.calls) == 3, f"expected 3 attempts, got {len(session.calls)}"


class TestParsers:
    """Tests for `parse_seriescalc` / `parse_tmy` and `_records_to_frame`."""

    def test_parse_seriescalc(self, seriescalc_payload):
        """seriescalc parses to one row per hourly record with a UTC-aware time."""
        df = h.parse_seriescalc(seriescalc_payload)
        n = len(seriescalc_payload["outputs"]["hourly"])
        assert len(df) == n, f"expected {n} rows, got {len(df)}"
        assert pd.api.types.is_datetime64_any_dtype(df["time"]), df["time"].dtype
        assert str(df["time"].dt.tz) == "UTC", df["time"].dt.tz
        assert {"G(i)", "T2m"}.issubset(df.columns), list(df.columns)

    def test_parse_tmy_normalizes_time_utc(self, tmy_payload):
        """TMY's `time(UTC)` key is normalised to a UTC-aware `time` column."""
        df = h.parse_tmy(tmy_payload)
        assert "time" in df.columns and "time(UTC)" not in df.columns, list(df.columns)
        assert pd.api.types.is_datetime64_any_dtype(df["time"]), df["time"].dtype
        assert str(df["time"].dt.tz) == "UTC", df["time"].dt.tz
        assert "RH" in df.columns, list(df.columns)

    def test_records_to_frame_missing_time_key(self):
        """A record list without the time key passes through without a time col."""
        df = h._records_to_frame([{"value": 1}], "time")
        assert "time" not in df.columns, list(df.columns)
        assert df["value"].tolist() == [1], df["value"].tolist()


class TestMisc:
    """Tests for `error_message` and `empty_canonical`."""

    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"message": "over the sea", "status": 400}, "over the sea"),
            ({"status": 400}, ""),
            ("not a dict", ""),
        ],
    )
    def test_error_message(self, payload, expected):
        """`error_message` returns the message field or empty for odd inputs."""
        assert h.error_message(payload) == expected, h.error_message(payload)

    def test_empty_canonical(self):
        """`empty_canonical` returns a zero-row frame with the given columns."""
        df = h.empty_canonical(["time", "G(i)"])
        assert list(df.columns) == ["time", "G(i)"], list(df.columns)
        assert df.empty, "frame should have no rows"
