"""Unit tests for `earthlens.cli.refresh` (network mocked, writes to tmp)."""

from __future__ import annotations

import gzip
import importlib
import json
import pathlib
import shutil
from types import SimpleNamespace

import pytest
import yaml

import earthlens.stac.catalog as stac_catalog
from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.refresh import (
    AuditOutcome,
    CoverageOutcome,
    RefreshOutcome,
    _curated_collection_ids,
    _diff,
    _flatten,
    _gee_classify,
    _replace_index_block,
    audit_one,
    coverage_one,
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
            "nwm",
        } <= set(supported_providers())

    def test_chc_is_refreshable(self):
        """CHC's FTP product-tree walk is wired up."""
        assert "chc" in supported_providers()

    def test_nineteen_refreshable_providers(self):
        """Every provider with a refreshable index has refresh/audit (incl. s3, nwm)."""
        assert len(supported_providers()) == 19, sorted(supported_providers())
        assert "s3" in supported_providers(), "s3 regenerates its index from curated"
        assert "nwm" in supported_providers(), "nwm walks its operational bucket"


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


class TestGeeClassify:
    """Tests for the gee curation-coverage classifier (network mocked)."""

    def test_curated_id_is_done(self, monkeypatch):
        """An already-curated asset is bucketed DONE without a fetch."""
        monkeypatch.setattr(
            refresh_mod, "_gee_stac_or_none", lambda aid: pytest.fail("no fetch")
        )
        assert _gee_classify("X/Y", {"X/Y"}) == "DONE"

    def test_bands_with_metadata_are_addressable(self, monkeypatch):
        """An image with a band carrying gee:units is addressable."""
        monkeypatch.setattr(
            refresh_mod,
            "_gee_stac_or_none",
            lambda aid: {"summaries": {"eo:bands": [{"name": "B1", "gee:units": "K"}]}},
        )
        assert _gee_classify("X/Y", set()) == "addressable"

    def test_bare_bands_are_thin(self, monkeypatch):
        """An image whose bands carry no usable metadata is thin."""
        monkeypatch.setattr(
            refresh_mod,
            "_gee_stac_or_none",
            lambda aid: {"summaries": {"eo:bands": [{"name": "B1"}]}},
        )
        assert _gee_classify("X/Y", set()) == "thin"

    def test_feature_collection_is_table(self, monkeypatch):
        """A FeatureCollection is bucketed table (out of raster scope)."""
        monkeypatch.setattr(
            refresh_mod, "_gee_stac_or_none", lambda aid: {"gee:type": "table"}
        )
        assert _gee_classify("X/Y", set()) == "table"

    def test_no_doc_is_missing(self, monkeypatch):
        """An asset with no STAC document is bucketed missing."""
        monkeypatch.setattr(refresh_mod, "_gee_stac_or_none", lambda aid: None)
        assert _gee_classify("X/Y", set()) == "missing"


class TestCoverageOne:
    """Tests for coverage_one (the `audit --coverage` driver)."""

    def test_gee_buckets_available_universe(self, monkeypatch):
        """coverage_one classifies each available id and lists the addressable todo."""
        monkeypatch.setitem(
            refresh_mod._COVERAGE,
            "gee",
            lambda catalog: (
                {"DONE": 1, "addressable": 1, "thin": 1, "table": 0, "missing": 0},
                ["B"],
            ),
        )
        outcome = coverage_one(_info("gee"))
        assert outcome.status == "ok", "gee coverage ran"
        assert outcome.counts["addressable"] == 1 and outcome.todo == ["B"]

    def test_unsupported_provider(self):
        """A provider with no classifier reports unsupported."""
        assert coverage_one(_info("gdacs")).status == "unsupported"

    def test_to_dict_carries_counts(self):
        """CoverageOutcome.to_dict round-trips the buckets."""
        data = CoverageOutcome("gee", "ok", counts={"DONE": 2}).to_dict()
        assert data["counts"] == {"DONE": 2}


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
    "NCDCID   ICAO  NAME            ST LAT      LON\n"
    "-------- ----- --------------- -- -------- ---------\n"
    "10000001 KABR  ABERDEEN        SD 45.4558  -98.4133\n"
    "10000002 PAEC  NOME            AK 64.5114  -165.295\n"
    "10000003 xx    BAD ROW         ZZ 0.0      0.0\n"
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


