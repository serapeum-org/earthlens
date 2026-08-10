"""Unit tests for the ISIMIP catalog loader and vocabulary."""

from __future__ import annotations

import pytest

from earthlens.isimip import (
    CATALOG_PATH,
    Catalog,
    Forcing,
    Round,
    Scenario,
    Variable,
    clear_catalog_cache,
)
from earthlens.isimip import catalog as catalog_mod

pytestmark = [pytest.mark.isimip, pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the parse cache around each test so disk edits are seen."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestCatalogLoad:
    """Tests for loading the bundled ISIMIP catalog."""

    def test_bundled_catalog_loads(self):
        """The bundled YAML loads with the expected config and vocab blocks."""
        cat = Catalog()
        assert cat.data_url == "https://data.isimip.org/api/v1", cat.data_url
        assert cat.files_api_url == "https://files.isimip.org/api/v2", cat.files_api_url
        assert "InputData" in cat.products and "OutputData" in cat.products
        assert cat.time_steps == ["daily", "monthly"], cat.time_steps
        assert cat.datasets and cat.forcings and cat.scenarios and cat.rounds

    def test_available_datasets_sorted(self):
        """`available_datasets` is the sorted variable-key index."""
        cat = Catalog()
        assert cat.available_datasets == sorted(cat.datasets), cat.available_datasets

    def test_rows_are_typed(self):
        """Each vocabulary block holds its typed frozen row model."""
        cat = Catalog()
        assert isinstance(cat.get_dataset("pr"), Variable)
        assert isinstance(cat.get_forcing("gfdl-esm4"), Forcing)
        assert isinstance(cat.get_scenario("ssp585"), Scenario)
        assert isinstance(cat.get_round("ISIMIP3b"), Round)

    def test_variable_metadata(self):
        """A curated variable carries its CMOR units and long name."""
        pr = Catalog().get_dataset("pr")
        assert pr.units == "kg m-2 s-1", pr.units
        assert "recipitation" in pr.long_name, pr.long_name

    def test_get_catalog_returns_datasets(self):
        """`get_catalog()` returns the same object as `datasets`."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_repr_counts(self):
        """The repr reports dataset counts, not contents."""
        assert "datasets=" in repr(Catalog())

    def test_catalog_path_points_at_bundled_yaml(self):
        """`CATALOG_PATH` points at the shipped `isimip_data_catalog.yaml`."""
        assert CATALOG_PATH.name == "isimip_data_catalog.yaml"
        assert CATALOG_PATH.exists(), CATALOG_PATH


class TestLookups:
    """Tests for the did-you-mean lookups over the vocabulary blocks."""

    def test_forcing_round(self):
        """A 3b GCM reports its simulation round."""
        assert Catalog().get_forcing("gfdl-esm4").round == "ISIMIP3b"

    def test_scenario_round(self):
        """A scenario reports its simulation round."""
        assert Catalog().get_scenario("ssp585").round == "ISIMIP3b"

    def test_round_default_license(self):
        """A round carries a documentation licence label."""
        assert "CC0" in Catalog().get_round("ISIMIP3b").default_license

    @pytest.mark.parametrize(
        "getter, key, noun",
        [
            ("get_forcing", "gfdl-esm5", "forcing"),
            ("get_scenario", "ssp999", "scenario"),
            ("get_round", "ISIMIP9z", "round"),
        ],
    )
    def test_unknown_key_raises_with_hint(self, getter, key, noun):
        """An unknown forcing / scenario / round raises a did-you-mean ValueError."""
        with pytest.raises(ValueError, match=f"not a curated ISIMIP {noun}"):
            getattr(Catalog(), getter)(key)

    def test_unknown_variable_raises(self):
        """An unknown variable raises via the base did-you-mean lookup."""
        with pytest.raises(ValueError, match="not in the ISIMIP catalog"):
            Catalog().get_dataset("rainfall")

    def test_contains_and_getitem(self):
        """`in` and `[]` operate over the curated variable map."""
        cat = Catalog()
        assert "pr" in cat
        assert cat["pr"].units == "kg m-2 s-1"
        with pytest.raises(KeyError):
            _ = cat["not-a-var"]


class TestNormalizeForcing:
    """Tests for the forcing-name normaliser."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("GFDL-ESM4", "gfdl-esm4"),
            ("gfdl-esm4", "gfdl-esm4"),
            ("UKESM1-0-LL", "ukesm1-0-ll"),
        ],
    )
    def test_lowercases(self, raw, expected):
        """Any casing normalises to the lowercase API spelling."""
        assert Catalog.normalize_forcing(raw) == expected


class TestParseErrors:
    """Tests for catalog parse-time validation errors."""

    def test_missing_config_keys_raises(self, tmp_path):
        """A YAML without the API URLs raises a clear ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("products: [InputData]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="data_url"):
            Catalog.load(catalog_path=bad)

    def test_malformed_row_raises(self, tmp_path):
        """An unknown field in a curated row fails validation with the key named."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "data_url: http://d\nfiles_api_url: http://f\n"
            "variables:\n  pr:\n    bogus: 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Variable 'pr' failed validation"):
            Catalog.load(catalog_path=bad)

    def test_catalog_path_override_and_cache(self, tmp_path, monkeypatch):
        """A monkeypatched CATALOG_PATH loads a custom minimal catalog."""
        custom = tmp_path / "isimip_data_catalog.yaml"
        custom.write_text(
            "data_url: http://d\nfiles_api_url: http://f\n"
            "products: [InputData]\ntime_steps: [daily]\n"
            "variables:\n  pr:\n    units: kg m-2 s-1\n    long_name: Precip\n"
            "rounds:\n  ISIMIP3b:\n    description: x\n    default_license: CC0 1.0\n"
            "forcings:\n  gfdl-esm4:\n    round: ISIMIP3b\n"
            "scenarios:\n  ssp585:\n    round: ISIMIP3b\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", custom)
        clear_catalog_cache()
        cat = Catalog()
        assert cat.data_url == "http://d", cat.data_url
        assert list(cat.datasets) == ["pr"], cat.datasets
