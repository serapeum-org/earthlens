"""Tests for the Tropycal basin -> track-field catalog."""

from __future__ import annotations

import pytest
from earthlens.tropycal.catalog import CATALOG_PATH
from pydantic import ValidationError

from earthlens.tropycal import Basin, Catalog, TrackField

pytestmark = pytest.mark.tropycal

_EXPECTED_CODES = [
    "all",
    "australia",
    "both",
    "east_pacific",
    "north_atlantic",
    "north_indian",
    "south_atlantic",
    "south_indian",
    "south_pacific",
    "west_pacific",
]


class TestTrackField:
    """Tests for the TrackField model."""

    def test_build(self):
        """A TrackField stores units and long_name."""
        field = TrackField(units="kt", long_name="Maximum sustained wind")
        assert field.units == "kt"
        assert field.long_name == "Maximum sustained wind"

    def test_extra_forbidden(self):
        """An unknown key on TrackField is rejected."""
        with pytest.raises(ValidationError):
            TrackField(units="kt", bogus=1)


class TestBasin:
    """Tests for the Basin model."""

    def test_build(self):
        """A Basin stores name, sources, and typed fields."""
        basin = Basin(
            name="North Atlantic",
            sources=["ibtracs", "hurdat"],
            fields={"vmax": {"units": "kt"}},
        )
        assert basin.name == "North Atlantic"
        assert basin.sources == ["ibtracs", "hurdat"]
        assert isinstance(basin.fields["vmax"], TrackField)

    def test_extra_forbidden(self):
        """An unknown key on Basin is rejected."""
        with pytest.raises(ValidationError):
            Basin(name="X", typo=1)


class TestCatalog:
    """Tests for the Catalog loader and lookups."""

    def test_bundled_yaml_loads(self):
        """The bundled YAML loads through a default Catalog()."""
        assert Catalog().codes() == _EXPECTED_CODES

    def test_get_basin(self):
        """get_basin resolves a known basin to its row."""
        assert Catalog().get_basin("north_atlantic").name == "North Atlantic"

    def test_contains_and_len(self):
        """The dict-like surface (in / len) is inherited from AbstractCatalog."""
        cat = Catalog()
        assert "north_atlantic" in cat
        assert len(cat) == len(_EXPECTED_CODES)

    def test_sources_for_hurdat_basin(self):
        """North Atlantic is served by both ibtracs and hurdat."""
        assert Catalog().sources_for("north_atlantic") == ["ibtracs", "hurdat"]

    def test_sources_for_ibtracs_only_basin(self):
        """West Pacific is served only by ibtracs (no jtwc source)."""
        assert Catalog().sources_for("west_pacific") == ["ibtracs"]

    def test_get_field_units(self):
        """get_field returns a track field with its declared units."""
        assert Catalog().get_field("north_atlantic", "mslp").units == "hPa"

    def test_get_field_unknown_field_raises_keyerror(self):
        """An unknown field code raises KeyError."""
        with pytest.raises(KeyError):
            Catalog().get_field("north_atlantic", "nope")

    def test_unknown_basin_did_you_mean(self):
        """An unknown basin raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'north_atlantic'") as exc:
            Catalog().get_basin("north_altantic")
        assert "Tropycal basin catalog" in str(exc.value)

    def test_missing_basins_block_raises(self, tmp_path):
        """A YAML with no basins: block raises a clear ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="basins"):
            Catalog.load(catalog_path=bad)

    def test_malformed_row_raises(self, tmp_path):
        """A basin row with an unknown key fails validation at load time."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "basins:\n  north_atlantic:\n    name: NA\n    bogus: 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(catalog_path=bad)

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same object as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_catalog_path_points_at_bundled_yaml(self):
        """CATALOG_PATH points at the shipped basin YAML."""
        assert CATALOG_PATH.name == "tropycal_data_catalog.yaml"
        assert CATALOG_PATH.exists()

    def test_describe(self):
        """describe() returns the basin's name, sources, and sorted fields."""
        info = Catalog().describe("north_atlantic")
        assert info == {
            "basin": "north_atlantic",
            "name": "North Atlantic",
            "sources": ["ibtracs", "hurdat"],
            "fields": ["category", "mslp", "vmax"],
        }

    def test_describe_unknown_raises(self):
        """describe() on an unknown basin raises the did-you-mean ValueError."""
        with pytest.raises(ValueError, match="Tropycal basin catalog"):
            Catalog().describe("atlantis")

    def test_available_datasets_index_populated(self):
        """available_datasets indexes the whole basin universe (gee/ecmwf parity)."""
        assert Catalog().available_datasets == _EXPECTED_CODES

    def test_health_clean_on_bundled_catalog(self):
        """health() reports no offenders for the shipped catalog."""
        report = Catalog().health()
        assert report == {
            "basin_without_sources": [],
            "basin_without_fields": [],
            "basin_unknown_source": [],
        }

    def test_health_flags_bad_rows(self):
        """health() flags empty sources/fields and unknown sources."""
        from earthlens.tropycal import Basin

        cat = Catalog(
            datasets={
                "north_atlantic": Basin(
                    name="NA", sources=["ibtracs"], fields={"vmax": {"units": "kt"}}
                ),
                "nosrc": Basin(
                    name="NoSrc", sources=[], fields={"vmax": {"units": "kt"}}
                ),
                "broken": Basin(name="Broken", sources=["jtwc"], fields={}),
            }
        )
        report = cat.health()
        assert report["basin_without_sources"] == ["nosrc"]
        assert report["basin_without_fields"] == ["broken"]
        assert report["basin_unknown_source"] == ["broken:jtwc"]

    def test_load_nonexistent_path_raises(self, tmp_path):
        """Loading a non-existent catalog path raises FileNotFoundError."""
        from earthlens.tropycal.catalog import clear_catalog_cache

        clear_catalog_cache()
        with pytest.raises(FileNotFoundError):
            Catalog.load(catalog_path=tmp_path / "does_not_exist.yaml")