class TestNwmRefresher:
    """Tests for the NWM (unsigned operational-bucket walk) lister."""

    def test_collapses_ensemble_members_to_base_config(self):
        """A `_mem<N>` member directory collapses to its base config key."""
        assert refresh_mod._nwm_collapse_member("medium_range_mem3") == "medium_range"
        assert refresh_mod._nwm_collapse_member("short_range") == "short_range"

    def test_refresh_diffs_collapsed_live_against_configurations(self, monkeypatch):
        """Live config dirs collapse to the curated namespace before the diff."""
        catalog = load_catalog(_info("nwm"))
        # Express the curated configs live, exploding one ensemble into members
        # and adding the uncurated assimilation-input directory.
        live_dirs = [
            f"{key}_mem1" if cfg.members else key
            for key, cfg in catalog.configurations.items()
        ] + ["usgs_timeslices"]
        monkeypatch.setattr(refresh_mod, "_nwm_live_config_dirs", lambda: live_dirs)
        outcome = refresh_one(_info("nwm"))
        assert outcome.status == "ok", "nwm refresh ran"
        assert (
            outcome.live_count == len(catalog.configurations) + 1
        ), "members collapsed"
        assert outcome.new_ids == ["usgs_timeslices"], "only the uncurated dir is new"
        assert not outcome.removed_ids, "every curated config is still live"

    def test_audit_curated_config_not_broken_uncurated_untracked(self, monkeypatch):
        """A live curated config is not broken; usgs_timeslices is untracked."""
        catalog = load_catalog(_info("nwm"))
        live_dirs = [
            f"{key}_mem1" if cfg.members else key
            for key, cfg in catalog.configurations.items()
        ] + ["usgs_timeslices"]
        monkeypatch.setattr(refresh_mod, "_nwm_live_config_dirs", lambda: live_dirs)
        outcome = audit_one(_info("nwm"))
        assert outcome.status == "ok", "nwm audit ran"
        assert (
            "short_range" not in outcome.broken
        ), "a live curated config is not broken"
        assert "usgs_timeslices" in outcome.untracked, "the uncurated dir is untracked"

    def test_refresh_has_no_writer(self, monkeypatch):
        """nwm's index is derived from curated rows, so --write is a no-op read."""
        monkeypatch.setattr(
            refresh_mod, "_nwm_live_config_dirs", lambda: ["short_range"]
        )
        outcome = refresh_one(_info("nwm"), write=True)
        assert outcome.status == "ok", "nwm refresh ran"
        assert not outcome.written, "no on-disk index block to rewrite"
        assert "live read only" in outcome.detail, "reported as read-only"


class _FakeNwmClient:
    """A minimal in-memory S3 stand-in for the NWM bucket-primitive tests.

    Serves `get_paginator(...).paginate()` from `date_pages` (the
    `nwm.YYYYMMDD/` prefix walk) and `list_objects_v2(...)` from
    `dir_prefixes` (one day's configuration directories).
    """

    def __init__(self, date_pages=None, dir_prefixes=None):
        self._date_pages = date_pages or []
        self._dir_prefixes = dir_prefixes or []

    def get_paginator(self, operation):
        """Return a paginator whose `paginate` yields the canned date pages."""
        assert operation == "list_objects_v2", operation
        return SimpleNamespace(paginate=lambda **kw: iter(self._date_pages))

    def list_objects_v2(self, **kwargs):
        """Return the canned configuration-directory `CommonPrefixes`."""
        return {"CommonPrefixes": self._dir_prefixes}


def _date_page(*days):
    """Build a paginator page of `nwm.<day>/` common prefixes."""
    return {"CommonPrefixes": [{"Prefix": f"nwm.{day}/"} for day in days]}


