"""Unit tests for `earthlens.cli.refresh` (network mocked, writes to tmp)."""

from __future__ import annotations

import gzip
import importlib
import json
import shutil

import pytest
import yaml

import earthlens.stac.catalog as stac_catalog
from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.refresh import (
    AuditOutcome,
    RefreshOutcome,
    _curated_collection_ids,
    _diff,
    _flatten,
    _replace_index_block,
    audit_one,
    refresh_one,
    supported_providers,
)

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestDiff:
    """Tests for _diff."""

    def test_new_and_removed(self):
        """Live-only ids are 'new', bundled-only ids are 'removed'."""
        assert _diff(["a", "b", "c"], ["a", "b", "x"]) == (3, 3, ["c"], ["x"])

    def test_dedupes_live(self):
        """Duplicate live ids are counted once."""
        assert _diff(["a", "a", "b"], ["a"]) == (2, 1, ["b"], [])


class TestFlatten:
    """Tests for _flatten."""

    def test_unions_and_sorts(self):
        """Grouped ids flatten to a sorted, de-duplicated list."""
        assert _flatten({"a": ["x", "y"], "b": ["y", "z"]}) == ["x", "y", "z"]


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_public_providers_supported(self):
        """The wired-up providers all appear."""
        assert {
            "stac",
            "ecmwf",
            "openeo",
            "hdx",
            "earthdata",
            "openaq",
            "cmems",
            "eumetsat",
            "sentinel_hub",
            "gee",
            "worldpop",
            "usgs_water",
            "radar",
            "firms",
            "fdsn",
            "overture",
        } <= set(supported_providers())

    def test_sixteen_refreshable_providers(self):
        """Every provider with a faithful live listing has refresh/audit."""
        assert len(supported_providers()) == 16, sorted(supported_providers())


class TestEcmwfRefresher:
    """Tests for the ECMWF (CDS catalogue) lister."""

    def test_lists_cds_collection_ids(self, monkeypatch):
        """ecmwf refresh reads the public CDS catalogue collection ids."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {"collections": [{"id": "reanalysis-era5-land"}]},
        )
        outcome = refresh_one(_info("ecmwf"))
        assert outcome.status == "ok", "ecmwf refresh ran"
        assert outcome.live_count == 1, "one CDS dataset id listed"


class TestOpeneoRefresher:
    """Tests for the openEO lister."""

    def test_lists_collection_ids(self, monkeypatch):
        """openeo refresh reads the public /collections id list."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "SENTINEL2_L2A"}, {"id": "S1_GRD"}]},
        )
        outcome = refresh_one(_info("openeo"))
        assert outcome.status == "ok", "openeo refresh ran"
        assert outcome.live_count == 2, "two collection ids listed"


class TestHdxRefresher:
    """Tests for the HDX (CKAN) lister."""

    def test_lists_package_names(self, monkeypatch):
        """hdx refresh reads CKAN package_list and audits by hdx_id."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"result": ["kontur-boundaries", "kontur-population"]},
        )
        outcome = refresh_one(_info("hdx"))
        assert outcome.status == "ok", "hdx refresh ran"
        assert outcome.live_count == 2, "two package names listed"


class TestEarthdataRefresher:
    """Tests for the earthdata (CMR) lister."""

    def test_walks_providers_and_paginates(self, monkeypatch):
        """Each provider's CMR pages are gathered into the short-name set."""
        pages = {None: (["A", "B"], "cursor"), "cursor": (["C"], None)}
        monkeypatch.setattr(
            refresh_mod, "_cmr_page", lambda provider, after: pages[after]
        )
        outcome = refresh_one(_info("earthdata"))
        assert outcome.status == "ok", "earthdata refresh ran"
        assert outcome.live_count == 3, "A/B/C gathered across two pages"


