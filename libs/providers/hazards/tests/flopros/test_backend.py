"""Unit tests for the FLOPROS backend (offline, via a pre-seeded cache)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.flopros import FLOPROS

pytestmark = pytest.mark.flopros


def _backend(cache: Path, **kwargs) -> FLOPROS:
    """Build a FLOPROS backend pointed at the pre-seeded cache dir."""
    return FLOPROS(cache_dir=cache, **kwargs)


def test_download_returns_feature_collection(flopros_cache):
    """A default download returns all three units as a FeatureCollection."""
    fc = _backend(flopros_cache).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 3
    assert "merged_riverine" in fc.columns


def test_layer_selection_keeps_only_requested_columns(flopros_cache):
    """`layer=` trims the value columns to the requested layer(s)."""
    fc = _backend(flopros_cache, layer="merged_riverine").download()
    value_cols = [c for c in fc.columns if c not in ("name", "geonunit", "type_en")]
    assert "merged_riverine" in value_cols
    assert "modelled_riverine" not in value_cols


def test_geometry_false_returns_dataframe(flopros_cache):
    """`geometry=False` returns a geometry-dropped DataFrame (tabular)."""
    backend = _backend(flopros_cache, geometry=False)
    out = backend.download()
    assert isinstance(out, pd.DataFrame)
    assert backend.OUTPUT_KIND == "tabular"
    assert "geometry" not in out.columns


def test_country_filter_selects_one_unit(flopros_cache):
    """`country=` keeps only the matching unit (name or geonunit)."""
    fc = _backend(flopros_cache, country="Betaland", geometry=False).download()
    assert list(fc["name"]) == ["Beta Province"]


def test_bbox_filter_drops_far_unit(flopros_cache):
    """A bbox over the equator drops the far-away third unit."""
    fc = _backend(
        flopros_cache, lat_lim=[-1.0, 4.0], lon_lim=[-1.0, 4.0], geometry=False
    ).download()
    assert set(fc["name"]) == {"Alphaland", "Beta Province"}


def test_country_miss_returns_empty(flopros_cache):
    """A non-matching country yields an empty table, not an error."""
    out = _backend(flopros_cache, country="Atlantis", geometry=False).download()
    assert out.empty


def test_unknown_layer_rejected_at_construction(flopros_cache):
    """A bad `layer=` fails at construction, before any download."""
    with pytest.raises(ValueError, match="is not a FLOPROS layer"):
        _backend(flopros_cache, layer="hurricane")


def test_written_file_lands_under_path(flopros_cache, tmp_path):
    """With `path` set, a GeoPackage is written and the collection returned."""
    fc = FLOPROS(
        cache_dir=flopros_cache, path=tmp_path, layer="merged_riverine"
    ).download()
    assert len(fc) == 3
    written = list(tmp_path.glob("flopros_*.gpkg"))
    assert len(written) == 1


def test_write_filename_embeds_country_and_bbox(flopros_cache, tmp_path):
    """The written filename carries the country slug and a bbox tag."""
    FLOPROS(
        cache_dir=flopros_cache,
        path=tmp_path,
        layer="merged_riverine",
        country="Alphaland",
        lat_lim=[-1.0, 1.5],
        lon_lim=[-1.0, 1.5],
    ).download()
    written = list(tmp_path.glob("flopros_merged_riverine_Alphaland_bbox*.gpkg"))
    assert len(written) == 1


def test_empty_result_with_geometry_writes_nothing(flopros_cache, tmp_path):
    """A country miss with geometry=True returns empty and writes no file."""
    fc = FLOPROS(
        cache_dir=flopros_cache, path=tmp_path, country="Atlantis"
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 0
    assert not list(tmp_path.glob("*.gpkg"))


def test_cache_miss_triggers_download(tmp_path, write_canned_zip, monkeypatch):
    """An empty cache downloads the zip via HttpClient before reading."""
    calls: list[tuple] = []

    def _fake_download(self, url, dest, **kwargs):
        calls.append((url, dest))
        write_canned_zip(dest)

    monkeypatch.setattr(
        "earthlens.flopros.backend.HttpClient.download", _fake_download
    )
    fc = FLOPROS(cache_dir=tmp_path / "cache", geometry=False).download()
    assert len(calls) == 1
    assert len(fc) == 3


def test_invalid_cached_zip_is_redownloaded(tmp_path, write_canned_zip, monkeypatch):
    """A truncated cached zip is discarded and re-downloaded."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "flopros_supplement.zip").write_bytes(b"not a zip")
    downloaded: list[Path] = []

    def _fake_download(self, url, dest, **kwargs):
        downloaded.append(dest)
        write_canned_zip(dest)

    monkeypatch.setattr(
        "earthlens.flopros.backend.HttpClient.download", _fake_download
    )
    fc = FLOPROS(cache_dir=cache, geometry=False).download()
    assert len(downloaded) == 1
    assert len(fc) == 3