class TestNwmBucketPrimitives:
    """Tests for the shared NWM bucket primitives in refresh.py."""

    def test_unsigned_client_is_us_east_1_s3(self):
        """`_nwm_unsigned_client` builds an unsigned us-east-1 S3 client offline."""
        client = refresh_mod._nwm_unsigned_client()
        assert client.meta.region_name == "us-east-1", "region pinned to us-east-1"
        assert (
            client.meta.service_model.service_name == "s3"
        ), "an S3 client is returned"

    def test_latest_complete_day_picks_day_before_latest(self):
        """The day before the newest prefix is chosen (newest may be partial)."""
        client = _FakeNwmClient(
            date_pages=[_date_page("20260601", "20260603", "20260602")]
        )
        assert (
            refresh_mod._nwm_latest_complete_day(client) == "nwm.20260602"
        ), "second-newest day selected"

    def test_latest_complete_day_single_day_uses_only_day(self):
        """With a single published day, that day is used as-is."""
        client = _FakeNwmClient(date_pages=[_date_page("20260601")])
        assert refresh_mod._nwm_latest_complete_day(client) == "nwm.20260601"

    def test_latest_complete_day_ignores_non_nwm_prefixes(self):
        """Prefixes that do not start with `nwm.` are skipped."""
        client = _FakeNwmClient(date_pages=[{"CommonPrefixes": [{"Prefix": "index/"}]}])
        with pytest.raises(RuntimeError, match=r"no nwm\.YYYYMMDD"):
            refresh_mod._nwm_latest_complete_day(client)

    def test_config_dirs_parses_and_sorts_directory_names(self):
        """Configuration directory names are parsed from the day's prefixes."""
        client = _FakeNwmClient(
            dir_prefixes=[
                {"Prefix": "nwm.20260602/short_range/"},
                {"Prefix": "nwm.20260602/medium_range_mem1/"},
            ]
        )
        assert refresh_mod._nwm_config_dirs(client, "nwm.20260602") == [
            "medium_range_mem1",
            "short_range",
        ], "directory names parsed and sorted"

    def test_live_config_dirs_composes_the_primitives(self, monkeypatch):
        """`_nwm_live_config_dirs` wires client -> day -> dirs together."""
        monkeypatch.setattr(refresh_mod, "_nwm_unsigned_client", lambda: "CLIENT")
        monkeypatch.setattr(
            refresh_mod,
            "_nwm_latest_complete_day",
            lambda client: "DAY" if client == "CLIENT" else "WRONG",
        )
        monkeypatch.setattr(
            refresh_mod,
            "_nwm_config_dirs",
            lambda client, day: (
                ["a", "b"] if (client, day) == ("CLIENT", "DAY") else []
            ),
        )
        assert refresh_mod._nwm_live_config_dirs() == ["a", "b"], "primitives composed"


class _FakeFTP:
    """A minimal in-memory FTP stand-in for the CHC walk test."""

    def __init__(self, tree):
        self._tree = tree
        self._cwd = ""

    def cwd(self, path):
        self._cwd = "" if path == "/" else path

    def nlst(self):
        return self._tree.get(self._cwd.rstrip("/"), [])


class TestChcRefresher:
    """Tests for the CHC (anonymous-FTP product-tree walk) lister."""

    def test_walk_classifies_product_dirs(self):
        """A dir of data files / year-subdirs is a product dir; others descend."""
        tree = {
            "pub/org/chc/products": ["CHIRPS", "README.txt"],
            "pub/org/chc/products/CHIRPS": ["daily", "monthly"],
            "pub/org/chc/products/CHIRPS/daily": ["1981", "1982", "x.tif"],
            "pub/org/chc/products/CHIRPS/monthly": ["data.nc"],
        }
        found = refresh_mod._chc_walk(_FakeFTP(tree), "pub/org/chc/products", 6)
        assert found == [
            "pub/org/chc/products/CHIRPS/daily/",
            "pub/org/chc/products/CHIRPS/monthly/",
        ], "both leaf product dirs discovered, README skipped"

    def test_refresh_diffs_against_ftp_bases(self, monkeypatch):
        """CHC diffs the live tree against catalog ftp_bases, not the slugs."""
        bases = refresh_mod._chc_ftp_bases(load_catalog(_info("chc")))
        live = bases[:-1] + ["pub/org/chc/products/NEW_PRODUCT/daily/"]
        monkeypatch.setattr(refresh_mod, "_chc_discovered_paths", lambda: live)
        outcome = refresh_one(_info("chc"))
        assert outcome.status == "ok", "chc refresh ran"
        assert outcome.new_ids == [
            "pub/org/chc/products/NEW_PRODUCT/daily/"
        ], "only-on-ftp surfaced as new"
        assert len(outcome.removed_ids) == 1, "the dropped base is only-in-yaml"

    def test_refresh_has_no_writer(self, monkeypatch):
        """CHC's curated-slug index can't be machine-written: live read only."""
        monkeypatch.setattr(refresh_mod, "_chc_discovered_paths", lambda: [])
        outcome = refresh_one(_info("chc"), write=True)
        assert outcome.status == "ok" and "not supported" in outcome.detail


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
            "deafrica",
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
            refresh_mod,
            "_get_json",
            lambda url: {"collections": [{"id": "x"}], "links": []},
        )

        def boom(info, grouped):
            raise OSError("disk full")

        monkeypatch.setitem(refresh_mod._WRITERS, "stac", boom)
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "error", "failure captured"
        assert "write failed" in outcome.detail, "reason preserved"

    def test_empty_live_fetch_refuses_to_write(self, monkeypatch):
        """An empty live fetch must not overwrite the index; the writer is skipped."""
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url: {"collections": [], "links": []}
        )
        called = {"wrote": False}

        def writer(info, grouped):
            called["wrote"] = True
            return "x"

        monkeypatch.setitem(refresh_mod._WRITERS, "stac", writer)
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "ok", "empty fetch is not an error"
        assert called["wrote"] is False, "writer must not run on an empty fetch"
        assert "refusing to overwrite" in outcome.detail, "skip is reported"


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

    def test_preserves_comment_above_next_block(self, tmp_path):
        """A comment between the replaced block and its sibling is not swallowed."""
        path = tmp_path / "_index.yaml"
        path.write_text(
            "available_collections:\n- OLD\n"
            "\n# processes follow\navailable_processes:\n- add\n",
            "utf-8",
        )
        _replace_index_block(path, "available_collections", ["NEW"])
        text = path.read_text("utf-8")
        assert "# processes follow" in text, "inter-block comment preserved"
        data = yaml.safe_load(text)
        assert data["available_collections"] == ["NEW"]
        assert data["available_processes"] == ["add"], "sibling intact"