class TestOpenaqRefresher:
    """Tests for the OpenAQ lister."""

    def test_lists_parameter_names(self, monkeypatch):
        """openaq refresh reads the v3 /parameters name list."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {"results": [{"name": "pm25"}, {"name": "o3"}]},
        )
        outcome = refresh_one(_info("openaq"))
        assert outcome.status == "ok", "openaq refresh ran"
        assert outcome.live_count == 2, "two parameter names listed"

    def test_audit_no_untracked_when_curated_covers_live(self, monkeypatch):
        """A provider whose index lives elsewhere reports no false untracked."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {"results": [{"name": "pm25"}]},
        )
        outcome = audit_one(_info("openaq"))
        assert outcome.status == "ok", "audit ran"
        assert "pm25" not in outcome.untracked, "curated live id is not untracked"


class TestCmemsRefresher:
    """Tests for the CMEMS (copernicusmarine) lister."""

    def test_walks_products_and_datasets(self, monkeypatch):
        """cmems refresh flattens products[].datasets[].dataset_id."""
        from types import SimpleNamespace

        fake = SimpleNamespace(
            products=[
                SimpleNamespace(datasets=[SimpleNamespace(dataset_id="a")]),
                SimpleNamespace(
                    datasets=[
                        SimpleNamespace(dataset_id="b"),
                        SimpleNamespace(dataset_id="c"),
                    ]
                ),
            ]
        )
        monkeypatch.setattr(refresh_mod, "_cmems_describe", lambda: fake)
        outcome = refresh_one(_info("cmems"))
        assert outcome.status == "ok", "cmems refresh ran"
        assert outcome.live_count == 3, "a/b/c across two products"


class TestEumetsatRefresher:
    """Tests for the EUMETSAT (public browse) lister."""

    def test_lists_collection_ids_from_links(self, monkeypatch):
        """eumetsat refresh reads each browse link's title as the id."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {
                "links": [{"title": "EO:EUM:DAT:1"}, {"title": "EO:EUM:DAT:2"}]
            },
        )
        outcome = refresh_one(_info("eumetsat"))
        assert outcome.status == "ok", "eumetsat refresh ran"
        assert outcome.live_count == 2, "two collection ids listed"


class TestSentinelHubRefresher:
    """Tests for the Sentinel Hub (SDK enum) lister."""

    def test_lists_data_collection_names(self, monkeypatch):
        """sentinel_hub refresh reads the DataCollection enum names."""
        monkeypatch.setattr(
            refresh_mod, "_sh_data_collection_names", lambda: ["SENTINEL2_L2A", "DEM"]
        )
        outcome = refresh_one(_info("sentinel_hub"))
        assert outcome.status == "ok", "sentinel_hub refresh ran"
        assert outcome.live_count == 2, "two collection names listed"


class TestGeeRefresher:
    """Tests for the GEE (EE STAC walk) lister."""

    def test_fetches_ids_for_each_dataset_href(self, monkeypatch):
        """gee refresh walks the tree then fetches each dataset doc's id."""
        monkeypatch.setattr(
            refresh_mod, "_gee_dataset_hrefs", lambda: ["h/a", "h/b", "h/c"]
        )
        monkeypatch.setattr(
            refresh_mod, "_gee_fetch_id", lambda href: href.rsplit("/", 1)[1].upper()
        )
        outcome = refresh_one(_info("gee"))
        assert outcome.status == "ok", "gee refresh ran"
        assert outcome.live_count == 3, "A/B/C ids fetched"


class TestWorldpopRefresher:
    """Tests for the WorldPop (REST sub-alias crawl) lister."""

    def test_crawls_aliases_to_subaliases(self, monkeypatch):
        """worldpop refresh crawls top aliases then each alias's sub-aliases."""

        def fake(url, **kw):
            if url.endswith("/rest/data"):
                return {"data": [{"alias": "pop"}, {"alias": "births"}]}
            return {"data": [{"alias": "wpgp"}, {"alias": "G2_BUILT_S"}]}

        monkeypatch.setattr(refresh_mod, "_get_json", fake)
        outcome = refresh_one(_info("worldpop"))
        assert outcome.status == "ok", "worldpop refresh ran"
        assert outcome.live_count == 2, "deduped sub-alias ids across aliases"


