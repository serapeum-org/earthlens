"""Shared offline fixtures for the Sensor.Community backend tests.

Drives the backend without network by injecting a fake
`SensorCommunityClient` (`client=`) whose `live_snapshot()` returns canned
records and `archive_csv(...)` returns fixture CSV text (or `None` for a
missing day).
"""

from __future__ import annotations

from typing import Any

import pytest

SDS_CSV = (
    "sensor_id;sensor_type;location;lat;lon;timestamp;P1;durP1;ratioP1;P2;durP2;ratioP2\n"
    "140;SDS011;65;48.778;9.160;2026-06-30T00:02:21;8.5;;;4.2;;\n"
    "140;SDS011;65;48.778;9.160;2026-06-30T00:05:07;9.1;;;4.6;;\n"
)

DHT_CSV = (
    "sensor_id;sensor_type;location;lat;lon;timestamp;temperature;humidity\n"
    "108;DHT22;49;48.530;9.200;2026-06-30T00:03:00;24.9;55.0\n"
)


def _record(
    *,
    sensor_id: int = 140,
    sensor_type: str = "SDS011",
    lat: str = "48.778",
    lon: str = "9.16",
) -> dict[str, Any]:
    """Build one live JSON API record."""
    return {
        "location": {"latitude": lat, "longitude": lon},
        "sensor": {"id": sensor_id, "sensor_type": {"name": sensor_type}},
    }


class _FakeClient:
    """Recording fake over the live + archive hosts."""

    def __init__(
        self,
        snapshot: list[dict[str, Any]] | None = None,
        archive: dict[tuple[str, str, str], str] | None = None,
    ) -> None:
        self.snapshot = snapshot if snapshot is not None else [_record(), _record()]
        self.archive = (
            archive
            if archive is not None
            else {("2026-06-30", "sds011", "140"): SDS_CSV}
        )
        self.archive_calls: list[tuple[str, str, str]] = []

    def live_snapshot(self) -> list[dict[str, Any]]:
        return self.snapshot

    def archive_csv(self, date: str, sensor_type: str, sensor_id: str) -> str | None:
        self.archive_calls.append((date, sensor_type, sensor_id))
        return self.archive.get((date, sensor_type, sensor_id))


@pytest.fixture
def fake_client() -> _FakeClient:
    """A fake Sensor.Community client with one SDS011 sensor + one day."""
    return _FakeClient()


@pytest.fixture
def make_record():
    """Factory for a live JSON API record (see `_record`)."""
    return _record
