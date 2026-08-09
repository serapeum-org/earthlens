"""Unit tests for the Aqueduct catalog loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.aqueduct import AdminLevel, Catalog, Scenario, clear_catalog_cache
from earthlens.aqueduct import catalog as catalog_module

pytestmark = pytest.mark.aqueduct


def test_bundled_catalog_lists_three_admin_levels() -> None:
    """The shipped catalog exposes exactly country, state, and basin."""
    assert Catalog().available() == ["basin", "country", "state"]


def test_get_returns_admin_level_row() -> None:
    """get resolves an admin level to its AdminLevel spec."""
    row = Catalog().get("country")
    assert isinstance(row, AdminLevel)
    assert row.shapefile_stem == "aqueduct_global_flood_risk_data_by_country_20150304"
    assert row.container_zip is None


def test_get_unknown_level_raises_with_hint() -> None:
    """An unknown admin level raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'country'"):
        Catalog().get("countries")


def test_download_url_direct_level_uses_own_zip() -> None:
    """A direct level's URL is its own zip under base_url."""
    url = Catalog().download_url("basin")
    assert url.startswith("https://files.wri.org/")
    assert url.endswith("by_river_basin_20150304.zip")


def test_download_url_nested_level_uses_container_zip() -> None:
    """State has no standalone URL, so its download URL is the bundle."""
    assert Catalog().download_url("state").endswith("maps_and_data_20150304.zip")


def test_indicator_year_return_period_vocabularies() -> None:
    """The code-map vocabularies match the pinned dictionary decode."""
    cat = Catalog()
    assert cat.indicators == {
        "gdp_affected": "G",
        "population_affected": "P",
        "urban_damage": "U",
    }
    assert cat.years == {"2010": "10", "2030": "30"}
    assert cat.return_periods[1000] == "1T"
    assert cat.return_periods[100] == "100"


def test_scenarios_carry_code_and_valid_years() -> None:
    """The baseline is 2010-only and the futures are 2030-only."""
    cat = Catalog()
    assert cat.scenarios["baseline"] == Scenario(code="bh", years=["2010"])
    assert cat.scenarios["ssp2-rcp8p5"].code == "28"
    assert cat.scenarios["ssp2-rcp8p5"].years == ["2030"]
    assert all(
        s.years == ["2030"] for name, s in cat.scenarios.items() if name != "baseline"
    )


def test_license_and_attribution_recorded() -> None:
    """The permissive licence and attribution are carried on the catalog."""
    cat = Catalog()
    assert cat.license == "CC-BY-4.0"
    assert "World Resources Institute" in cat.attribution


def test_missing_admin_levels_block_raises(tmp_path: Path, monkeypatch) -> None:
    """A catalog file with no admin_levels block fails fast."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("base_url: https://example.org\n", encoding="utf-8")
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", bad)
    clear_catalog_cache()
    with pytest.raises(ValueError, match="admin_levels"):
        Catalog.load()
    clear_catalog_cache()


def test_load_accepts_explicit_path(tmp_path: Path) -> None:
    """load reads an explicit catalog path, not just the bundled default."""
    custom = tmp_path / "one.yaml"
    custom.write_text(
        "base_url: https://h\n"
        "admin_levels:\n"
        "  country:\n"
        "    zip: c.zip\n"
        "    shapefile_stem: c\n",
        encoding="utf-8",
    )
    clear_catalog_cache()
    cat = Catalog.load(custom)
    assert cat.available() == ["country"]
    assert cat.download_url("country") == "https://h/c.zip"
    clear_catalog_cache()
