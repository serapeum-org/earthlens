"""Unit tests for the risk-indicators catalog loader and admin lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.risk_indicators import Catalog, Dataset
from earthlens.risk_indicators import catalog as catalog_mod

pytestmark = pytest.mark.risk_indicators


def _write_yaml(tmp_path: Path, body: str) -> Path:
    """Write a catalog YAML to a temp file and clear the parse cache."""
    path = tmp_path / "cat.yaml"
    path.write_text(body, encoding="utf-8")
    catalog_mod.clear_catalog_cache()
    return path


class TestCatalogLoading:
    """The bundled catalog loads its datasets and admin table."""

    def test_lists_datasets_sorted(self):
        """available() returns the shipped dataset ids, sorted."""
        available = Catalog().available()
        assert available == sorted(available)
        assert {
            "thinkhazard:flood_river",
            "inform:risk",
            "gfw:tree_cover_loss",
            "gfw:admin_boundary",
        }.issubset(set(available))

    def test_admin_table_loaded(self):
        """The ISO3 -> ADM0 admin table loads with Kenya present."""
        cat = Catalog()
        assert cat.admin_codes["KEN"] == 133
        assert len(cat.admin_codes) > 200

    def test_eleven_thinkhazard_hazards_plus_all(self):
        """ThinkHazard ships 11 hazard datasets plus the all-hazards row."""
        th = [d for d in Catalog().datasets.values() if d.provider == "thinkhazard"]
        with_hazard = [d for d in th if d.hazard is not None]
        assert len(with_hazard) == 11
        assert any(d.hazard is None for d in th)


class TestGet:
    """get() resolves ids and reports the provider / output kind."""

    def test_thinkhazard_row(self):
        """A ThinkHazard row reports its provider, kind, and hazard."""
        row = Catalog().get("thinkhazard:flood_river")
        assert isinstance(row, Dataset)
        assert row.provider == "thinkhazard"
        assert row.output_kind == "tabular"
        assert row.hazard == "FL"

    def test_gfw_vector_row(self):
        """The GFW geometry dataset is vector output."""
        row = Catalog().get("gfw:admin_boundary")
        assert row.provider == "gfw"
        assert row.output_kind == "vector"
        assert row.gfw_geostore == "admin"

    def test_gfw_tabular_row(self):
        """A GFW tabular row carries dataset/version/sql_template."""
        row = Catalog().get("gfw:tree_cover_loss")
        assert row.output_kind == "tabular"
        assert row.gfw_dataset and row.gfw_version and "{iso}" in row.sql_template

    @pytest.mark.parametrize(
        ("dataset_id", "indicator_id"),
        [
            ("inform:risk", "INFORM"),
            ("inform:hazard_exposure", "HA"),
            ("inform:vulnerability", "VU"),
            ("inform:coping_capacity", "CC"),
        ],
    )
    def test_inform_rows_pin_the_served_workflow(self, dataset_id, indicator_id):
        """Every INFORM Risk row pins workflow 503 and carries its indicator id."""
        row = Catalog().get(dataset_id)
        assert row.workflow_id == 503
        assert row.indicator_id == indicator_id

    def test_climate_risk_pins_the_served_scenario(self):
        """The climate row reads the one Climate Change workflow that holds scores."""
        row = Catalog().get("inform:climate_risk")
        assert row.workflow_id == 451
        assert "RCP4.5-SSP1" in row.long_name

    def test_unknown_id_raises_did_you_mean(self):
        """An unknown but close id raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'inform:risk'"):
            Catalog().get("inform:rsk")


class TestCatalogIntegrity:
    """Every shipped row is well-formed for its provider."""

    def test_every_row_valid(self):
        """Each row has a known provider, a valid output kind, and a citation."""
        for dataset_id, row in Catalog().datasets.items():
            assert row.provider in {"thinkhazard", "inform", "gfw"}, dataset_id
            assert row.output_kind in {"tabular", "vector"}, dataset_id
            assert row.citation, dataset_id


