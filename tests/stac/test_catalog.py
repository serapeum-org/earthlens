"""Unit tests for `earthlens.stac.catalog`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.stac.catalog import (
    CATALOG_PATH,
    Catalog,
    Collection,
    Endpoint,
    _load_catalog_data,
    _yaml_files_for,
    clear_catalog_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Reset the module-level parse cache so tmp-file rewrites work."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.stac
class TestBundledCatalog:
    """The bundled `catalog/` directory parses and exposes endpoints + collections."""

    def test_catalog_path_exists(self):
        """CATALOG_PATH points at the shipped catalog directory with an index."""
        assert CATALOG_PATH.is_dir()
        assert (CATALOG_PATH / "_index.yaml").is_file()

    def test_default_construction_loads_collections(self):
        """Catalog() loads the bundled per-endpoint files."""
        assert len(Catalog().datasets) > 0

    def test_endpoints_loaded_with_signers(self):
        """Each endpoint carries its URL and signer type."""
        cat = Catalog()
        assert cat.endpoints["planetary-computer"].signer == "mpc-sas"
        assert cat.endpoints["cdse"].signer == "cdse-s3"
        assert cat.endpoints["earth-search"].signer == "anonymous"

    def test_available_collections_index(self):
        """The informational available_collections index is keyed by endpoint."""
        avail = Catalog().available_collections
        assert "sentinel-2-l2a" in avail["planetary-computer"]

    def test_get_collection_default_assets(self):
        """A curated collection exposes its default asset set."""
        assert Catalog().get_collection("sentinel-2-l2a").default_assets == [
            "B02",
            "B03",
            "B04",
            "B08",
        ]

    def test_collection_is_typed(self):
        """get_collection returns a Collection model."""
        assert isinstance(Catalog().get_collection("sentinel-2-l2a"), Collection)

    def test_bulk_cog_collections_curated(self):
        """The bulk COG-imagery curation populated many endpoint-namespaced entries."""
        cat = Catalog()
        assert len(cat.datasets) > 100, f"expected the bulk COG curation, got {len(cat.datasets)}"
        namespaced = [k for k in cat.datasets if "/" in k]
        assert namespaced, "expected endpoint-namespaced <endpoint>/<id> bulk entries"
        # every namespaced key's endpoint prefix matches its collection's endpoint
        for key in namespaced[:20]:
            assert key.split("/", 1)[0] == cat.get_collection(key).endpoint

    def test_default_assets_prefer_imagery_not_qa(self):
        """default_assets pick spectral bands, not QA/quality/uncertainty assets."""
        cat = Catalog()
        # Landsat: RGB+NIR, not the qa_pixel/qa_radsat the alphabetical pick chose.
        assert cat.get_collection("earth-search/landsat-c2-l2").default_assets == [
            "red", "green", "blue", "nir08",
        ]
        # CDSE NDVI: the data band, not the uncertainty/quality/count bands.
        assert cat.get_collection("cdse/clms_ndvi_global_1km_10daily_v3_cog").default_assets == [
            "ndvi_ndvi",
        ]


@pytest.mark.stac
class TestResolve:
    """`resolve` maps a logical key to the endpoint's actual collection id."""

    def test_alias_applied_for_earth_search(self):
        """Earth Search serves Sentinel-2 L2A under sentinel-2-c1-l2a."""
        assert Catalog().resolve("earth-search", "sentinel-2-l2a") == "sentinel-2-c1-l2a"

    def test_default_id_when_no_alias(self):
        """Without an alias the collection_id (== key here) is returned."""
        assert Catalog().resolve("planetary-computer", "sentinel-2-l2a") == "sentinel-2-l2a"

    def test_unknown_collection_raises(self):
        """Resolving an unknown collection raises ValueError."""
        with pytest.raises(ValueError):
            Catalog().resolve("planetary-computer", "no-such-collection")


@pytest.mark.stac
class TestLookupErrors:
    """Misses surface as ValueError with a did-you-mean hint."""

    def test_get_collection_did_you_mean(self):
        """A near-miss collection key suggests the closest match."""
        with pytest.raises(ValueError, match="sentinel-2-l2a"):
            Catalog().get_collection("sentinel-2-l2")

    def test_get_endpoint_unknown_raises(self):
        """An unknown endpoint key raises ValueError listing the known keys."""
        with pytest.raises(ValueError, match="not a known endpoint"):
            Catalog().get_endpoint("nope")


def _write(path: Path, text: str) -> Path:
    """Write `text` to `path` and return it."""
    path.write_text(text)
    return path


@pytest.mark.stac
class TestLoaderRules:
    """The multi-file loader merges and validates the per-endpoint files."""

    def test_yaml_files_for_directory_sorted(self, tmp_path):
        """A directory yields its *.yaml siblings, sorted."""
        _write(tmp_path / "b.yaml", "collections: {}\n")
        _write(tmp_path / "a.yaml", "collections: {}\n")
        assert [p.name for p in _yaml_files_for(tmp_path)] == ["a.yaml", "b.yaml"]

    def test_missing_path_raises(self, tmp_path):
        """A non-existent path fails loud."""
        with pytest.raises(ValueError, match="does not exist"):
            _yaml_files_for(tmp_path / "nope")

    def test_empty_collections_block_raises(self, tmp_path):
        """A catalog with no collections is rejected."""
        _write(tmp_path / "x.yaml", "endpoints: {}\n")
        with pytest.raises(ValueError, match="collections:"):
            _load_catalog_data(tmp_path)

    def test_duplicate_collection_across_files_raises(self, tmp_path):
        """The same collection key in two files is a load error."""
        body = (
            "endpoints:\n  e:\n    url: u\n    signer: anonymous\n"
            "collections:\n  dup:\n    endpoint: e\n"
        )
        _write(tmp_path / "a.yaml", body)
        _write(tmp_path / "b.yaml", "collections:\n  dup:\n    endpoint: e\n")
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_clear_catalog_cache_runs(self):
        """clear_catalog_cache empties the parse cache without error."""
        _load_catalog_data(CATALOG_PATH)
        clear_catalog_cache()

    def test_invalid_endpoint_signer_raises(self, tmp_path):
        """An endpoint with an unknown signer literal is rejected."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n    signer: bogus\n"
            "collections:\n  c:\n    endpoint: e\n",
        )
        with pytest.raises(ValueError, match="invalid endpoint"):
            _load_catalog_data(tmp_path)

    def test_invalid_asset_field_raises(self, tmp_path):
        """An asset with an unknown field is rejected by extra='forbid'."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n"
            "collections:\n  c:\n    endpoint: e\n    assets:\n"
            "      B04: {nope: 1}\n",
        )
        with pytest.raises(ValueError, match="invalid asset"):
            _load_catalog_data(tmp_path)

    def test_resolve_falls_back_to_logical_key(self):
        """A collection with no collection_id and no alias resolves to its key."""
        cat = Catalog(
            endpoints={"e": Endpoint(key="e", url="u")},
            datasets={"x": Collection(endpoint="e")},
        )
        assert cat.resolve("e", "x") == "x"

    def test_collection_unknown_endpoint_raises(self, tmp_path):
        """A collection naming an undeclared endpoint is rejected."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n"
            "collections:\n  c:\n    endpoint: missing\n",
        )
        with pytest.raises(ValueError, match="not declared in"):
            _load_catalog_data(tmp_path)
