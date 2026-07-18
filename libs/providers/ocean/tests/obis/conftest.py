"""Shared fixtures for the OBIS backend tests — a faked `pyobis` module."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest


class _FakeResponse:
    """Stand-in for a pyobis 1.x `OccResponse` query object."""

    def __init__(self, frame: pd.DataFrame):
        """Hold the DataFrame `.execute()` will return."""
        self._frame = frame
        self.data: dict | None = None

    def execute(self, **kwargs) -> pd.DataFrame:
        """Populate `.data` and return the records DataFrame (1.x contract)."""
        self.data = {"total": len(self._frame), "results": self._frame}
        return self._frame

    def to_pandas(self) -> pd.DataFrame:
        """Return the same DataFrame `.execute()` yields."""
        return self._frame


class _FakeOccurrences:
    """Stand-in for `pyobis.occurrences` recording `search` calls."""

    def __init__(self):
        """Start with an empty result frame and no recorded calls."""
        self.frame = pd.DataFrame()
        self.calls: list[dict] = []

    def set_frame(self, frame: pd.DataFrame) -> None:
        """Pin the DataFrame the next `search(...).execute()` returns."""
        self.frame = frame

    def search(self, **kwargs):
        """Record the call and return a lazy response over the frame."""
        self.calls.append(kwargs)
        return _FakeResponse(self.frame)


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Build an OBIS-style occurrence DataFrame from row dicts."""
    return pd.DataFrame(rows)


def _row(**fields):
    """Build one OBIS occurrence record dict with sensible defaults."""
    record = {
        "id": "occ-1",
        "scientificName": "Delphinus delphis",
        "decimalLatitude": 40.0,
        "decimalLongitude": -2.0,
        "eventDate": "2018-06-01",
        "depth": 5.0,
        "basisOfRecord": "HumanObservation",
        "dataset_id": "ds-1",
        "license": "CC-BY-4.0",
    }
    record.update(fields)
    return record


@pytest.fixture
def fake_obis(monkeypatch):
    """Install a fake `pyobis` module exposing `occurrences`."""
    occurrences = _FakeOccurrences()
    module = ModuleType("pyobis")
    module.occurrences = occurrences
    monkeypatch.setitem(sys.modules, "pyobis", module)
    return SimpleNamespace(occurrences=occurrences, frame=_frame, row=_row)