class TestUsgsWaterRefresher:
    """Tests for the USGS Water (dataretrieval) lister."""

    def test_lists_parameter_codes(self, monkeypatch):
        """usgs_water refresh reads the reference-table parameter codes."""
        monkeypatch.setattr(
            refresh_mod, "_usgs_parameter_codes", lambda: ["00060", "00065", "00060"]
        )
        outcome = refresh_one(_info("usgs_water"))
        assert outcome.status == "ok", "usgs_water refresh ran"
        assert outcome.live_count == 2, "deduped codes"

    def test_audit_curated_codes_not_broken(self, monkeypatch):
        """Curated codes present live are not flagged broken."""
        monkeypatch.setattr(
            refresh_mod, "_usgs_parameter_codes", lambda: ["00060", "00065", "00010"]
        )
        outcome = audit_one(_info("usgs_water"))
        assert "00060" not in outcome.broken, "a live curated code is not broken"


_RADAR_TABLE = (
    "NCDCID   ICAO  NAME            ST\n"
    "-------- ----- --------------- --\n"
    "10000001 KABR  ABERDEEN        SD\n"
    "10000002 PAEC  NOME            AK\n"
    "10000003 xx    BAD ROW         ZZ\n"
)


class TestRadarRefresher:
    """Tests for the radar (NOAA HOMR) lister."""

    def test_parses_icao_ids(self):
        """Only four-letter alphabetic ICAO ids are parsed from the table."""
        assert refresh_mod._radar_station_ids(_RADAR_TABLE) == ["KABR", "PAEC"]

    def test_refresh_diffs_against_curated_stations(self, monkeypatch):
        """radar has no available_* block, so live diffs vs curated stations."""
        monkeypatch.setattr(refresh_mod, "_get_text", lambda url: _RADAR_TABLE)
        outcome = refresh_one(_info("radar"))
        assert outcome.status == "ok", "radar refresh ran"
        assert outcome.live_count == 2, "two ICAO ids parsed"
        assert outcome.bundled_count > 2, "diffed against the curated station set"