class TestRedact:
    """Tests for _redact (credential scrubbing in error messages)."""

    def test_masks_the_secret(self):
        """An occurrence of the secret is replaced with ***."""
        from earthlens.cli.refresh import _redact

        assert _redact("for url: .../csv/SEKRET/all", "SEKRET") == (
            "for url: .../csv/***/all"
        )

    def test_empty_secret_is_noop(self):
        """An empty secret leaves the text unchanged."""
        from earthlens.cli.refresh import _redact

        assert _redact("nothing to hide", "") == "nothing to hide"


class TestFirmsKeyRedaction:
    """The FIRMS map key must never appear in a surfaced refresh error."""

    def test_refresh_firms_error_scrubs_key(self, monkeypatch):
        """A FIRMS HTTP error (URL holds the key) is reported with the key masked."""
        monkeypatch.setenv("FIRMS_MAP_KEY", "TOPSECRETKEY")

        def boom(url):
            raise RuntimeError(f"404 Client Error for url: {url}")

        monkeypatch.setattr(refresh_mod, "_get_text", boom)
        outcome = refresh_one(_info("firms"))
        assert outcome.status == "error", "error captured"
        assert "TOPSECRETKEY" not in outcome.detail, "map key scrubbed from detail"


class TestRadarMissingColumns:
    """_radar_station_rows degrades cleanly when a required column is absent."""

    def test_missing_name_column_returns_empty(self):
        """A HOMR header without NAME yields {} instead of raising KeyError."""
        text = "ICAO  LAT      LON\n----- -------- --------\nKABR  45.0     -98.0"
        assert refresh_mod._radar_station_rows(text) == {}

    def test_absent_st_column_defaults_state_blank(self):
        """A table with the required columns but no ST keeps the row, state=''."""
        text = (
            "ICAO  NAME       LAT      LON\n"
            "----- ---------- -------- --------\n"
            "KABR  ABERDEEN   45.0     -98.0"
        )
        rows = refresh_mod._radar_station_rows(text)
        assert rows["KABR"]["state"] == "", "absent ST column -> empty state"


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
        "provider", ["ecmwf", "cmems", "eumetsat", "sentinel_hub", "gee"]
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

    def test_openeo_writes_both_collections_and_processes(self, tmp_path, monkeypatch):
        """openeo --write rewrites available_collections AND available_processes."""
        info, module, dst = _catalog_copy("openeo", tmp_path, monkeypatch)
        monkeypatch.setattr(
            refresh_mod, "_openeo_process_ids", lambda: ["load_collection", "ndvi"]
        )
        refresh_mod._WRITERS["openeo"](info, {"openeo": ["ONLY_ONE"]})
        after = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert after["available_collections"] == ["ONLY_ONE"], "collections rewritten"
        assert after["available_processes"] == ["load_collection", "ndvi"], "procs too"

    def test_radar_regenerates_stations_block(self, tmp_path, monkeypatch):
        """radar --write re-parses HOMR into the full curated stations: block."""
        info, module, dst = _catalog_copy("radar", tmp_path, monkeypatch)
        homr = (
            "NCDCID   ICAO  NAME            ST LAT      LON\n"
            "-------- ----- --------------- -- -------- ---------\n"
            "10000001 KABR  ABERDEEN        SD 45.4558  -98.4133\n"
            "10000002 PAEC  NOME            AK 64.5114  -165.295\n"
        )
        monkeypatch.setattr(refresh_mod, "_get_text", lambda url: homr)
        refresh_mod._WRITERS["radar"](info, {"radar": []})
        module.clear_catalog_cache()
        catalog = load_catalog(info)
        assert sorted(catalog.datasets) == ["KABR", "PAEC"], "stations regenerated"
        assert catalog.datasets["KABR"].name == "Aberdeen", "row fields parsed"
        assert catalog.datasets["KABR"].latitude == 45.4558, "latitude parsed"

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