class TestDatasetValidator:
    """The Dataset model enforces per-provider required fields."""

    def test_inform_requires_workflow_and_indicator(self):
        """An INFORM row without workflow_id/indicator_id is rejected."""
        with pytest.raises(ValueError, match="workflow_id and indicator_id"):
            Dataset(id="inform:x", provider="inform", output_kind="tabular")

    def test_tabular_gfw_requires_query_fields(self):
        """A tabular GFW row without dataset/version/sql is rejected."""
        with pytest.raises(ValueError, match="gfw_dataset"):
            Dataset(id="gfw:x", provider="gfw", output_kind="tabular")

    def test_vector_gfw_requires_geostore(self):
        """A vector GFW row without gfw_geostore is rejected."""
        with pytest.raises(ValueError, match="gfw_geostore"):
            Dataset(id="gfw:x", provider="gfw", output_kind="vector")

    def test_thinkhazard_row_needs_no_extra_fields(self):
        """A ThinkHazard row validates with just provider + output_kind."""
        row = Dataset(id="thinkhazard:x", provider="thinkhazard", output_kind="tabular")
        assert row.hazard is None

    @pytest.mark.parametrize("provider", ["thinkhazard", "inform"])
    def test_non_gfw_provider_must_be_tabular(self, provider):
        """A thinkhazard / inform row declared as vector is rejected."""
        kwargs = (
            {"workflow_id": 503, "indicator_id": "INFORM"}
            if provider == "inform"
            else {}
        )
        with pytest.raises(ValueError, match="must be output_kind 'tabular'"):
            Dataset(
                id=f"{provider}:x", provider=provider, output_kind="vector", **kwargs
            )


class TestResolveAdmin:
    """resolve_admin maps an ISO3 to a ThinkHazard ADM0 code."""

    def test_kenya_resolves(self):
        """Kenya resolves to its ADM0 code 133."""
        assert Catalog().resolve_admin("KEN") == "133"

    def test_case_insensitive(self):
        """A lowercase ISO3 resolves the same."""
        assert Catalog().resolve_admin("ken") == "133"

    def test_unknown_iso_raises(self):
        """An unknown ISO3 raises a clear ValueError."""
        with pytest.raises(ValueError, match="not a known ISO3"):
            Catalog().resolve_admin("ZZZ")

    def test_non_country_level_rejected(self):
        """A non-country admin level is rejected (use a raw admin_code)."""
        with pytest.raises(ValueError, match="level=0"):
            Catalog().resolve_admin("KEN", level=1)


class TestLoaderErrors:
    """The loader rejects a malformed or empty catalog file."""

    def test_empty_datasets_block_raises(self, tmp_path):
        """A file with no datasets block is rejected."""
        path = _write_yaml(tmp_path, "admin_codes:\n  KEN: 133\n")
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            Catalog.load(path)

    def test_invalid_row_raises(self, tmp_path):
        """A row with an unknown provider fails validation."""
        path = _write_yaml(
            tmp_path,
            "datasets:\n  bad:\n    provider: nope\n    output_kind: tabular\n",
        )
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(path)

    def test_missing_file_raises(self, tmp_path):
        """Loading a non-existent catalog path raises FileNotFoundError."""
        catalog_mod.clear_catalog_cache()
        with pytest.raises(FileNotFoundError):
            Catalog.load(tmp_path / "does_not_exist.yaml")


class TestCacheControl:
    """The parse cache reloads after an edit and on clear()."""

    def test_clear_cache_forces_reload(self, tmp_path):
        """clear_catalog_cache() drops the cached parse."""
        path = _write_yaml(
            tmp_path,
            "datasets:\n  inform:risk:\n    provider: inform\n    "
            "output_kind: tabular\n    workflow_id: 503\n    indicator_id: INFORM\n",
        )
        first = Catalog.load(path)
        assert "inform:risk" in first.datasets
        catalog_mod.clear_catalog_cache()
        assert "inform:risk" in Catalog.load(path).datasets
