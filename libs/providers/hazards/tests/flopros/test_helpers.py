"""Unit tests for `earthlens.flopros._helpers`."""

from __future__ import annotations

from types import SimpleNamespace

import geopandas as gpd
import pytest
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import box

from earthlens.flopros import Catalog
from earthlens.flopros._helpers import (
    _is_global,
    build_feature_collection,
    filter_units,
    resolve_layers,
)

pytestmark = pytest.mark.flopros


def _source() -> FeatureCollection:
    """A two-unit source collection with identity + two layer columns."""
    gdf = gpd.GeoDataFrame(
        {
            "name": ["Alphaland", "Beta Province"],
            "geonunit": ["Alphaland", "Betaland"],
            "type_en": ["Country", "Province"],
            "MerL_Riv": [100.0, 250.0],
            "ModL_Riv": [80.0, 200.0],
        },
        geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3)],
        crs="EPSG:4326",
    )
    return FeatureCollection(gdf)


def _global_space() -> SimpleNamespace:
    """A whole-globe spatial extent (no bbox filter)."""
    return SimpleNamespace(
        latitude_min=-90.0, latitude_max=90.0, longitude_min=-180.0, longitude_max=180.0
    )


def test_resolve_layers_none_returns_all():
    """`layer=None` returns every catalog layer in order."""
    resolved = resolve_layers(Catalog().get("flopros"), None)
    assert resolved["merged_riverine"] == "MerL_Riv"
    assert len(resolved) == 10


def test_resolve_layers_single_name():
    """A single layer name resolves to its one source column."""
    assert resolve_layers(Catalog().get("flopros"), "merged_riverine") == {
        "merged_riverine": "MerL_Riv"
    }


def test_resolve_layers_dedupes_a_list():
    """A list of layers is de-duplicated, order preserved."""
    resolved = resolve_layers(
        Catalog().get("flopros"),
        ["modelled_riverine", "merged_riverine", "modelled_riverine"],
    )
    assert list(resolved) == ["modelled_riverine", "merged_riverine"]


def test_resolve_layers_unknown_raises():
    """An unknown layer name raises a listing `ValueError`."""
    with pytest.raises(ValueError, match="is not a FLOPROS layer"):
        resolve_layers(Catalog().get("flopros"), "typhoon")


def test_build_feature_collection_selects_and_renames():
    """The trimmed collection keeps identity + renamed layer columns."""
    trimmed = build_feature_collection(
        _source(), ["name", "geonunit", "type_en"], {"merged_riverine": "MerL_Riv"}
    )
    assert "merged_riverine" in trimmed.columns
    assert "MerL_Riv" not in trimmed.columns
    assert list(trimmed["merged_riverine"]) == [100.0, 250.0]


def test_build_feature_collection_missing_column_raises():
    """A missing source column is a clean domain error, not a KeyError."""
    with pytest.raises(ValueError, match="missing expected column"):
        build_feature_collection(_source(), ["name"], {"ghost": "NotThere"})


def test_filter_units_by_country_matches_name_or_geonunit():
    """`country` matches case-insensitively on `name` or `geonunit`."""
    trimmed = build_feature_collection(
        _source(), ["name", "geonunit", "type_en"], {"merged_riverine": "MerL_Riv"}
    )
    result = filter_units(trimmed, "betaland", _global_space())
    assert list(result["name"]) == ["Beta Province"]


def test_filter_units_by_bbox_keeps_intersecting():
    """A bbox keeps only rows whose geometry intersects it."""
    trimmed = build_feature_collection(
        _source(), ["name", "geonunit", "type_en"], {"merged_riverine": "MerL_Riv"}
    )
    space = SimpleNamespace(
        latitude_min=0.0, latitude_max=1.5, longitude_min=0.0, longitude_max=1.5
    )
    result = filter_units(trimmed, None, space)
    assert list(result["name"]) == ["Alphaland"]


def test_filter_units_country_miss_is_empty():
    """A country that matches nothing yields an empty collection."""
    trimmed = build_feature_collection(
        _source(), ["name", "geonunit", "type_en"], {"merged_riverine": "MerL_Riv"}
    )
    assert filter_units(trimmed, "Atlantis", _global_space()).empty


def test_extract_shapefile_missing_shp_raises(tmp_path):
    """A zip with no matching `.shp` member raises a listing FileNotFoundError."""
    import zipfile

    from earthlens.flopros._helpers import extract_shapefile

    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("MYSTEM.dbf", b"only a sidecar, no .shp")
    with pytest.raises(FileNotFoundError, match="MYSTEM.shp is not a member"):
        extract_shapefile(zip_path, "MYSTEM", tmp_path / "out")


def test_is_global_true_for_whole_earth():
    """`_is_global` is True only for a full WGS84 box."""
    assert _is_global(_global_space())
    assert not _is_global(
        SimpleNamespace(
            latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
        )
    )
