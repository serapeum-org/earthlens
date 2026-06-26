"""Shared fakes for the solar_wind_atlas tests (no real GDAL / network)."""

from __future__ import annotations

import io
import sys
import types
import zipfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from earthlens.solar_wind_atlas import _helpers


def zip_bytes(member: str = "World_GHI.tif") -> bytes:
    """Return the bytes of a one-member ZIP holding a stub GeoTIFF."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, b"II*\x00stub")
    return buffer.getvalue()


class FakeWindow:
    """Stand-in for a created pyramids `Dataset`, recording the written path."""

    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Record the write target and drop a stub byte so the file exists."""
        self._recorder["written"] = path
        Path(path).write_bytes(b"TIF")


class FakeDataset:
    """Stand-in for `pyramids.dataset.Dataset` capturing read_part / create."""

    recorder: dict = {}
    #: A plausible global geotransform (origin, 0.0025 deg pixel, negative dy).
    geotransform = (-180.0, 0.0025, 0.0, 80.0, 0.0, -0.0025)
    epsg = 4326

    @classmethod
    def read_file(cls, path: str) -> FakeDataset:
        """Record the opened path and return a fresh instance."""
        cls.recorder.setdefault("opened", []).append(path)
        return cls()

    def read_part(
        self,
        bbox: tuple[float, float, float, float],
        *,
        dst_width: int,
        dst_height: int,
        bbox_crs: int = 4326,
    ) -> np.ndarray:
        """Record the window request and return a zero array of that size."""
        type(self).recorder.setdefault("read_part", []).append(
            {
                "bbox": bbox,
                "dst_width": dst_width,
                "dst_height": dst_height,
                "bbox_crs": bbox_crs,
            }
        )
        return np.zeros((dst_height, dst_width), dtype="float32")

    @classmethod
    def create_from_array(cls, *, arr: np.ndarray, geo: tuple, epsg: int) -> FakeWindow:
        """Record the geo-wrap and return a writable fake window."""
        cls.recorder.setdefault("create", []).append(
            {"shape": arr.shape, "geo": geo, "epsg": epsg}
        )
        return FakeWindow(cls.recorder)


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> type[FakeDataset]:
    """Inject a fake `pyramids.dataset` module so no real GDAL is touched."""
    FakeDataset.recorder = {}
    module = types.ModuleType("pyramids.dataset")
    module.Dataset = FakeDataset
    monkeypatch.setitem(sys.modules, "pyramids.dataset", module)
    return FakeDataset


class FakeResponse:
    """Context-manager stand-in for a streaming requests response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        """No-op — the fake always succeeds."""

    def iter_content(self, chunk_size: int = 1) -> list[bytes]:
        """Yield the body in one chunk."""
        return [self._body]


class FakeGet:
    """Callable `requests.get` stand-in that counts calls and streams a ZIP."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, url: str, *, stream: bool = False, timeout: float = 0.0
    ) -> FakeResponse:
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
