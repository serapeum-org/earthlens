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
