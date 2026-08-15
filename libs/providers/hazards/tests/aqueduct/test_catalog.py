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
    catalog = Catalog()
    with pytest.raises(ValueError, match="Did you mean 'country'"):
        catalog.get("countries")


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


def test_admin_level_and_scenario_models_are_frozen() -> None:
    """The AdminLevel and Scenario rows are immutable and reject extra fields."""
    from pydantic import ValidationError

    row = AdminLevel(zip="c.zip", shapefile_stem="c")
    with pytest.raises(ValidationError):
        row.zip = "other.zip"
    with pytest.raises(ValidationError):
        AdminLevel(zip="c.zip", shapefile_stem="c", bogus=1)
    scenario = Scenario(code="bh", years=["2010"])
    with pytest.raises(ValidationError):
        scenario.code = "24"


def test_get_catalog_returns_datasets() -> None:
    """get_catalog returns the same admin-level map as datasets."""
    cat = Catalog()
    assert cat.get_catalog() is cat.datasets


def test_cli_validator_flags_empty_vocabulary() -> None:
    """The datasets-validate validator passes a full catalog but flags an empty vocab."""
    from earthlens.aqueduct.cli import validator

    checked, issues = validator(Catalog())
    assert checked == 3
    assert issues == []
    emptied = Catalog().model_copy(update={"return_periods": {}})
    _, broken = validator(emptied)
    assert any("return_periods" in issue for issue in broken)


def test_malformed_admin_level_raises(tmp_path: Path, monkeypatch) -> None:
    """An admin-level row with an unknown field fails validation."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "admin_levels:\n  country:\n    zip: c.zip\n    shapefile_stem: c\n"
        "    bogus: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", bad)
    clear_catalog_cache()
    with pytest.raises(ValueError, match="admin level 'country' failed validation"):
        Catalog.load()
    clear_catalog_cache()


def test_malformed_scenario_raises(tmp_path: Path, monkeypatch) -> None:
    """A scenario row with an unknown field fails validation."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "admin_levels:\n  country:\n    zip: c.zip\n    shapefile_stem: c\n"
        "scenarios:\n  x:\n    code: bh\n    bogus: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", bad)
    clear_catalog_cache()
    with pytest.raises(ValueError, match="scenario 'x' failed validation"):
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
