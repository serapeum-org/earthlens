"""Unit tests for the solar_wind_atlas helpers (faked pyramids + requests)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
from pyramids.dataset import Dataset, GeoReference

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


def test_window_crop_degenerate_bbox_widened_before_crop(
    fake_pyramids: type[FakeDataset], tmp_path: Path
) -> None:
    """A zero-extent point bbox is widened to one source pixel before crop.

    crop(bbox=) requires a strictly positive box, so a point AOI must be
    widened first; assert the collapsed edges are pushed out.
    """
    _helpers.window_crop(
        "https://x/w.tif", [12.0, 55.0, 12.0, 55.0], tmp_path / "p.tif"
    )
    bbox = fake_pyramids.recorder["crop"][0]["bbox"]
    assert bbox[2] > bbox[0], f"west edge not widened: {bbox}"
    assert bbox[3] > bbox[1], f"south edge not widened: {bbox}"
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


def _write_geotiff(
    path: Path, *, no_data_value: float | None, fill: float | None = None
) -> None:
    """Write a 20x20 0.1-deg EPSG:4326 GeoTIFF for real windowed-crop tests.

    `fill` writes a constant raster (use the no-data value for an all-no-data
    source); otherwise a ramp of distinct values.
    """
    if fill is None:
        arr = np.arange(400, dtype="float32").reshape(20, 20)
    else:
        arr = np.full((20, 20), fill, dtype="float32")
    Dataset.from_array(
        arr,
        no_data_value=no_data_value,
        geo_ref=GeoReference(top_left_corner=(0.0, 0.0), cell_size=0.1, epsg=4326),
    ).to_file(str(path))


class TestReadPartToGeotiffReal:
    """`read_part_to_geotiff` against a real (local) pyramids raster."""

    def test_windowed_crop_carries_source_no_data(self, tmp_path: Path) -> None:
        """A normal window writes a subset carrying the source's own no-data."""
        src = tmp_path / "src.tif"
        _write_geotiff(src, no_data_value=-32768.0)
        out = tmp_path / "out.tif"
        _helpers.read_part_to_geotiff(str(src), [0.2, -0.6, 0.6, -0.2], out)
        result = Dataset.read_file(str(out))
        assert result.rows > 0, "a non-empty window is written"
        assert result.columns > 0, "a non-empty window is written"
        assert result.rows < 20, "only the AOI window read"
        assert result.columns < 20, "only the AOI window read"
        assert result.no_data_value[0] == -32768.0, "source no-data carried through"

    def test_degenerate_point_yields_small_crop_not_raise(self, tmp_path: Path) -> None:
        """A point AOI produces a small crop instead of raising (review H1)."""
        src = tmp_path / "src.tif"
        _write_geotiff(src, no_data_value=-9999.0)
        out = tmp_path / "point.tif"
        _helpers.read_part_to_geotiff(str(src), [0.35, -0.35, 0.35, -0.35], out)
        result = Dataset.read_file(str(out))
        assert 1 <= result.rows <= 2, f"rows should clamp small, got {result.rows}"
        assert 1 <= result.columns <= 2, (
            f"columns should clamp small, got {result.columns}"
        )

    def test_all_nodata_aoi_writes_crop_not_raise(self, tmp_path: Path) -> None:
        """An all-no-data AOI writes an all-no-data crop instead of raising."""
        src = tmp_path / "src.tif"
        _write_geotiff(src, no_data_value=-9999.0, fill=-9999.0)
        out = tmp_path / "empty.tif"
        _helpers.read_part_to_geotiff(str(src), [0.2, -0.6, 0.6, -0.2], out)
        result = Dataset.read_file(str(out))
        assert bool((result.read_array() == -9999.0).all()), "written all-no-data"


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
