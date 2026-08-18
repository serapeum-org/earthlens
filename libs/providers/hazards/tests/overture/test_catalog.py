"""Unit tests for `earthlens.overture.catalog`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.overture import catalog as overture_catalog
from earthlens.overture.catalog import CATALOG_PATH, Catalog, Theme
from earthlens.overture.releases import is_release_id


@pytest.mark.overture
class TestTheme:
    """`Theme` validation and type resolution."""

    def test_build_minimal(self):
        """A theme builds from types/default_type/geometry."""
        theme = Theme(types=["place"], default_type="place", geometry="Point")
        assert theme.default_type == "place"
        assert theme.geometry == "Point"

    def test_default_type_must_be_in_types(self):
        """`default_type` outside `types` is rejected at construction."""
        with pytest.raises(Exception) as exc_info:
            Theme(types=["building"], default_type="place", geometry="Polygon")
        assert "default_type" in str(exc_info.value)

    def test_extra_field_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            Theme(
                types=["place"],
                default_type="place",
                geometry="Point",
                bogus="x",
            )

    def test_empty_types_rejected(self):
        """A theme with no types is invalid."""
        with pytest.raises(Exception):
            Theme(types=[], default_type="place", geometry="Point")

    def test_resolve_types_empty_returns_default(self):
        """An empty/`None` request resolves to the theme's default type."""
        theme = Theme(
            types=["building", "building_part"],
            default_type="building",
            geometry="Polygon",
        )
        assert theme.resolve_types([]) == ["building"]
        assert theme.resolve_types(None) == ["building"]

    def test_resolve_types_explicit(self):
        """An explicit valid type list passes through unchanged."""
        theme = Theme(
            types=["segment", "connector"],
            default_type="segment",
            geometry="LineString",
        )
        assert theme.resolve_types(["connector"]) == ["connector"]

    def test_resolve_types_unknown_raises(self):
        """An unknown requested type raises with the valid set named."""
        theme = Theme(types=["place"], default_type="place", geometry="Point")
        with pytest.raises(ValueError, match=r"not valid types"):
            theme.resolve_types(["road"])


