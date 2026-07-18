"""Shared fixtures for the soilgrids tests — a faked `Dataset.from_wcs`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest


class FakeDataset:
    """A stand-in for a pyramids `Dataset` that records `from_wcs` calls.

    The class-level `recorder` collects one kwargs dict per `from_wcs` call so a
    test can assert the coverage ids, bbox, endpoint, and CRS shim the backend
    passed, without any network or GDAL; `masks` and `written` record the
    polygon-mask crop and the `to_file` writes on the mask path.
    """

    recorder: list[dict[str, Any]] = []
    masks: list[Any] = []
    written: list[str] = []
    fail_coverages: set[str] = set()
    fail_to_file: set[str] = set()
    no_data_value = (-32768.0,)

    def __init__(self, coverage: str) -> None:
        self.coverage = coverage
        self.closed = False

    @classmethod
    def from_wcs(cls, endpoint: str, **kwargs: Any) -> FakeDataset:
        """Record the call and return an in-memory fake (never writes a file).

        A coverage listed in `fail_coverages` raises before returning, modelling
        pyramids' pre-write (fetch/MEM) failure, to exercise the backend's
        per-coverage failure isolation.
        """
        cls.recorder.append({"endpoint": endpoint, **kwargs})
        coverage = kwargs["coverage"]
        if coverage in cls.fail_coverages:
            raise RuntimeError(f"faked WCS failure for {coverage}")
        output = kwargs.get("output")
        if output is not None:
            Path(output).write_bytes(b"MM\x00*stub-geotiff")
        return cls(coverage)

    def crop(self, mask: Any = None, touch: bool = True) -> FakeDataset:
        """Record the polygon mask and return a distinct masked fake dataset."""
        FakeDataset.masks.append(mask)
        return FakeDataset(self.coverage)

    def to_file(self, path: str) -> None:
        """Record and write a stub GeoTIFF (the mask-path write).

        A coverage in `fail_to_file` writes a partial file then raises, to
        exercise a mask-path write that fails mid-way.
        """
        FakeDataset.written.append(path)
        Path(path).write_bytes(b"MM\x00*stub-geotiff")
        if self.coverage in FakeDataset.fail_to_file:
            raise RuntimeError(f"faked to_file failure for {self.coverage}")

    def close(self) -> None:
        """Mark the fake dataset closed (the backend releases the handle)."""
        self.closed = True


@pytest.fixture
def fake_from_wcs(monkeypatch: pytest.MonkeyPatch) -> type[FakeDataset]:
    """Patch `pyramids.dataset.Dataset.from_wcs` with the recording fake."""
    from pyramids.dataset import Dataset

    FakeDataset.recorder = []
    FakeDataset.masks = []
    FakeDataset.written = []
    FakeDataset.fail_coverages = set()
    FakeDataset.fail_to_file = set()
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