class TestFirmsRefresher:
    """Tests for the FIRMS (data_availability) lister."""

    def test_lists_sensor_ids_excluding_burned_area(self, monkeypatch):
        """firms refresh parses data_id and drops the burned-area products."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_text",
            lambda url: "data_id,min_date,max_date\n"
            "VIIRS_SNPP_NRT,2020,2026\nBA_MODIS,2000,2026\nMODIS_NRT,2019,2026\n",
        )
        outcome = refresh_one(_info("firms"))
        assert outcome.status == "ok", "firms refresh ran"
        assert outcome.live_count == 2, "BA_MODIS excluded"

    def test_non_csv_body_is_error(self, monkeypatch):
        """A non-CSV body (bad key / quota) reports 'error', not raised."""
        monkeypatch.setattr(refresh_mod, "_get_text", lambda url: "Invalid MAP_KEY")
        assert refresh_one(_info("firms")).status == "error", "bad body captured"


class TestFdsnRefresher:
    """Tests for the FDSN (obspy URL_MAPPINGS) lister."""

    def test_diffs_obspy_providers_against_curated(self, monkeypatch):
        """fdsn live providers diff against the curated fdsn_id set."""
        monkeypatch.setattr(
            refresh_mod, "_fdsn_provider_ids", lambda: ["USGS", "IRIS", "NEWCENTER"]
        )
        outcome = refresh_one(_info("fdsn"))
        assert outcome.status == "ok", "fdsn refresh ran"
        assert outcome.live_count == 3, "three obspy providers listed"
        assert "NEWCENTER" in outcome.new_ids, "an uncurated centre is new"


class TestOvertureRefresher:
    """Tests for the Overture (releases) lister."""

    def test_diffs_releases_not_feature_types(self, monkeypatch):
        """overture diffs the live releases against available_releases."""
        monkeypatch.setattr(
            refresh_mod,
            "_overture_release_ids",
            lambda: ["2099-01-01.0", "2026-05-20.0"],
        )
        outcome = refresh_one(_info("overture"))
        assert outcome.status == "ok", "overture refresh ran"
        assert "2099-01-01.0" in outcome.new_ids, "a new release is flagged"
        assert all("-" in rid for rid in outcome.removed_ids), "diffed vs releases"


@pytest.fixture
def stac_catalog_copy(tmp_path, monkeypatch):
    """Redirect the STAC catalog dir to a writable temp copy.

    Copies the whole sharded catalog (so `load_catalog` still works) and
    points `CATALOG_PATH` at it, so `--write` rewrites the copy and never
    the repo's bundled file.

    Returns:
        The temp catalog directory.
    """
    dst = tmp_path / "catalog"
    shutil.copytree(stac_catalog.CATALOG_PATH, dst)
    monkeypatch.setattr(stac_catalog, "CATALOG_PATH", dst)
    return dst


class TestWrite:
    """Tests for the `--write` (catalog-update) half of refresh_one."""

    def test_rewrites_available_collections_block(self, stac_catalog_copy, monkeypatch):
        """--write rewrites available_collections, preserving endpoints."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "only-one"}], "links": []},
        )
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "ok", "write succeeded"
        assert outcome.written.endswith("_index.yaml"), "the index file was written"
        data = yaml.safe_load((stac_catalog_copy / "_index.yaml").read_text("utf-8"))
        assert list(data["endpoints"]) == [
            "planetary-computer",
            "cdse",
            "earth-search",
        ], "endpoints block preserved"
        assert data["available_collections"]["planetary-computer"] == ["only-one"]

    def test_preserves_header_comment(self, stac_catalog_copy, monkeypatch):
        """The file's leading comment block survives the rewrite."""
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url: {"collections": [], "links": []}
        )
        refresh_one(_info("stac"), write=True)
        text = (stac_catalog_copy / "_index.yaml").read_text("utf-8")
        assert text.startswith("# STAC catalog index"), "header comment kept"

    def test_unsupported_writer_reports_detail(self, monkeypatch):
        """A provider that can read live but not write reports it in detail."""
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url: {"collections": [], "links": []}
        )
        monkeypatch.delitem(refresh_mod._WRITERS, "stac")
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "ok", "the live read still succeeded"
        assert "not supported" in outcome.detail, "write-unsupported noted"

    def test_write_error_is_captured(self, monkeypatch):
        """A write failure reports 'error' rather than raising."""
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url: {"collections": [], "links": []}
        )

        def boom(info, grouped):
            raise OSError("disk full")

        monkeypatch.setitem(refresh_mod._WRITERS, "stac", boom)
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "error", "failure captured"
        assert "write failed" in outcome.detail, "reason preserved"


class TestReplaceIndexBlock:
    """Tests for _replace_index_block."""

    def test_replaces_only_target_block(self, tmp_path):
        """A sibling block and the header comment survive the rewrite."""
        path = tmp_path / "_index.yaml"
        path.write_text(
            "# header\n"
            "available_collections:\n- OLD_A\n- OLD_B\n"
            "available_processes:\n- absolute\n- add\n",
            "utf-8",
        )
        _replace_index_block(path, "available_collections", ["NEW_X"])
        data = yaml.safe_load(path.read_text("utf-8"))
        assert data["available_collections"] == ["NEW_X"], "target replaced"
        assert data["available_processes"] == ["absolute", "add"], "sibling kept"
        assert path.read_text("utf-8").startswith("# header"), "header kept"

    def test_missing_block_raises(self, tmp_path):
        """A file without the block raises ValueError."""
        path = tmp_path / "_index.yaml"
        path.write_text("other: 1\n", "utf-8")
        with pytest.raises(ValueError, match="no available_datasets"):
            _replace_index_block(path, "available_datasets", ["a"])


