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
        assert cat.endpoints["deafrica"].signer == "anonymous"
        assert cat.endpoints["deafrica"].region == "af-south-1"

    def test_all_nine_endpoints_load_together(self):
        """The 9 bundled per-endpoint yaml files merge under the duplicate-key loader."""
        cat = Catalog()
        assert set(cat.endpoints) == {
            "planetary-computer",
            "cdse",
            "earth-search",
            "deafrica",
            "dea",
            "veda",
            "usgs-landsat",
            "bdc",
            "eodc",
        }
        # available_collections holds a per-endpoint live index for each of the 9;
        # source-coop is documentation-only (no endpoint, no collections).
        for ep in cat.endpoints:
            assert ep in cat.available_collections, ep
            assert len(cat.available_collections[ep]) >= 3, ep

    def test_deafrica_collections_curated(self):
        """The deafrica endpoint exposes its curated flagship collections."""
        cat = Catalog()
        wofs = cat.get_collection("deafrica/wofs_ls")
        assert wofs.endpoint == "deafrica"
        assert (
            wofs.signer is None
        )  # no per-collection override → endpoint default (anonymous)
        assert wofs.default_assets == ["water"]
        # the 10 confirmed roadmap collections all load
        for key in (
            "deafrica/wofs_ls",
            "deafrica/wofs_ls_summary_annual",
            "deafrica/fc_ls",
            "deafrica/crop_mask",
            "deafrica/gm_ls8_ls9_annual",
            "deafrica/ls8_sr",
            "deafrica/ls9_sr",
            "deafrica/s2_l2a",
            "deafrica/dem_cop_30",
            "deafrica/dem_cop_90",
        ):
            assert cat.get_collection(key).endpoint == "deafrica", key

    def test_dea_collections_curated(self):
        """The dea endpoint exposes its curated flagship collections."""
        cat = Catalog()
        assert cat.endpoints["dea"].region == "ap-southeast-2"
        assert cat.endpoints["dea"].signer == "anonymous"
        ard = cat.get_collection("dea/ga_ls8c_ard_3")
        assert ard.endpoint == "dea"
        assert ard.signer is None
        assert ard.default_assets == [
            "nbart_red",
            "nbart_green",
            "nbart_blue",
            "nbart_nir",
        ]
        for key in (
            "dea/ga_ls8c_ard_3",
            "dea/ga_ls9c_ard_3",
            "dea/ga_s2am_ard_3",
            "dea/ga_s2bm_ard_3",
            "dea/ga_ls_wo_3",
            "dea/ga_ls_fc_3",
            "dea/ga_ls8c_nbart_gm_cyear_3",
            "dea/ga_s2ls_intertidal_cyear_3",
            "dea/ga_ls_mangrove_cover_cyear_3",
            "dea/ga_srtm_dem1sv1_0",
        ):
            assert cat.get_collection(key).endpoint == "dea", key

    def test_veda_collections_curated(self):
        """The veda endpoint exposes its curated flagship collections."""
        cat = Catalog()
        assert cat.endpoints["veda"].region == "us-west-2"
        assert cat.endpoints["veda"].signer == "anonymous"
        nldas = cat.get_collection("veda/nldas3")
        assert nldas.endpoint == "veda"
        assert nldas.signer is None
        assert nldas.default_assets == ["cog_default"]
        for key in (
            "veda/nldas3",
            "veda/delta-disasters-hd-blackmarble-nightlights",
            "veda/CMIP245-winter-median-pr",
            "veda/CMIP585-winter-median-pr",
            "veda/caldor-fire-burn-severity",
            "veda/hls-ndvi",
            "veda/EPA-annual-emissions-1A-Combustion-Mobile",
        ):
            assert cat.get_collection(key).endpoint == "veda", key

    def test_usgs_landsat_collections_requester_pays(self):
        """USGS LandsatLook collections inherit aws-requester-pays from the endpoint default."""
        cat = Catalog()
        assert cat.endpoints["usgs-landsat"].region == "us-west-2"
        assert cat.endpoints["usgs-landsat"].signer == "aws-requester-pays"
        sr = cat.get_collection("usgs-landsat/landsat-c2l2-sr")
        assert sr.endpoint == "usgs-landsat"
        # explicit per-collection override (matches the endpoint default — kept
        # explicit so the requester-pays intent is visible at the row level).
        assert sr.signer == "aws-requester-pays"
        assert sr.default_assets == ["red", "green", "blue", "nir08"]
        for key in (
            "usgs-landsat/landsat-c2l2-sr",
            "usgs-landsat/landsat-c2l2-st",
            "usgs-landsat/landsat-c2l1",
        ):
            col = cat.get_collection(key)
            assert col.endpoint == "usgs-landsat", key
            assert col.signer == "aws-requester-pays", key

    def test_bdc_collections_curated(self):
        """The bdc endpoint exposes its curated flagship collections (all anonymous today)."""
        cat = Catalog()
        assert cat.endpoints["bdc"].signer == "anonymous"
        ndvi = cat.get_collection("bdc/CBERS4-WFI-16D-2")
        assert ndvi.endpoint == "bdc"
        assert ndvi.signer is None
        assert ndvi.requires_token is False
        for key in (
            "bdc/CBERS4-WFI-16D-2",
            "bdc/CB4A-WFI-L4-SR-1",
            "bdc/CB4A-WPM-L4-DN-1",
            "bdc/AMZ1-WFI-L4-SR-1",
            "bdc/S2_L2A-1",
            "bdc/mod13q1-6.1",
            "bdc/myd13q1-6.1",
        ):
            col = cat.get_collection(key)
            assert col.endpoint == "bdc", key
            # all currently-exposed BDC collections read anonymously
            assert col.signer is None, key
            assert col.requires_token is False, key

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
        assert len(cat.datasets) > 100, (
            f"expected the bulk COG curation, got {len(cat.datasets)}"
        )
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
            "red",
            "green",
            "blue",
            "nir08",
        ]
        # CDSE NDVI: the data band, not the uncertainty/quality/count bands.
        assert cat.get_collection(
            "cdse/clms_ndvi_global_1km_10daily_v3_cog"
        ).default_assets == [
            "ndvi_ndvi",
        ]


