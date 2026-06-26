"""Unit tests for the solar_wind_atlas helpers (faked pyramids + requests)."""

from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest

from earthlens.base import SpatialExtent
from earthlens.solar_wind_atlas import _helpers


class _FakeWindow:
    """Stand-in for a created pyramids `Dataset`, recording the written path."""

    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def to_file(self, path: str) -> None:
        """Record the write target and drop a stub byte so the file exists."""
        self._recorder["written"] = path
        Path(path).write_bytes(b"TIF")


class _FakeDataset:
    """Stand-in for `pyramids.dataset.Dataset` capturing read_part / create."""

    recorder: dict = {}
    #: A plausible global geotransform (origin, 0.0025 deg pixel, negative dy).
    geotransform = (-180.0, 0.0025, 0.0, 80.0, 0.0, -0.0025)
    epsg = 4326

    @classmethod
    def read_file(cls, path: str) -> _FakeDataset:
        """Record the opened path and return a fresh instance."""
        cls.recorder["opened"] = path
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
        type(self).recorder["read_part"] = {
            "bbox": bbox,
            "dst_width": dst_width,
            "dst_height": dst_height,
            "bbox_crs": bbox_crs,
        }
        return np.zeros((dst_height, dst_width), dtype="float32")

    @classmethod
    def create_from_array(cls, *, arr: np.ndarray, geo: tuple, epsg: int) -> _FakeWindow:
        """Record the geo-wrap and return a writable fake window."""
        cls.recorder["create"] = {"shape": arr.shape, "geo": geo, "epsg": epsg}
        return _FakeWindow(cls.recorder)


@pytest.fixture
def fake_pyramids(monkeypatch: pytest.MonkeyPatch) -> type[_FakeDataset]:
    """Inject a fake `pyramids.dataset` module so no real GDAL is touched."""
    _FakeDataset.recorder = {}
    module = types.ModuleType("pyramids.dataset")
    module.Dataset = _FakeDataset
    monkeypatch.setitem(sys.modules, "pyramids.dataset", module)
    return _FakeDataset


def _zip_bytes(member: str = "World_GHI.tif") -> bytes:
    """Return the bytes of a one-member ZIP holding a stub GeoTIFF."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, b"II*\x00stub")
    return buffer.getvalue()


def test_vsicurl_prefixes_the_url() -> None:
    """vsicurl wraps an http URL as a /vsicurl/ path verbatim."""
    assert _helpers.vsicurl("https://x/GHI.tif") == "/vsicurl/https://x/GHI.tif"


def test_bbox_from_extent_returns_wsen() -> None:
    """bbox_from_extent yields [west, south, east, north] from a SpatialExtent."""
    space = SpatialExtent.from_pairs(lat_lim=[55.0, 55.5], lon_lim=[12.0, 12.5])
    assert _helpers.bbox_from_extent(space) == [12.0, 55.0, 12.5, 55.5]


def test_window_crop_opens_vsicurl_and_reads_part(
    fake_pyramids: type[_FakeDataset], tmp_path: Path
) -> None:
    """window_crop opens the /vsicurl path and extracts via read_part, not crop."""
    out = tmp_path / "wind_100m.tif"
    result = _helpers.window_crop(
        "https://ndownloader.figshare.com/files/17247017",
        [12.0, 55.0, 12.5, 55.5],
        out,
    )
    assert result == out
    assert fake_pyramids.recorder["opened"].startswith("/vsicurl/https://")
    assert fake_pyramids.recorder["read_part"]["bbox"] == (12.0, 55.0, 12.5, 55.5)
    assert fake_pyramids.recorder["written"] == str(out)


def test_window_crop_window_size_matches_native_grid(
    fake_pyramids: type[_FakeDataset], tmp_path: Path
) -> None:
    """A 0.5 deg bbox at the 0.0025 deg native grid reads a ~200x200 window."""
    _helpers.window_crop("https://x/w.tif", [12.0, 55.0, 12.5, 55.5], tmp_path / "w.tif")
    part = fake_pyramids.recorder["read_part"]
    assert part["dst_width"] == 200
    assert part["dst_height"] == 200


def test_zip_cache_path_uses_url_filename(tmp_path: Path) -> None:
    """zip_cache_path maps a URL to cache_dir/<filename>."""
    url = "https://api.globalsolaratlas.info/download/World/World_GHI.zip"
    assert _helpers.zip_cache_path(url, tmp_path) == tmp_path / "World_GHI.zip"


def test_inner_tif_finds_geotiff_member(tmp_path: Path) -> None:
    """inner_tif returns the .tif member name of a ZIP archive."""
    zip_path = tmp_path / "gsa.zip"
    zip_path.write_bytes(_zip_bytes("World_GHI.tif"))
    assert _helpers.inner_tif(zip_path) == "World_GHI.tif"


def test_inner_tif_raises_without_geotiff(tmp_path: Path) -> None:
    """inner_tif raises ValueError when no GeoTIFF member is present."""
    zip_path = tmp_path / "empty.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", b"no raster here")
    zip_path.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match="no GeoTIFF"):
        _helpers.inner_tif(zip_path)


def test_download_zip_streams_once_then_caches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """download_zip GETs once and reuses the cached archive on a second call."""
    fake_get = _FakeGet()
    monkeypatch.setattr(_helpers.requests, "get", fake_get)
    url = "https://api.globalsolaratlas.info/download/World/World_GHI.zip"
    first = _helpers.download_zip(url, tmp_path)
    second = _helpers.download_zip(url, tmp_path)
    assert first == second == tmp_path / "World_GHI.zip"
    assert fake_get.calls == 1


def test_download_cache_crop_downloads_then_windows(
    fake_pyramids: type[_FakeDataset], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """download_cache_crop fetches the ZIP once, then reads /vsizip windowed."""
    fake_get = _FakeGet()
    monkeypatch.setattr(_helpers.requests, "get", fake_get)
    cache = tmp_path / "cache"
    out = tmp_path / "ghi.tif"
    result = _helpers.download_cache_crop(
        "https://api.globalsolaratlas.info/download/World/World_GHI.zip",
        [12.0, 55.0, 12.5, 55.5],
        out,
        cache,
    )
    assert result == out
    assert fake_get.calls == 1
    opened = fake_pyramids.recorder["opened"]
    assert opened.startswith("/vsizip/") and opened.endswith("World_GHI.tif")


class _FakeResponse:
    """Context-manager stand-in for a streaming requests response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        """No-op — the fake always succeeds."""

    def iter_content(self, chunk_size: int = 1) -> list[bytes]:
        """Yield the body in one chunk."""
        return [self._body]


class _FakeGet:
    """Callable `requests.get` stand-in that counts calls and streams a ZIP."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, url: str, *, stream: bool = False, timeout: float = 0.0
    ) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(_zip_bytes("World_GHI.tif"))
