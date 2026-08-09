"""Unit tests for the NSI source catalog."""

from __future__ import annotations

import pytest

from earthlens.nsi.catalog import (
    CATALOG_PATH,
    Catalog,
    Source,
    clear_catalog_cache,
)

pytestmark = pytest.mark.nsi


@pytest.mark.unit
class TestCatalogLoad:
    """Loading and resolving the bundled catalog."""

    def test_available_lists_three_sources(self) -> None:
        """The shipped catalog carries the three flood sources."""
        assert Catalog().available() == ["nfhl", "nfip", "structures"]

    def test_get_resolves_output_kind(self) -> None:
        """Each source declares the expected output kind."""
        cat = Catalog()
        assert cat.get("structures").output_kind == "vector"
        assert cat.get("nfhl").output_kind == "vector"
        assert cat.get("nfip").output_kind == "tabular"

    def test_nfip_carries_records_key_and_page_size(self) -> None:
        """The nfip row pins the envelope key and a page size."""
        nfip = Catalog().get("nfip")
        assert nfip.records_key == "NfipClaims"
        assert nfip.page_size and nfip.page_size > 0

    def test_nfhl_carries_layer(self) -> None:
        """The nfhl row pins the ArcGIS flood-hazard layer."""
        nfhl = Catalog().get("nfhl")
        assert nfhl.layer_id == 28
        assert nfhl.layer_name == "S_Fld_Haz_Ar"

    def test_field_maps_use_pinned_nfip_names(self) -> None:
        """The nfip field map uses floodZoneCurrent / censusGeoid (A1 fix)."""
        provider_fields = set(Catalog().get("nfip").fields.values())
        assert "floodZoneCurrent" in provider_fields
        assert "censusGeoid" in provider_fields
        assert "floodZone" not in provider_fields
        assert "censusTract" not in provider_fields

    def test_unknown_source_hints(self) -> None:
        """An unknown key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="did you mean|structures|NSI catalog"):
            Catalog().get("structure")


@pytest.mark.unit
class TestSourceValidation:
    """The `Source` cross-field validator."""

    def test_nfhl_needs_layer(self) -> None:
        """An nfhl row without a layer is rejected."""
        with pytest.raises(ValueError):
            Source(
                id="nfhl",
                provider="fema-arcgis",
                endpoint="https://x",
                output_kind="vector",
            )

    def test_nfip_needs_records_key(self) -> None:
        """An nfip row without a records key is rejected."""
        with pytest.raises(ValueError):
            Source(
                id="nfip",
                provider="openfema",
                endpoint="https://x",
                output_kind="tabular",
            )

    def test_structures_must_be_vector(self) -> None:
        """A structures row declared tabular is rejected."""
        with pytest.raises(ValueError):
            Source(
                id="structures",
                provider="nsi",
                endpoint="https://x",
                output_kind="tabular",
            )

    def test_nfhl_must_be_vector(self) -> None:
        """An nfhl row with a layer but tabular output is rejected."""
        with pytest.raises(ValueError, match="vector"):
            Source(
                id="nfhl",
                provider="fema-arcgis",
                endpoint="https://x",
                output_kind="tabular",
                layer_id=28,
                layer_name="S_Fld_Haz_Ar",
            )

    def test_nfip_must_be_tabular(self) -> None:
        """An nfip row with a records key but vector output is rejected."""
        with pytest.raises(ValueError, match="tabular"):
            Source(
                id="nfip",
                provider="openfema",
                endpoint="https://x",
                output_kind="vector",
                records_key="NfipClaims",
            )


@pytest.mark.unit
class TestCatalogFromFile:
    """Loading from an explicit path (empty / malformed cases)."""

    def test_empty_sources_block_raises(self, tmp_path) -> None:
        """A YAML with no sources block is rejected."""
        p = tmp_path / "empty.yaml"
        p.write_text("sources:\n", encoding="utf-8")
        with pytest.raises(ValueError, match="sources"):
            Catalog.load(p)

    def test_bad_row_raises(self, tmp_path) -> None:
        """A malformed source row is wrapped in a clear ValueError."""
        p = tmp_path / "bad.yaml"
        p.write_text(
            "sources:\n  x:\n    provider: nsi\n    output_kind: vector\n",
            encoding="utf-8",
        )
        clear_catalog_cache()
        with pytest.raises(ValueError, match="failed validation|endpoint"):
            Catalog.load(p)

    def test_get_catalog_returns_datasets(self) -> None:
        """`get_catalog` returns the same mapping as `datasets`."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets


@pytest.mark.unit
class TestCache:
    """The module-level parse cache."""

    def test_clear_cache_forces_reparse(self) -> None:
        """`clear_catalog_cache` empties the cache without error."""
        Catalog.load()
        clear_catalog_cache()
        assert Catalog.load().available() == ["nfhl", "nfip", "structures"]

    def test_catalog_path_points_at_bundled_yaml(self) -> None:
        """The default catalog path is the shipped YAML."""
        assert CATALOG_PATH.name == "nsi_data_catalog.yaml"
        assert CATALOG_PATH.exists()
