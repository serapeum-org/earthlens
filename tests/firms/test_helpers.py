"""Tests for the pure FIRMS request/parsing helpers (no pyramids)."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.firms._helpers import (
    chunk_windows,
    classify_body,
    firms_get,
)


class _Resp:
    """Minimal fake response exposing status_code and text."""

    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code


def test_single_day_window_is_day_range_one():
    """A 1-day window is a single day_range=1 request."""
    assert chunk_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 1)) == [
        (dt.date(2024, 1, 1), 1)
    ]


def test_25_day_window_chunks_10_10_5():
    """A 25-day window splits into 10/10/5 with correct starts."""
    chunks = chunk_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 25))
    assert chunks == [
        (dt.date(2024, 1, 1), 10),
        (dt.date(2024, 1, 11), 10),
        (dt.date(2024, 1, 21), 5),
    ]


def test_exact_multiple_of_ten():
    """A 20-day window is two full 10-day chunks, no remainder."""
    assert [dr for _, dr in chunk_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 20))] == [
        10,
        10,
    ]


def test_end_before_start_raises():
    """A reversed window is rejected."""
    with pytest.raises(ValueError, match="before start"):
        chunk_windows(dt.date(2024, 1, 10), dt.date(2024, 1, 1))


def test_classify_csv_header():
    """A body starting with the latitude header is csv."""
    assert classify_body("latitude,longitude,frp\n1,2,3") == "csv"
    assert classify_body("  LATITUDE,longitude\n") == "csv"


def test_classify_auth_quota_error():
    """Error bodies route to auth / quota / error by wording."""
    assert classify_body("Invalid MAP_KEY.") == "auth"
    assert classify_body("You have exceeded your transaction limit") == "quota"
    assert classify_body("Invalid coordinates given") == "error"


def test_firms_get_returns_csv_without_retry():
    """A CSV response is returned on the first call, no sleeps."""
    waits: list[float] = []
    calls = {"n": 0}

    def _get(url, timeout):
        calls["n"] += 1
        return _Resp("latitude,longitude\n1,2")

    resp = firms_get("u", timeout=1, get=_get, sleep=waits.append)
    assert resp.text.startswith("latitude")
    assert calls["n"] == 1
    assert waits == []


def test_firms_get_retries_on_429_then_succeeds():
    """A 429 once triggers one back-off then returns the CSV."""
    waits: list[float] = []
    responses = [_Resp("rate limit", 429), _Resp("latitude,longitude\n1,2")]

    def _get(url, timeout):
        return responses.pop(0)

    resp = firms_get("u", timeout=1, get=_get, sleep=waits.append, backoff_factor=2.0)
    assert resp.text.startswith("latitude")
    assert waits == [2.0]


def test_firms_get_retries_on_quota_body_200():
    """A HTTP-200 quota body is treated as rate-limited and retried."""
    waits: list[float] = []
    responses = [
        _Resp("You have exceeded your transaction limit", 200),
        _Resp("latitude,longitude\n1,2"),
    ]

    def _get(url, timeout):
        return responses.pop(0)

    resp = firms_get("u", timeout=1, get=_get, sleep=waits.append)
    assert resp.text.startswith("latitude")
    assert len(waits) == 1


def test_firms_get_gives_up_after_max_retries():
    """A persistent quota body returns the last response after the cap."""
    waits: list[float] = []

    def _get(url, timeout):
        return _Resp("transaction limit reached", 200)

    resp = firms_get("u", timeout=1, get=_get, sleep=waits.append, max_retries=3)
    assert classify_body(resp.text) == "quota"
    assert len(waits) == 3