class TestS3IndexRegen:
    """Tests for s3 refresh --write (regenerate available_datasets from curated)."""

    def test_write_regenerates_index_from_curated(self, tmp_path, monkeypatch):
        """s3 --write rewrites available_datasets to the sorted curated names."""
        info, module, dst = _catalog_copy("s3", tmp_path, monkeypatch)
        before = sorted(load_catalog(info).datasets)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "s3 refresh ran"
        assert outcome.written.endswith("s3_data_catalog.yaml"), "in-file index written"
        module.clear_catalog_cache()
        assert sorted(load_catalog(info).available_datasets) == before, "index==curated"


class TestGhslTileRegen:
    """Tests for refresh_ghsl_tiles (GIS tile-grid regeneration)."""

    def test_writes_tile_geojson(self, tmp_path, monkeypatch):
        """The tile frame is written to TILE_SCHEMA_PATH as GeoJSON."""
        import geopandas as gpd
        from shapely.geometry import box

        import earthlens.ghsl._helpers as ghsl_helpers

        frame = gpd.GeoDataFrame(
            {
                "tile_id": ["R1_C1"],
                "left": [0],
                "top": [1],
                "right": [1],
                "bottom": [0],
                "geometry": [box(0, 0, 1, 1)],
            },
            crs="ESRI:54009",
        )
        monkeypatch.setattr(refresh_mod, "_ghsl_tile_frame", lambda: frame)
        dest = tmp_path / "tile_schema.geojson"
        monkeypatch.setattr(ghsl_helpers, "TILE_SCHEMA_PATH", dest)
        path, count = refresh_mod.refresh_ghsl_tiles()
        assert count == 1 and dest.exists(), "tile geojson written"
        assert path.endswith("tile_schema.geojson"), "wrote the bundled tile index"


class TestComputedIndexWriters:
    """Tests for the sibling-index writers (openaq / worldpop / usgs_water)."""

    def test_worldpop_writes_available_products_sibling(self, tmp_path, monkeypatch):
        """worldpop --write persists the grouped crawl to a sibling YAML."""
        info, module, dst = _catalog_copy("worldpop", tmp_path, monkeypatch)
        path = refresh_mod._WRITERS["worldpop"](info, {"pop": ["wpgp", "wpgp1km"]})
        data = yaml.safe_load(pathlib.Path(path).read_text("utf-8"))
        assert pathlib.Path(path).name == "available_products.yaml", "sibling written"
        assert data["available_products"]["pop"] == ["wpgp", "wpgp1km"], "crawl kept"

    def test_openaq_writes_available_parameters_sibling(self, tmp_path, monkeypatch):
        """openaq --write persists the flat live parameter list to a sibling."""
        info, module, dst = _catalog_copy("openaq", tmp_path, monkeypatch)
        path = refresh_mod._WRITERS["openaq"](info, {"openaq": ["o3", "pm25"]})
        data = yaml.safe_load(pathlib.Path(path).read_text("utf-8"))
        assert data["available_parameters"] == ["o3", "pm25"], "flat list written"

    def test_usgs_water_writes_parameter_table_sibling(self, tmp_path, monkeypatch):
        """usgs_water --write persists the full reference table to a sibling."""
        info, module, dst = _catalog_copy("usgs_water", tmp_path, monkeypatch)
        monkeypatch.setattr(
            refresh_mod,
            "_usgs_parameter_rows",
            lambda: {"00060": {"name": "Discharge", "group": "PHY", "unit": "ft3/s"}},
        )
        path = refresh_mod._WRITERS["usgs_water"](info, {"usgs_water": ["00060"]})
        data = yaml.safe_load(pathlib.Path(path).read_text("utf-8"))
        assert data["available_parameters"]["00060"]["unit"] == "ft3/s", "table written"

    def test_refresh_one_write_reports_sibling_path(self, tmp_path, monkeypatch):
        """refresh_one(write=True) returns the sibling path for openaq."""
        info, module, dst = _catalog_copy("openaq", tmp_path, monkeypatch)
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url, **kw: {"results": [{"name": "pm25"}]}
        )
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "openaq write ran"
        assert outcome.written.endswith("available_parameters.yaml"), "sibling path"


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
        assert audit_one(_info("gdacs")).status == "unsupported"

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
        outcome = refresh_one(_info("gdacs"))
        assert outcome.status == "unsupported", "gdacs has no live endpoint"

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