@pytest.mark.overture
class TestCatalog:
    """`Catalog` loading, lookup, and release index."""

    def test_themes_sorted(self):
        """`themes()` lists the six curated themes, sorted."""
        assert Catalog().themes() == [
            "addresses",
            "base",
            "buildings",
            "divisions",
            "places",
            "transportation",
        ]

    def test_get_theme_resolves(self):
        """`get_theme` returns the matching `Theme`."""
        theme = Catalog().get_theme("buildings")
        assert theme.default_type == "building"
        assert "building_part" in theme.types

    def test_get_theme_unknown_did_you_mean(self):
        """An unknown theme raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match=r"Did you mean 'buildings'\?"):
            Catalog().get_theme("building")

    def test_dict_surface(self):
        """The catalog supports `in`, `[]`, and `len`."""
        cat = Catalog()
        assert "places" in cat
        assert cat["places"].default_type == "place"
        assert len(cat) == 6

    def test_iter_yields_theme_keys(self):
        """Iterating the catalog yields its curated theme names."""
        assert set(Catalog()) == {
            "addresses",
            "base",
            "buildings",
            "divisions",
            "places",
            "transportation",
        }

    def test_curated_themes_have_licenses(self):
        """Every curated theme lists ODbL among its licenses."""
        cat = Catalog()
        for name in cat.themes():
            assert "ODbL-1.0" in cat.get_theme(name).licenses, name

    def test_available_datasets_covers_universe(self):
        """The available index lists every Overture type (curated + deferred)."""
        available = set(Catalog().available_datasets)
        assert {"building", "place", "segment", "division_area"} <= available
        assert {"address", "land", "water"} <= available, "base/addresses indexed too"
        assert len(available) >= 15

    def test_curated_types_are_subset_of_available(self):
        """Every curated theme's types are members of the available index."""
        cat = Catalog()
        available = set(cat.available_datasets)
        for name in cat.themes():
            assert set(cat.get_theme(name).types) <= available, name

    def test_available_types_sorted(self):
        """`available_types` returns the index sorted."""
        types = Catalog().available_types()
        assert types == sorted(types)

    def test_available_releases_indexed(self):
        """The bundled YAML ships a non-empty, well-formed release index."""
        releases = Catalog().available_releases
        assert releases, "the bundled catalog should ship a release index"
        assert all(is_release_id(r) for r in releases), releases

    def test_latest_release_is_the_newest_indexed(self):
        """`latest_release` returns the newest release the bundled index carries."""
        cat = Catalog()
        newest = sorted(
            cat.available_releases,
            key=lambda r: (r.split(".")[0], int(r.split(".")[1])),
        )[-1]
        assert cat.latest_release() == newest

    def test_latest_release_none_when_index_empty(self):
        """`latest_release` is `None` when the index is explicitly empty."""
        cat = Catalog(datasets=Catalog().datasets, available_releases=[])
        assert cat.latest_release() is None

    def test_latest_release_returns_newest(self):
        """`latest_release` returns the newest of a supplied index."""
        cat = Catalog(
            datasets=Catalog().datasets,
            available_releases=["2026-05-20.0", "2026-04-15.0"],
        )
        assert cat.latest_release() == "2026-05-20.0"

    def test_latest_release_ignores_index_order(self):
        """An ascending index — the order a refresh writes — still yields the newest."""
        cat = Catalog(
            datasets=Catalog().datasets,
            available_releases=["2026-04-15.0", "2026-05-20.0"],
        )
        assert cat.latest_release() == "2026-05-20.0"

    def test_latest_release_compares_the_ordinal_numerically(self):
        """A two-digit ordinal beats a one-digit one from the same date."""
        cat = Catalog(
            datasets=Catalog().datasets,
            available_releases=["2026-07-22.9", "2026-07-22.10"],
        )
        assert cat.latest_release() == "2026-07-22.10"

    def test_latest_release_ignores_an_id_without_an_ordinal(self):
        """An id with no ordinal is not a release id and loses to one that is."""
        cat = Catalog(
            datasets=Catalog().datasets,
            available_releases=["2026-07-22.0", "2026-07-22"],
        )
        assert cat.latest_release() == "2026-07-22.0"

    def test_latest_release_ignores_a_malformed_entry(self):
        """A junk entry is skipped rather than sorted above a real release."""
        cat = Catalog(
            datasets=Catalog().datasets,
            available_releases=["2026-07-22.0", "https:"],
        )
        assert cat.latest_release() == "2026-07-22.0"

    def test_latest_release_none_when_every_entry_is_malformed(self):
        """An index with nothing release-shaped resolves to no release at all."""
        cat = Catalog(datasets=Catalog().datasets, available_releases=["https:"])
        assert cat.latest_release() is None

    def test_clear_catalog_cache_empties_the_parse_cache(self):
        """Clearing the cache drops the memoised parse and reloading still works."""
        Catalog()
        assert overture_catalog._CATALOG_CACHE, "loading memoises the parse"
        overture_catalog.clear_catalog_cache()
        assert not overture_catalog._CATALOG_CACHE, "the cache is emptied"
        assert Catalog().themes(), "the catalog reloads after a clear"

    def test_load_missing_themes_block_raises(self, tmp_path: Path):
        """A YAML without a `themes:` block is rejected."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("available_releases: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"empty 'themes:' block"):
            Catalog.load(bad)

    def test_load_malformed_theme_raises(self, tmp_path: Path):
        """A theme row that fails `Theme` validation is reported by name."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "themes:\n  oops:\n    types: []\n    default_type: x\n    geometry: P\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=r"theme 'oops' failed validation"):
            Catalog.load(bad)

    def test_catalog_path_points_at_bundled_yaml(self):
        """`CATALOG_PATH` points at the shipped YAML next to the module."""
        assert CATALOG_PATH.name == "overture_data_catalog.yaml"
        assert CATALOG_PATH.exists()

    def test_get_catalog_returns_datasets(self):
        """`get_catalog` returns the same object as `datasets`."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets
