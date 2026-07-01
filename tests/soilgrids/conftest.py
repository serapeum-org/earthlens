"""Shared fixtures for the soilgrids tests — a faked `Dataset.from_wcs`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest


class FakeDataset:
    """A stand-in for a pyramids `Dataset` that records `from_wcs` calls.

    The class-level `recorder` collects one kwargs dict per `from_wcs` call so a
    test can assert the coverage ids, bbox, endpoint, and CRS shim the backend
    passed, without any network or GDAL.
    """

    recorder: list[dict[str, Any]] = []

    def __init__(self, coverage: str) -> None:
        self.coverage = coverage
        self.closed = False

    @classmethod
    def from_wcs(cls, endpoint: str, **kwargs: Any) -> "FakeDataset":
        """Record the call, write a stub GeoTIFF to `output`, return a fake."""
        cls.recorder.append({"endpoint": endpoint, **kwargs})
        output = kwargs.get("output")
        if output is not None:
            Path(output).write_bytes(b"MM\x00*stub-geotiff")
        return cls(kwargs["coverage"])

    def close(self) -> None:
        """Mark the fake dataset closed (the backend releases the handle)."""
        self.closed = True


@pytest.fixture
def fake_from_wcs(monkeypatch: pytest.MonkeyPatch) -> type[FakeDataset]:
    """Patch `pyramids.dataset.Dataset.from_wcs` with the recording fake."""
    from pyramids.dataset import Dataset

    FakeDataset.recorder = []
    monkeypatch.setattr(Dataset, "from_wcs", FakeDataset.from_wcs)
    return FakeDataset


@pytest.fixture
def info_log() -> Iterator[list[str]]:
    """Capture INFO-and-above loguru messages into a list for the test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