class TestCatalogCache:
    """Tests for the (path, mtime) parse cache."""

    def test_repeated_construction_uses_cache(self):
        """A second Catalog() on the unchanged file reuses the cached parse."""
        from earthlens.tropycal.catalog import _CATALOG_CACHE, clear_catalog_cache

        clear_catalog_cache()
        Catalog()
        assert len(_CATALOG_CACHE) == 1
        Catalog()
        assert len(_CATALOG_CACHE) == 1

    def test_clear_cache_forces_reparse(self, tmp_path):
        """clear_catalog_cache empties the cache so the next load re-parses."""
        from earthlens.tropycal.catalog import _CATALOG_CACHE, clear_catalog_cache

        Catalog()
        assert _CATALOG_CACHE
        clear_catalog_cache()
        assert not _CATALOG_CACHE

    def test_mtime_change_invalidates_cache(self, tmp_path):
        """A rewritten file (new mtime) is re-parsed rather than served stale."""
        from earthlens.tropycal.catalog import clear_catalog_cache

        clear_catalog_cache()
        yaml = tmp_path / "cat.yaml"
        yaml.write_text(
            "basins:\n  north_atlantic:\n    name: NA\n    sources: [ibtracs]\n",
            encoding="utf-8",
        )
        first = Catalog.load(catalog_path=yaml)
        assert first.codes() == ["north_atlantic"]
        yaml.write_text(
            "basins:\n"
            "  north_atlantic:\n    name: NA\n    sources: [ibtracs]\n"
            "  west_pacific:\n    name: WP\n    sources: [ibtracs]\n",
            encoding="utf-8",
        )
        second = Catalog.load(catalog_path=yaml)
        assert second.codes() == ["north_atlantic", "west_pacific"]