def _catalog_copy(provider, tmp_path, monkeypatch):
    """Copy a provider's catalog (dir or single file) and repoint CATALOG_PATH."""
    info = _info(provider)
    module = importlib.import_module(f"{info.module}.catalog")
    src = module.CATALOG_PATH
    dst = tmp_path / src.name
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy(src, dst)
    monkeypatch.setattr(module, "CATALOG_PATH", dst)
    module.clear_catalog_cache()
    return info, module, dst


class TestIndexWriters:
    """Tests for the generic `_index_writer` writers (round-trip)."""

    @pytest.mark.parametrize(
        "provider", ["ecmwf", "openeo", "cmems", "eumetsat", "sentinel_hub", "gee"]
    )
    def test_round_trips_real_catalog(self, provider, tmp_path, monkeypatch):
        """Writing a provider's own ids back leaves the loader's index intact.

        Args:
            provider: The catalog-backed provider to round-trip.
        """
        info, module, _dst = _catalog_copy(provider, tmp_path, monkeypatch)
        before = sorted(load_catalog(info).available_datasets)
        path = refresh_mod._WRITERS[provider](info, {provider: before})
        module.clear_catalog_cache()
        after = sorted(load_catalog(info).available_datasets)
        assert after == before, f"{provider} index drifted on round-trip"
        assert path.endswith("_index.yaml"), "wrote the sharded index file"

    def test_openeo_preserves_processes_block(self, tmp_path, monkeypatch):
        """openeo --write rewrites collections without touching processes."""
        info, module, dst = _catalog_copy("openeo", tmp_path, monkeypatch)
        before = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        refresh_mod._WRITERS["openeo"](info, {"openeo": ["ONLY_ONE"]})
        after = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert after["available_collections"] == ["ONLY_ONE"], "collections rewritten"
        assert after["available_processes"] == before["available_processes"], "kept"

    def test_overture_writes_releases_keeps_feature_types(self, tmp_path, monkeypatch):
        """overture --write rewrites available_releases, keeping the type set."""
        info, module, dst = _catalog_copy("overture", tmp_path, monkeypatch)
        before = yaml.safe_load(dst.read_text("utf-8"))
        refresh_mod._WRITERS["overture"](info, {"overture": ["2099-01-01.0"]})
        after = yaml.safe_load(dst.read_text("utf-8"))
        assert after["available_releases"] == ["2099-01-01.0"], "releases rewritten"
        assert after["available_datasets"] == before["available_datasets"], "types kept"


class TestHdxWriter:
    """Tests for the merge-preserving HDX sidecar writer."""

    def test_merges_metadata_and_drops_gone(self, tmp_path, monkeypatch):
        """Surviving ids keep org/title; new ids get bare rows; gone ids drop."""
        info, _module, dst = _catalog_copy("hdx", tmp_path, monkeypatch)
        sidecar = dst / "_available.json.gz"
        with gzip.open(sidecar, "wt", encoding="utf-8") as handle:
            json.dump(
                {
                    "__comment__": "x",
                    "datasets": {
                        "keep": {"org": "O", "title": "T"},
                        "gone": {"org": "g", "title": "g"},
                    },
                },
                handle,
            )
        refresh_mod._WRITERS["hdx"](info, {"hdx": ["keep", "newone"]})
        with gzip.open(sidecar, "rt", encoding="utf-8") as handle:
            rows = json.load(handle)["datasets"]
        assert rows["keep"] == {"org": "O", "title": "T"}, "metadata preserved"
        assert rows["newone"] == {"org": "", "title": ""}, "new id bare"
        assert "gone" not in rows, "id absent upstream dropped"


class TestWriteEcmwfThroughRefreshOne:
    """Tests for refresh_one(write=True) on a generic-writer provider."""

    def test_writes_index_from_live_fetch(self, tmp_path, monkeypatch):
        """ecmwf --write persists the live CDS ids into available_datasets."""
        info, module, dst = _catalog_copy("ecmwf", tmp_path, monkeypatch)
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {"collections": [{"id": "reanalysis-era5-land"}]},
        )
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "write succeeded"
        assert outcome.written.endswith("_index.yaml"), "index file written"
        module.clear_catalog_cache()
        data = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert data["available_datasets"] == ["reanalysis-era5-land"], "live persisted"


