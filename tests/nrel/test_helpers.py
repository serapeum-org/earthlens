"""Unit tests for the pure NREL helpers (`earthlens.nrel._helpers`)."""

from __future__ import annotations

import pytest
import requests

from earthlens.base import SpatialExtent
from earthlens.nrel import _helpers as h

from .conftest import FakeResponse, FakeSession

pytestmark = pytest.mark.nrel


def _bbox(lat0: float, lat1: float, lon0: float, lon1: float) -> SpatialExtent:
    """Build a SpatialExtent from explicit edges."""
    return SpatialExtent.from_pairs(lat_lim=[lat0, lat1], lon_lim=[lon0, lon1])


class TestPointGrid:
    """Tests for `point_grid`."""

    def test_grid_count_and_corners(self):
        """A 2x2-degree bbox at 1-degree spacing yields a 3x3 grid."""
        grid = h.point_grid(_bbox(39.0, 41.0, -106.0, -104.0), 1.0)
        assert len(grid) == 9
        assert grid[0] == (39.0, -106.0)
        assert grid[-1] == (41.0, -104.0)

    def test_single_point_bbox(self):
        """A degenerate bbox yields exactly one coordinate."""
        assert h.point_grid(_bbox(39.74, 39.74, -105.18, -105.18), 0.05) == [
            (39.74, -105.18)
        ]

    def test_latitude_major_order(self):
        """Points are emitted latitude-major (all lons for the south row first)."""
        grid = h.point_grid(_bbox(39.0, 40.0, -106.0, -105.0), 1.0)
        assert grid == [(39.0, -106.0), (39.0, -105.0), (40.0, -106.0), (40.0, -105.0)]

    def test_non_positive_spacing_raises(self):
        """A non-positive spacing raises ValueError."""
        with pytest.raises(ValueError, match="spacing_deg must be positive"):
            h.point_grid(_bbox(39.0, 40.0, -106.0, -105.0), 0.0)


class TestBuildUrl:
    """Tests for `build_url`."""

    def test_url_carries_all_required_params(self):
        """The URL is on NLR_HOST and carries key/email/wkt/names/attributes."""
        url = h.build_url(
            "/api/nsrdb/v2/solar/x.csv",
            39.74,
            -105.18,
            2020,
            ["ghi", "dni"],
            api_key="KEY",
            email="me@example.com",
        )
        assert url.startswith(h.NLR_HOST + "/api/nsrdb/v2/solar/x.csv?")
        assert "api_key=KEY" in url
        assert "email=me%40example.com" in url
        assert "wkt=POINT%28-105.18+39.74%29" in url
        assert "names=2020" in url
        assert "attributes=ghi%2Cdni" in url
        assert "interval=60" in url
        assert "utc=false" in url

    def test_interval_and_utc_overrides(self):
        """Explicit interval / utc are reflected in the query string."""
        url = h.build_url(
            "/x.csv",
            1.0,
            2.0,
            "tmy",
            ["ghi"],
            api_key="K",
            email="e@x.com",
            interval=30,
            utc="true",
        )
        assert "interval=30" in url and "utc=true" in url and "names=tmy" in url


class TestThrottledGet:
    """Tests for `throttled_get`."""

    def test_returns_first_non_429(self):
        """A 200 response is returned without retry."""
        session = FakeSession(FakeResponse(text="ok", status_code=200))
        resp = h.throttled_get(
            session, "u", last_call=[0.0], sleep=lambda s: None, monotonic=lambda: 100.0
        )
        assert resp.status_code == 200
        assert session.calls == ["u"]

    def test_waits_when_called_too_soon(self, monkeypatch: pytest.MonkeyPatch):
        """A call within MIN_INTERVAL sleeps for the remaining time."""
        monkeypatch.setattr(h, "MIN_INTERVAL", 1.0)
        slept: list[float] = []
        session = FakeSession(FakeResponse(text="ok", status_code=200))
        h.throttled_get(
            session,
            "u",
            last_call=[100.0],
            sleep=slept.append,
            monotonic=lambda: 100.5,
        )
        assert slept and slept[0] == pytest.approx(0.5)

    def test_retries_429_then_succeeds(self):
        """A 429 is retried with backoff until a non-429 response arrives."""
        session = FakeSession(
            [FakeResponse(status_code=429), FakeResponse(text="ok", status_code=200)]
        )
        resp = h.throttled_get(
            session, "u", last_call=[0.0], sleep=lambda s: None, monotonic=lambda: 0.0
        )
        assert resp.status_code == 200
        assert len(session.calls) == 2

    def test_all_429_raises(self):
        """Exhausting the retries on a persistent 429 raises HTTPError."""
        session = FakeSession(FakeResponse(status_code=429))
        with pytest.raises(requests.HTTPError):
            h.throttled_get(
                session,
                "u",
                last_call=[0.0],
                max_retries=2,
                sleep=lambda s: None,
                monotonic=lambda: 0.0,
            )


class TestParseCsv:
    """Tests for `parse_psm3_csv` and the header-offset detector."""

    def test_nsrdb_auto_detects_two_metadata_rows(self, nsrdb_csv: str):
        """The NSRDB fixture (2 metadata rows) parses to its data rows."""
        df = h.parse_psm3_csv(nsrdb_csv)
        assert len(df) == 3
        assert df["time"].dtype.kind == "M"
        assert {"GHI", "DNI", "DHI", "Temperature", "Wind Speed"}.issubset(df.columns)

    def test_wtk_auto_detects_one_metadata_row(self, wtk_csv: str):
        """The WTK fixture (1 metadata row) parses to its data rows."""
        df = h.parse_psm3_csv(wtk_csv)
        assert len(df) == 3
        assert df["time"].dtype.kind == "M"
        assert "wind speed at 100m (m/s)" in df.columns

    def test_explicit_meta_rows_override(self, nsrdb_csv: str):
        """An explicit meta_rows count skips exactly that many rows."""
        df = h.parse_psm3_csv(nsrdb_csv, meta_rows=2)
        assert len(df) == 3

    def test_time_assembled_from_date_parts(self, nsrdb_csv: str):
        """The time column is built from Year/Month/Day/Hour/Minute."""
        df = h.parse_psm3_csv(nsrdb_csv)
        assert str(df["time"].iloc[0]) == "2020-01-01 00:30:00"

    def test_leads_with_time_and_drops_raw_date_parts(self, nsrdb_csv: str):
        """time is the first column and the redundant date-part columns are gone."""
        df = h.parse_psm3_csv(nsrdb_csv)
        assert list(df.columns)[0] == "time"
        assert not ({"Year", "Month", "Day", "Hour", "Minute"} & set(df.columns))

    def test_missing_data_header_raises(self):
        """A body with no data-table header raises ValueError."""
        with pytest.raises(ValueError, match="no 'Year,Month,Day"):
            h.parse_psm3_csv("Source,foo\nNSRDB,1\n")


class TestEmptyCanonical:
    """Tests for `empty_canonical`."""

    def test_zero_rows_with_columns(self):
        """The fallback frame has the given columns and no rows."""
        df = h.empty_canonical(["time", "GHI", "lat"])
        assert list(df.columns) == ["time", "GHI", "lat"]
        assert len(df) == 0
