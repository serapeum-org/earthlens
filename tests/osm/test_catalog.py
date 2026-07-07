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

    def test_overpass_row_needs_query_template(self):
        """An overpass row without a query_template fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="overpass")

    def test_overpass_row_rejects_ohsome_filter(self):
        """An overpass row carrying an ohsome_filter fails validation."""
        with pytest.raises(ValidationError):
            Dataset(
                protocol="overpass",
                query_template="[out:json];({bbox});out geom;",
                ohsome_filter="building=*",
            )

    def test_ohsome_row_needs_filter(self):
        """An ohsome row without an ohsome_filter fails validation."""
        with pytest.raises(ValidationError):
            Dataset(protocol="ohsome")

    def test_ohsome_row_rejects_query_template(self):
        """An ohsome row carrying a query_template fails validation."""
        with pytest.raises(ValidationError):
            Dataset(
                protocol="ohsome",
                ohsome_filter="building=*",
                query_template="[out:json];out geom;",
            )

    def test_unknown_protocol_rejected(self):
        """A protocol outside the Literal is rejected."""
        with pytest.raises(ValidationError):
            Dataset(protocol="wfs", query_template="x")


class TestCatalog:
    """Loading and resolving the bundled named-query catalog."""

    def test_overpass_row_resolves(self, catalog):
        """overpass:hospitals resolves to an overpass protocol."""
        assert catalog.get("overpass:hospitals").protocol == "overpass"

    def test_ohsome_row_carries_filter(self, catalog):
        """An ohsome row exposes its ohsome_filter."""
        assert catalog.get("ohsome:buildings").ohsome_filter

    def test_query_ids_sorted(self, catalog):
        """query_ids returns the registered ids, sorted."""
        ids = catalog.query_ids()
        assert ids == sorted(ids)
        assert "overpass:roads" in ids and "ohsome:highways" in ids

    def test_dict_surface(self, catalog):
        """The catalog supports membership and len like a mapping."""
        assert "overpass:hospitals" in catalog
        assert len(catalog) >= 1

    def test_unknown_id_did_you_mean(self, catalog):
        """A near-miss id raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'overpass:hospitals'"):
            catalog.get("overpass:hospital")

    def test_every_row_has_protocol_and_query(self, catalog):
        """Catalog integrity: each row carries its protocol's query field."""
        for query_id, row in catalog.datasets.items():
            assert row.protocol in ("overpass", "ohsome", "pbf")
            if row.protocol == "overpass":
                assert row.query_template and "{bbox}" in row.query_template
            elif row.protocol == "ohsome":
                assert row.ohsome_filter
            else:
                assert row.pyrosm_method

    def test_id_prefix_matches_protocol(self, catalog):
        """Each id's `<protocol>:` prefix matches the row's protocol."""
        for query_id, row in catalog.datasets.items():
            assert query_id.split(":", 1)[0] == row.protocol


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
        path.write_text(
            "datasets:\n  overpass:x:\n    protocol: overpass\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="overpass:x"):
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
            "datasets:\n  ohsome:b:\n    protocol: ohsome\n    ohsome_filter: building=*\n",
            encoding="utf-8",
        )
        clear_catalog_cache()
        first = Catalog.load(path)
        second = Catalog.load(path)
        assert (
            first.get("ohsome:b").ohsome_filter == second.get("ohsome:b").ohsome_filter
        )
