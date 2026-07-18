"""Shared offline fixtures for the EEA (`eea_aq`) backend tests.

Drives the backend without network by injecting a fake `airbase` client
(`client=`) whose `request(...).download(dir)` copies a fixture Parquet —
shaped exactly like a real EEA download (string `Value`, numeric
`Pollutant` code, `CC/SPO-...` sampling points) — into the temp dir.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


def _fixture_frame() -> pd.DataFrame:
    """Build a raw EEA Parquet frame: MT pm25 (2 years) + DE pm10 + MT o3."""
    return pd.DataFrame(
        {
            "Samplingpoint": [
                "MT/SPO-MT001_06001",
                "MT/SPO-MT001_06001",
                "DE/SPO-DE002_00005",
                "MT/SPO-MT001_00007",
            ],
            "Pollutant": [6001, 6001, 5, 7],
            "Start": pd.to_datetime(
                [
                    "2023-06-01T00:00",
                    "2024-06-01T00:00",
                    "2023-06-01T00:00",
                    "2023-06-01T00:00",
                ]
            ),
            "End": pd.to_datetime(["2023-06-01T01:00"] * 4),
            "Value": ["5.63", "7.10", "12.5", "40.0"],
            "Unit": ["ug.m-3"] * 4,
            "AggType": ["hour"] * 4,
            "Validity": [1, 1, 1, 1],
            "Verification": [3, 3, 2, 3],
            "ResultTime": pd.to_datetime(["2023-01-01"] * 4),
            "DataCapture": [None] * 4,
            "FkObservationLog": ["x"] * 4,
        }
    )


class _FakeRequest:
    """Stand-in for `AirbaseRequest`: copies a fixture Parquet on download."""

    def __init__(self, parquet: Path) -> None:
        self._parquet = parquet

    def download(
        self, dir: str, skip_existing: bool = True, raise_for_status: bool = True
    ) -> None:
        shutil.copy(self._parquet, Path(dir) / "data.parquet")


class _FakeAirbaseClient:
    """Recording stand-in for `airbase.AirbaseClient`."""

    def __init__(self, parquet: Path) -> None:
        self._parquet = parquet
        self.calls: list[tuple[str, tuple[str, ...], Any]] = []

    def request(self, source: str, *countries: str, poll: Any = None, verbose: bool = True):
        self.calls.append((source, countries, poll))
        return _FakeRequest(self._parquet)


@pytest.fixture
def fixture_parquet(tmp_path_factory) -> Path:
    """Write the raw EEA fixture Parquet once and return its path."""
    path = tmp_path_factory.mktemp("eea_fx") / "fixture.parquet"
    _fixture_frame().to_parquet(path)
    return path


@pytest.fixture
def fake_client(fixture_parquet: Path) -> _FakeAirbaseClient:
    """A fake airbase client that serves the fixture Parquet."""
    return _FakeAirbaseClient(fixture_parquet)
