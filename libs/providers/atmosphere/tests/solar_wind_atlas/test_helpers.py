"""Unit tests for the solar_wind_atlas helpers (faked pyramids + requests)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from earthlens.base import SpatialExtent
from earthlens.solar_wind_atlas import _helpers

from .conftest import FailingGet, FakeDataset, FakeGet, zip_bytes

pytestmark = pytest.mark.solar_wind_atlas


def test_vsicurl_prefixes_the_url() -> None:
    """vsicurl wraps an http URL as a /vsicurl/ path verbatim."""
    assert _helpers.vsicurl("https://x/GHI.tif") == "/vsicurl/https://x/GHI.tif"


def test_bbox_from_extent_returns_wsen() -> None:
    """bbox_from_extent yields [west, south, east, north] from a SpatialExtent."""
    space = SpatialExtent.from_pairs(lat_lim=[55.0, 55.5], lon_lim=[12.0, 12.5])
    assert _helpers.bbox_from_extent(space) == [12.0, 55.0, 12.5, 55.5]


def test_window_crop_opens_vsicurl_and_crops(
    fake_pyramids: type[FakeDataset], tmp_path: Path
) -> None:
    """window_crop opens the /vsicurl path and windowed-crops it to the bbox."""
    out = tmp_path / "wind_100m.tif"
    result = _helpers.window_crop(
        "https://ndownloader.figshare.com/files/17247017",
        [12.0, 55.0, 12.5, 55.5],
        out,
    )
    assert result == out
    assert fake_pyramids.recorder["opened"][0].startswith("/vsicurl/https://")
    crop = fake_pyramids.recorder["crop"][0]
    assert crop["bbox"] == [12.0, 55.0, 12.5, 55.5]
    assert crop["epsg"] == 4326
    assert fake_pyramids.recorder["written"] == str(out)


def test_window_crop_degenerate_bbox_still_crops(
    fake_pyramids: type[FakeDataset], tmp_path: Path
) -> None:
    """A zero-extent point bbox is forwarded to crop (pyramids clamps the window)."""
    _helpers.window_crop(
        "https://x/w.tif", [12.0, 55.0, 12.0, 55.0], tmp_path / "p.tif"
    )
    assert fake_pyramids.recorder["crop"][0]["bbox"] == [12.0, 55.0, 12.0, 55.0]
    assert fake_pyramids.recorder["written"] == str(tmp_path / "p.tif")


def test_download_zip_cleans_partial_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed download removes its .part file instead of leaving a stub."""
    monkeypatch.setattr(_helpers.requests, "get", FailingGet())
    url = "https://api.globalsolaratlas.info/download/World/World_GHI.zip"
    with pytest.raises(OSError, match="connection dropped"):
        _helpers.download_zip(url, tmp_path)
    assert not (tmp_path / "World_GHI.zip.part").exists()
    assert not (tmp_path / "World_GHI.zip").exists()


def test_zip_cache_path_uses_url_filename(tmp_path: Path) -> None:
    """zip_cache_path maps a URL to cache_dir/<filename>."""
    url = "https://api.globalsolaratlas.info/download/World/World_GHI.zip"
    assert _helpers.zip_cache_path(url, tmp_path) == tmp_path / "World_GHI.zip"


def test_inner_tif_finds_geotiff_member(tmp_path: Path) -> None:
    """inner_tif returns the .tif member name of a ZIP archive."""
    zip_path = tmp_path / "gsa.zip"
    zip_path.write_bytes(zip_bytes("World_GHI.tif"))
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
    fake_get: FakeGet, tmp_path: Path
) -> None:
    """download_zip GETs once and reuses the cached archive on a second call."""
    url = "https://api.globalsolaratlas.info/download/World/World_GHI.zip"
    first = _helpers.download_zip(url, tmp_path)
    second = _helpers.download_zip(url, tmp_path)
    assert first == second == tmp_path / "World_GHI.zip"
    assert fake_get.calls == 1


def test_download_cache_crop_downloads_then_windows(
    fake_pyramids: type[FakeDataset], fake_get: FakeGet, tmp_path: Path
) -> None:
    """download_cache_crop fetches the ZIP once, then reads /vsizip windowed."""
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
    opened = fake_pyramids.recorder["opened"][0]
    assert opened.startswith("/vsizip/") and opened.endswith("World_GHI.tif")
