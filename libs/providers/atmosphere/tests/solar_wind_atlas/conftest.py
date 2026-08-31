"""Shared fakes for the solar_wind_atlas tests (no real GDAL / network)."""

from __future__ import annotations

import io
import sys
import types
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from earthlens.solar_wind_atlas import _helpers


def zip_bytes(member: str = "World_GHI.tif") -> bytes:
    """Return the bytes of a one-member ZIP holding a stub GeoTIFF."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, b"II*\x00stub")
    return buffer.getvalue()


class FakeWindow:
    """Stand-in for a cropped pyramids `Dataset`, recording the written path."""

    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Record the write target and drop a stub byte so the file exists."""
        self._recorder["written"] = path
        Path(path).write_bytes(b"TIF")


class FakeDataset:
    """Stand-in for `pyramids.dataset.Dataset` capturing the windowed crop.

    The real windowed read + geo/no-data carry-through now lives in
    `Dataset.crop(bbox=)` (pyramids), so the fake only records the crop request
    and the write — the window math and no-data preservation are pyramids' own
    tested concern, not the helper's.
    """

    recorder: dict = {}
    #: A plausible global geotransform (origin, 0.0025 deg pixel, negative dy)
    #: with matching dimensions, so `bbox_overlaps` sees a global extent.
    geotransform = (-180.0, 0.0025, 0.0, 80.0, 0.0, -0.0025)
    columns = 144000
    rows = 64000
    epsg = 4326
    #: Per-band no-data tuple, mirroring pyramids' `Dataset.no_data_value`.
    no_data_value = (-32768.0,)

    @classmethod
    def read_file(cls, path: str) -> FakeDataset:
        """Record the opened path and return a fresh instance."""
        cls.recorder.setdefault("opened", []).append(path)
        return cls()

    def crop(
        self,
        mask: object = None,
        touch: bool = True,
        *,
        bbox: list[float] | tuple[float, float, float, float] | None = None,
        epsg: object = None,
    ) -> FakeWindow:
        """Record the windowed bbox crop and return a writable fake window."""
        type(self).recorder.setdefault("crop", []).append({"bbox": bbox, "epsg": epsg})
        return FakeWindow(type(self).recorder)


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> type[FakeDataset]:
    """Inject a fake `pyramids.dataset` module so no real GDAL is touched."""
    FakeDataset.recorder = {}
    module = types.ModuleType("pyramids.dataset")
    module.Dataset = FakeDataset
    monkeypatch.setitem(sys.modules, "pyramids.dataset", module)
    return FakeDataset


class FakeResponse:
    """Stand-in for a streaming requests response (HttpClient shape)."""

    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        """No-op — the fake always succeeds."""

    def iter_content(self, chunk_size: int = 1) -> list[bytes]:
        """Yield the body, plus an empty chunk to exercise the skip guard."""
        return [self._body, b""]

    def close(self) -> None:
        """No-op — the fake holds no socket."""


class FailingResponse:
    """A streaming response whose body iteration raises mid-download."""

    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        """No-op — the failure happens during streaming, not at the status."""

    def iter_content(self, chunk_size: int = 1) -> list[bytes]:
        """Raise to simulate a dropped connection mid-stream."""
        raise OSError("connection dropped mid-stream")

    def close(self) -> None:
        """No-op — the fake holds no socket."""


class FailingGet:
    """Callable `requests.get` stand-in that fails partway through the body."""

    def __call__(self, url: str, **kwargs: object) -> FailingResponse:
        return FailingResponse()


class FakeGet:
    """Callable `requests.get` stand-in that counts calls and streams a ZIP."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls += 1
        return FakeResponse(zip_bytes("World_GHI.tif"))


@pytest.fixture
def fake_get(monkeypatch: pytest.MonkeyPatch) -> FakeGet:
    """Patch `_helpers.requests.get` with a call-counting ZIP streamer."""
    getter = FakeGet()
    monkeypatch.setattr(_helpers.requests, "get", getter)
    return getter


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
