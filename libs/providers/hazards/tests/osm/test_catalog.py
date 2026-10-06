"""Unit tests for the OSM named-query catalog."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.osm.catalog import Catalog, Dataset, clear_catalog_cache

pytestmark = pytest.mark.osm


@pytest.fixture
def catalog() -> Catalog:
    """A freshly loaded catalog (cache cleared first)."""
    clear_catalog_cache()
    return Catalog()


class TestDatasetModel:
    """The per-row Dataset model and its protocol validation."""

    def test_live_row_needs_query_template(self):
        """A live row without a query_template fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="live")

    def test_live_row_rejects_ohsome_filter(self):
        """A live row carrying an ohsome_filter fails validation."""
        with pytest.raises(ValidationError):
            Dataset(
                protocol="live",
                query_template="[out:json];({bbox});out geom;",
                ohsome_filter="building=*",
            )

    def test_history_row_needs_filter(self):
        """A history row without an ohsome_filter fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="history")

    def test_history_row_rejects_query_template(self):
        """A history row carrying a query_template fails validation."""
        with pytest.raises(ValidationError):
            Dataset(
                protocol="history",
                ohsome_filter="building=*",
                query_template="[out:json];out geom;",
            )

    def test_unknown_protocol_rejected(self):
        """A protocol outside the Literal is rejected."""
        with pytest.raises(ValidationError):
            Dataset(protocol="wfs", query_template="x")

    def test_bulk_row_needs_method(self):
        """A bulk row without a pyrosm_method fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="bulk")

    def test_bulk_row_rejects_unknown_method(self):
        """A bulk row naming an unknown pyrosm_method fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="bulk", pyrosm_method="get_bogus")

    def test_bulk_row_rejects_query_and_filter_fields(self):
        """A bulk row carrying a query_template or ohsome_filter fails validation."""
        with pytest.raises(ValidationError):
            Dataset(
                protocol="bulk",
                pyrosm_method="get_buildings",
                ohsome_filter="building=*",
            )

    def test_bulk_row_resolves(self):
        """A well-formed bulk row exposes its method and network_type."""
        row = Dataset(
            protocol="bulk", pyrosm_method="get_network", network_type="driving"
        )
        assert row.pyrosm_method == "get_network" and row.network_type == "driving"


class TestCatalog:
    """Loading and resolving the bundled named-query catalog."""

    def test_live_row_resolves(self, catalog):
        """live:hospitals resolves to the live protocol."""
        assert catalog.get("live:hospitals").protocol == "live"

    def test_history_row_carries_filter(self, catalog):
        """A history row exposes its ohsome_filter."""
        assert catalog.get("history:buildings").ohsome_filter

    def test_query_ids_sorted(self, catalog):
        """query_ids returns the registered ids, sorted."""
        ids = catalog.query_ids()
        assert ids == sorted(ids)
        assert "live:roads" in ids and "history:highways" in ids

    def test_dict_surface(self, catalog):
        """The catalog supports membership and len like a mapping."""
        assert "live:hospitals" in catalog
        assert len(catalog) >= 1

    def test_unknown_id_did_you_mean(self, catalog):
        """A near-miss id raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'live:hospitals'"):
            catalog.get("live:hospital")

    def test_every_row_has_protocol_and_query(self, catalog):
        """Catalog integrity: each row carries its protocol's query field."""
        for query_id, row in catalog.datasets.items():
            assert row.protocol in ("live", "history", "bulk")
            if row.protocol == "live":
                assert row.query_template and "{bbox}" in row.query_template
            elif row.protocol == "history":
                assert row.ohsome_filter
            else:
                assert row.pyrosm_method

    def test_id_prefix_matches_protocol(self, catalog):
        """Each id's `<protocol>:` prefix matches the row's protocol."""
        for query_id, row in catalog.datasets.items():
            assert query_id.split(":", 1)[0] == row.protocol

    def test_bulk_row_resolves(self, catalog):
        """bulk:buildings resolves to its get_buildings pyrosm method."""
        assert catalog.get("bulk:buildings").pyrosm_method == "get_buildings"

    def test_prefix_aliases_resolve(self, catalog):
        """The overpass/ohsome/pbf prefix aliases map to live/history/bulk."""
        assert catalog.get("overpass:hospitals").protocol == "live"
        assert catalog.get("ohsome:buildings").protocol == "history"
        assert catalog.get("pbf:buildings").protocol == "bulk"

    def test_alias_prefix_resolves_to_canonical_row(self, catalog):
        """An aliased id resolves to the same row object as its canonical form."""
        assert catalog.get("overpass:hospitals") is catalog.get("live:hospitals")

    def test_region_key_resolves(self, catalog):
        """A region key resolves to its Geofabrik path; a raw path passes through."""
        assert catalog.region_path("malta") == "europe/malta"
        assert catalog.region_path("europe/andorra") == "europe/andorra"

    def test_region_ids_sorted_and_populated(self, catalog):
        """region_ids returns the sorted region keys including the test extract."""
        ids = catalog.region_ids()
        assert ids == sorted(ids) and "malta" in ids

    def test_unknown_region_did_you_mean(self, catalog):
        """An unknown region key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="not a known Geofabrik region"):
            catalog.region_path("maltaa")


class TestCatalogLoad:
    """The disk loader and its error paths."""

    def test_missing_datasets_block_raises(self, tmp_path):
        """A YAML with no datasets: block is rejected."""
        path = tmp_path / "empty.yaml"
        path.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            Catalog.load(path)

    def test_malformed_row_raises(self, tmp_path):
        """A row that fails Dataset validation is reported with its id."""
        path = tmp_path / "bad.yaml"
        path.write_text("datasets:\n  live:x:\n    protocol: live\n", encoding="utf-8")
        with pytest.raises(ValueError, match="live:x"):
            Catalog.load(path)

    def test_missing_file_raises(self, tmp_path):
        """Loading a non-existent catalog path raises (mtime guard included)."""
        clear_catalog_cache()
        with pytest.raises((FileNotFoundError, ValueError)):
            Catalog.load(tmp_path / "does-not-exist.yaml")

    def test_cache_round_trip(self, tmp_path):
        """A second load of the same file is served from the parse cache."""
        path = tmp_path / "ok.yaml"
        path.write_text(
            "datasets:\n  history:b:\n    protocol: history\n    ohsome_filter: building=*\n",
            encoding="utf-8",
        )
        clear_catalog_cache()
        first = Catalog.load(path)
        second = Catalog.load(path)
        assert (
            first.get("history:b").ohsome_filter
            == second.get("history:b").ohsome_filter
        )
