"""Unit tests for the CatRaRE backend (offline, via a pre-seeded FileGDB cache)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.catrare import CatRaRE

pytestmark = pytest.mark.catrare


def _backend(cache: Path, **kwargs) -> CatRaRE:
    """Build a CatRaRE backend pointed at the pre-seeded cache dir."""
    return CatRaRE(cache_dir=cache, **kwargs)


def test_download_returns_feature_collection(catrare_cache, tmp_path):
    """A default download returns all three synthetic events, reprojected."""
    fc = _backend(catrare_cache, path=tmp_path).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 3
    assert fc.crs.to_epsg() == 4326
    assert "Event_ID" in fc.columns


def test_geometry_false_returns_dataframe(catrare_cache):
    """`geometry=False` returns a geometry-dropped DataFrame (tabular)."""
    backend = _backend(catrare_cache, geometry=False)
    out = backend.download()
    assert isinstance(out, pd.DataFrame)
    assert backend.OUTPUT_KIND == "tabular"
    assert "geometry" not in out.columns


def test_date_window_filters_events(catrare_cache):
    """A July-2021 window drops the 2005 event."""
    out = _backend(
        catrare_cache, start="2021-07-01", end="2021-07-31", geometry=False
    ).download()
    assert sorted(out["Event_ID"]) == [1, 2]


def test_points_layer_is_readable(catrare_cache, tmp_path):
    """`geometry_layer='points'` reads the RRmaxPoints layer (point geometry)."""
    fc = _backend(catrare_cache, geometry_layer="points", path=tmp_path).download()
    assert set(fc.geometry.geom_type.unique()) <= {"Point"}


def test_cache_miss_triggers_download(tmp_path, write_canned_gdb, monkeypatch):
    """An empty cache downloads the FileGDB via HttpClient before reading."""
    calls: list[tuple] = []

    def _fake_download(self, url, dest, **kwargs):
        calls.append((url, dest))
        write_canned_gdb(dest)

    monkeypatch.setattr(
        "earthlens.catrare.backend.HttpClient.download", _fake_download
    )
    fc = _backend(tmp_path / "cache", geometry=False).download()
    assert len(calls) == 1
    assert len(fc) == 3


def test_invalid_cached_zip_is_redownloaded(tmp_path, write_canned_gdb, monkeypatch):
    """A truncated cached FileGDB is discarded and re-downloaded."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "catrare_t5.gdb.zip").write_bytes(b"not a zip")
    downloaded: list[Path] = []

    def _fake_download(self, url, dest, **kwargs):
        downloaded.append(dest)
        write_canned_gdb(dest)

    monkeypatch.setattr(
        "earthlens.catrare.backend.HttpClient.download", _fake_download
    )
    fc = _backend(cache, geometry=False).download()
    assert len(downloaded) == 1
    assert len(fc) == 3


def test_unknown_threshold_rejected_at_construction(catrare_cache):
    """A bad `threshold=` fails at construction, before any download."""
    with pytest.raises(ValueError, match="CatRaRE catalog"):
        _backend(catrare_cache, threshold="t9")


def test_unknown_geometry_layer_rejected_at_construction(catrare_cache):
    """A bad `geometry_layer=` fails at construction."""
    with pytest.raises(ValueError, match="is not a CatRaRE geometry kind"):
        _backend(catrare_cache, geometry_layer="lines")


def test_written_file_lands_under_path(catrare_cache, tmp_path):
    """With `path` set, a GeoPackage is written and the collection returned."""
    fc = CatRaRE(cache_dir=catrare_cache, path=tmp_path).download()
    assert len(fc) == 3
    assert len(list(tmp_path.glob("catrare_t5_zones.gpkg"))) == 1


def test_empty_result_with_geometry_writes_nothing(catrare_cache, tmp_path):
    """A date window matching nothing returns empty and writes no file."""
    fc = CatRaRE(
        cache_dir=catrare_cache, path=tmp_path, start="1990-01-01", end="1990-12-31"
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 0
    assert not list(tmp_path.glob("*.gpkg"))


def test_empty_tabular_result_is_empty_frame(catrare_cache):
    """A no-match date window with geometry=False yields an empty DataFrame."""
    out = _backend(
        catrare_cache, start="1990-01-01", end="1990-12-31", geometry=False
    ).download()
    assert isinstance(out, pd.DataFrame)
    assert out.empty


def test_written_filename_embeds_bbox(catrare_cache, tmp_path):
    """A bbox request writes a file whose name carries the bbox tag."""
    CatRaRE(
        cache_dir=catrare_cache,
        path=tmp_path,
        lat_lim=[47.0, 55.0],
        lon_lim=[5.0, 15.0],
    ).download()
    assert len(list(tmp_path.glob("catrare_t5_zones_bbox*.gpkg"))) == 1