class TestCuratedCollectionIds:
    """Tests for _curated_collection_ids."""

    def test_returns_curated_collection_ids(self):
        """Every curated STAC record contributes its upstream collection id."""
        from earthlens.cli.adapter import load_catalog

        ids = _curated_collection_ids(load_catalog(_info("stac")))
        assert ids and all(isinstance(i, str) for i in ids), "non-empty str ids"
        assert ids == sorted(set(ids)), "sorted + de-duplicated"


class TestAuditOne:
    """Tests for audit_one."""

    def test_unsupported_provider(self):
        """A provider with no refresher reports 'unsupported'."""
        assert audit_one(_info("chc")).status == "unsupported"

    def test_reports_broken_and_untracked(self, monkeypatch):
        """Curated ids absent live are 'broken'; live ids off-index 'untracked'."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "only-live"}], "links": []},
        )
        outcome = audit_one(_info("stac"))
        assert outcome.status == "ok", "audit ran"
        assert outcome.broken, "curated collections not in the tiny live set are broken"
        assert outcome.untracked == ["only-live"], "live id absent from index"

    def test_network_error_is_captured(self, monkeypatch):
        """A failed request reports 'error' rather than raising."""

        def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(refresh_mod, "_get_json", boom)
        assert audit_one(_info("stac")).status == "error", "failure captured"


class TestAuditOutcome:
    """Tests for AuditOutcome."""

    def test_to_dict_exposes_broken(self):
        """to_dict carries the broken-drift list."""
        assert AuditOutcome("stac", "ok", broken=["gone"]).to_dict()["broken"] == [
            "gone"
        ]


class TestRefreshOutcome:
    """Tests for RefreshOutcome."""

    def test_to_dict_round_trips_fields(self):
        """to_dict exposes every field for JSON output."""
        outcome = RefreshOutcome("stac", "ok", live_count=3, new_ids=["c"])
        data = outcome.to_dict()
        assert data["status"] == "ok" and data["new_ids"] == ["c"]
        assert data["provider"] == "stac", "provider carried"


class TestRefreshOne:
    """Tests for refresh_one."""

    def test_unsupported_provider(self):
        """A provider with no refresher reports 'unsupported' (no network)."""
        outcome = refresh_one(_info("chc"))
        assert outcome.status == "unsupported", "chc has no live endpoint"

    def test_ok_with_mocked_live_index(self, monkeypatch):
        """A live fetch diffs against the bundled index and reports 'ok'."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {
                "collections": [{"id": "new-x"}, {"id": "new-y"}],
                "links": [],
            },
        )
        outcome = refresh_one(_info("stac"))
        assert outcome.status == "ok", "live fetch succeeded"
        assert outcome.live_count == 2, "two distinct live ids"
        assert set(outcome.new_ids) == {"new-x", "new-y"}, "both absent from bundle"

    def test_pagination_is_followed(self, monkeypatch):
        """`rel=next` links are followed to gather every page."""

        def fake(url):
            if "page2" not in url:
                return {
                    "collections": [{"id": "a"}],
                    "links": [{"rel": "next", "href": url + "?page2"}],
                }
            return {"collections": [{"id": "b"}], "links": []}

        monkeypatch.setattr(refresh_mod, "_get_json", fake)
        outcome = refresh_one(_info("stac"))
        assert {"a", "b"} <= set(outcome.new_ids), "both pages gathered"

    def test_network_error_is_captured(self, monkeypatch):
        """A failed request reports 'error' rather than raising."""

        def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(refresh_mod, "_get_json", boom)
        outcome = refresh_one(_info("stac"))
        assert outcome.status == "error", "failure captured, not raised"
        assert "connection refused" in outcome.detail, "reason preserved"
