"""Tests for the Sensor.Community client, parsing, and licence helpers."""

from __future__ import annotations

import pytest
import requests

from earthlens.sensor_community._helpers import (
    LicenseWarning,
    SensorCommunityClient,
    _parse_retry_after,
    empty_frame,
    frame_from_csv,
    sensors_in_bbox,
)
from tests.sensor_community.conftest import SDS_CSV, _record


class _Resp:
    """Canned requests-like response."""

    def __init__(self, *, status_code=200, text="", json_body=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_body
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    """Session returning a scripted sequence of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self._responses.pop(0)


@pytest.mark.sensor_community
def test_license_warning_is_user_warning():
    """`LicenseWarning` is a `UserWarning` subclass."""
    assert issubclass(LicenseWarning, UserWarning)


@pytest.mark.sensor_community
@pytest.mark.parametrize("value, expected", [("5", 5.0), (None, None), ("x", None)])
def test_parse_retry_after(value, expected):
    """Retry-After parses numeric values, else `None`."""
    assert _parse_retry_after(value) == expected


@pytest.mark.sensor_community
class TestClient:
    """The two-host client with back-off."""

    def test_live_snapshot(self):
        """`live_snapshot` returns the JSON array."""
        client = SensorCommunityClient(session=_Session([_Resp(json_body=[{"a": 1}])]))
        assert client.live_snapshot() == [{"a": 1}]

    def test_live_snapshot_non_list(self):
        """A non-list live body yields an empty list."""
        client = SensorCommunityClient(session=_Session([_Resp(json_body={"e": 1})]))
        assert client.live_snapshot() == []

    def test_archive_csv_success(self):
        """`archive_csv` returns the CSV text on 200."""
        client = SensorCommunityClient(session=_Session([_Resp(text=SDS_CSV)]))
        assert client.archive_csv("2026-06-30", "sds011", "140") == SDS_CSV

    def test_archive_csv_404_none(self):
        """A 404 archive file yields `None` (missing day)."""
        client = SensorCommunityClient(session=_Session([_Resp(status_code=404)]))
        assert client.archive_csv("2026-06-30", "sds011", "999") is None

    def test_archive_csv_error_raises(self):
        """A non-404 archive error propagates."""
        client = SensorCommunityClient(session=_Session([_Resp(status_code=500)]))
        with pytest.raises(requests.HTTPError):
            client.archive_csv("2026-06-30", "sds011", "1")

    def test_backoff_on_429(self):
        """A 429 is retried after a back-off sleep, then succeeds."""
        session = _Session(
            [_Resp(status_code=429, headers={"Retry-After": "0"}), _Resp(text="ok")]
        )
        waits: list[float] = []
        client = SensorCommunityClient(session=session, sleep=waits.append)
        assert client.archive_csv("d", "sds011", "1") == "ok"
        assert len(waits) == 1


@pytest.mark.sensor_community
class TestSensorsInBbox:
    """Discovery filtering of the live snapshot."""

    def test_dedup_and_filter(self):
        """Duplicate + out-of-bbox + wrong-type records are filtered."""
        snap = [
            _record(),
            _record(),  # dup
            _record(sensor_id=108, sensor_type="DHT22"),  # wrong type
            _record(sensor_id=999, lat="10.0", lon="10.0"),  # out of bbox
        ]
        out = sensors_in_bbox(snap, (48.5, 48.9), (9.0, 9.3), {"sds011"})
        assert [s["sensor_id"] for s in out] == ["140"]

    def test_bad_coords_skipped(self):
        """A record with non-numeric coordinates is skipped."""
        snap = [{"location": {"latitude": "x", "longitude": "y"},
                 "sensor": {"id": 1, "sensor_type": {"name": "SDS011"}}}]
        assert sensors_in_bbox(snap, (0, 90), (0, 90), {"sds011"}) == []


@pytest.mark.sensor_community
class TestFrameFromCsv:
    """Parsing a per-sensor CSV into the long schema."""

    def test_extracts_requested_columns(self):
        """Both P1 (pm10) and P2 (pm25) rows are emitted."""
        out = frame_from_csv(
            SDS_CSV, {"P2": "pm25", "P1": "pm10"}, {"pm25": "µg/m³", "pm10": "µg/m³"}
        )
        assert set(out["parameter"]) == {"pm25", "pm10"}
        assert len(out) == 4

    def test_missing_column_yields_empty(self):
        """A CSV lacking the requested column yields the empty frame."""
        out = frame_from_csv(SDS_CSV, {"temperature": "temperature"}, {"temperature": "°C"})
        assert out.empty

    def test_non_numeric_values_dropped(self):
        """Rows whose value is non-numeric are dropped."""
        csv = (
            "sensor_id;sensor_type;location;lat;lon;timestamp;P2;durP2;ratioP2\n"
            "1;SDS011;5;1.0;2.0;2026-06-30T00:00:00;bad;;\n"
        )
        out = frame_from_csv(csv, {"P2": "pm25"}, {"pm25": "µg/m³"})
        assert out.empty


@pytest.mark.sensor_community
def test_empty_frame_schema():
    """`empty_frame` has the full schema and zero rows."""
    frame = empty_frame()
    assert frame.empty and "sensor_type" in frame.columns