class TestGeeDatasetHrefs:
    """Tests for the EE STAC tree walk."""

    def test_bfs_collects_dataset_hrefs(self, monkeypatch):
        """The walk recurses sub-catalogs and collects dataset doc hrefs."""
        tree = {
            refresh_mod._GEE_STAC_ROOT: {
                "links": [
                    {"rel": "child", "href": "https://x/sub/catalog.json"},
                    {"rel": "child", "href": "https://x/ds_a.json"},
                    {"rel": "self", "href": "ignored"},
                    {"rel": "child"},
                ]
            },
            "https://x/sub/catalog.json": {
                "links": [{"rel": "child", "href": "https://x/ds_b.json"}]
            },
        }

        def fake_get(url):
            if url == "https://x/unreachable":
                raise RuntimeError("boom")
            return tree[url]

        monkeypatch.setattr(refresh_mod, "_get_json", fake_get)
        hrefs = refresh_mod._gee_dataset_hrefs()
        assert set(hrefs) == {"https://x/ds_a.json", "https://x/ds_b.json"}, hrefs

    def test_unreachable_subcatalog_skipped(self, monkeypatch):
        """An unreachable sub-catalog is skipped rather than raising."""

        def fake_get(url):
            raise RuntimeError("offline")

        monkeypatch.setattr(refresh_mod, "_get_json", fake_get)
        assert refresh_mod._gee_dataset_hrefs() == [], "all unreachable -> []"


class TestCmrPage:
    """Tests for the Earthdata CMR pagination helper."""

    def test_reads_short_names_and_cursor(self, monkeypatch):
        """A CMR page yields its ShortNames and the next search-after cursor."""
        import types

        def fake_get(url, params=None, headers=None, timeout=None):
            return types.SimpleNamespace(
                json=lambda: {"items": [{"umm": {"ShortName": "GPM"}}, {"umm": {}}]},
                headers={"CMR-Search-After": "cursor2"},
                raise_for_status=lambda: None,
            )

        monkeypatch.setattr(refresh_mod.requests, "get", fake_get)
        names, cursor = refresh_mod._cmr_page("GES_DISC", None)
        assert names == ["GPM"], "only items with a ShortName are kept"
        assert cursor == "cursor2", "next cursor carried"


class TestUsgsParameterTable:
    """Tests for the USGS reference-table parsers (dataretrieval mocked)."""

    def _patch_frame(self, monkeypatch, rows):
        """Patch dataretrieval to return a tiny pandas frame of `rows`."""
        import pandas as pd
        from dataretrieval import waterdata

        monkeypatch.setattr(
            waterdata, "get_reference_table", lambda collection=None: pd.DataFrame(rows)
        )

    def test_codes_listed(self, monkeypatch):
        """_usgs_parameter_codes returns every parameter_code as a string."""
        self._patch_frame(monkeypatch, [{"parameter_code": 60}, {"parameter_code": 10}])
        assert refresh_mod._usgs_parameter_codes() == ["60", "10"], "codes stringified"

    def test_rows_keyed_by_code(self, monkeypatch):
        """_usgs_parameter_rows keys name/group/unit by the parameter code."""
        self._patch_frame(
            monkeypatch,
            [
                {
                    "parameter_code": "00060",
                    "parameter_name": "Discharge",
                    "parameter_group_code": "PHY",
                    "unit_of_measure": "ft3/s",
                }
            ],
        )
        rows = refresh_mod._usgs_parameter_rows()
        assert rows["00060"]["name"] == "Discharge", "name parsed"
        assert rows["00060"]["unit"] == "ft3/s", "unit parsed"

    def test_blank_codes_skipped(self, monkeypatch):
        """A row with no usable code is dropped."""
        self._patch_frame(
            monkeypatch, [{"parameter_code": ""}, {"parameter_code": "1"}]
        )
        assert list(refresh_mod._usgs_parameter_rows()) == ["1"], "blank code dropped"