@pytest.mark.stac
class TestResolve:
    """`resolve` maps a logical key to the endpoint's actual collection id."""

    def test_alias_applied_for_earth_search(self):
        """Earth Search serves Sentinel-2 L2A under sentinel-2-c1-l2a."""
        assert (
            Catalog().resolve("earth-search", "sentinel-2-l2a") == "sentinel-2-c1-l2a"
        )

    def test_default_id_when_no_alias(self):
        """Without an alias the collection_id (== key here) is returned."""
        assert (
            Catalog().resolve("planetary-computer", "sentinel-2-l2a")
            == "sentinel-2-l2a"
        )

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

    def test_yaml_files_for_single_file(self, tmp_path):
        """A single YAML file path yields just that file."""
        single = _write(tmp_path / "one.yaml", "collections: {}\n")
        assert _yaml_files_for(single) == [single]

    def test_duplicate_endpoint_across_files_raises(self, tmp_path):
        """The same endpoint key in two files is a load error."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n    signer: anonymous\n"
            "collections:\n  c:\n    endpoint: e\n",
        )
        _write(
            tmp_path / "b.yaml",
            "endpoints:\n  e:\n    url: u2\n    signer: anonymous\n",
        )
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_invalid_collection_body_raises(self, tmp_path):
        """A collection with an unknown field is rejected with an 'invalid collection' error."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n    signer: anonymous\n"
            "collections:\n  c:\n    endpoint: e\n    bogus_field: 1\n",
        )
        with pytest.raises(ValueError, match="invalid collection"):
            _load_catalog_data(tmp_path)

    def test_requires_token_round_trips(self, tmp_path):
        """The Collection.requires_token field parses + survives the loader."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\n    signer: anonymous\n"
            "collections:\n  gated:\n    endpoint: e\n    requires_token: true\n"
            "    signer: bdc-token\n",
        )
        _, _, collections = _load_catalog_data(tmp_path)
        col = collections["gated"]
        assert col.requires_token is True, "requires_token field survives the loader"
        assert col.signer == "bdc-token", "bdc-token is a valid SignerType literal"

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same curated collection map as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

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

    def test_resolve_assets_renames_only_the_named_endpoint(self):
        """An endpoint publishing a band under another key gets the renamed one."""
        cat = Catalog(
            endpoints={
                "e": Endpoint(key="e", url="u"),
                "f": Endpoint(key="f", url="u"),
            },
            datasets={
                "x": Collection(endpoint="e", asset_aliases={"f": {"B04": "B04_10m"}})
            },
        )
        assert cat.resolve_assets("f", "x", ["B04"]) == ["B04_10m"]
        assert cat.resolve_assets("e", "x", ["B04"]) == ["B04"], (
            "an endpoint with no asset_aliases entry must pass keys through"
        )
        assert cat.resolve_assets("f", "x", ["SCL"]) == ["SCL"], (
            "an asset the endpoint does not rename passes through"
        )

    def test_cdse_sentinel2_assets_carry_the_resolution_suffix(self):
        """CDSE splits Sentinel-2 per resolution, so B04 is B04_10m there."""
        cat = Catalog()
        assert cat.resolve_assets("cdse", "sentinel-2-l2a", ["B04"]) == ["B04_10m"]
        assert cat.resolve_assets(
            "cdse", "sentinel-2-l2a", ["B02", "B03", "B04", "B08"]
        ) == ["B02_10m", "B03_10m", "B04_10m", "B08_10m"]
        assert cat.resolve_assets("planetary-computer", "sentinel-2-l2a", ["B04"]) == [
            "B04"
        ], "the rename is CDSE-only"

    def test_collection_unknown_endpoint_raises(self, tmp_path):
        """A collection naming an undeclared endpoint is rejected."""
        _write(
            tmp_path / "a.yaml",
            "endpoints:\n  e:\n    url: u\ncollections:\n  c:\n    endpoint: missing\n",
        )
        with pytest.raises(ValueError, match="not declared in"):
            _load_catalog_data(tmp_path)
