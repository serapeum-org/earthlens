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
import earthlens.stac.cli as stac_cli
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

    def test_refreshable_providers(self):
        """Every provider with a refreshable index has refresh/audit (incl. s3, nwm, jaxa, erddap, cluster)."""
        assert len(supported_providers()) == 26, sorted(supported_providers())
        assert "caravan" in supported_providers(), (
            "caravan watches its pinned Zenodo records for newer releases"
        )
        assert "s3" in supported_providers(), "s3 regenerates its index from curated"
        assert "nwm" in supported_providers(), "nwm walks its operational bucket"
        assert "jaxa" in supported_providers(), "jaxa lists both SDK universes"
        assert "erddap" in supported_providers(), (
            "erddap crawls each server's allDatasets"
        )
        for key in ("gbif", "obis", "wdpa", "iucn"):
            assert key in supported_providers(), f"{key} cluster backend is wired up"


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

    def test_ecmwf_reports_done_and_addressable_across_stores(self):
        """ecmwf coverage buckets the 3-store universe into DONE vs addressable."""
        outcome = coverage_one(_info("ecmwf"))
        assert outcome.status == "ok", "ecmwf coverage is supported"
        assert outcome.counts["DONE"] > 0, "curated rows are DONE"
        assert outcome.counts["addressable"] > 0, "uncurated ids are addressable"
        # a curated ADS row is DONE, not in the addressable todo
        assert "cams-global-reanalysis-eac4" not in outcome.todo

    def test_unsupported_provider(self):
        """A provider with no classifier reports unsupported."""
        assert coverage_one(_info("gdacs")).status == "unsupported"

    def test_to_dict_carries_counts(self):
        """CoverageOutcome.to_dict round-trips the buckets."""
        data = CoverageOutcome("gee", "ok", counts={"DONE": 2}).to_dict()
        assert data["counts"] == {"DONE": 2}


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
        assert outcome.new_ids == ["pub/org/chc/products/NEW_PRODUCT/daily/"], (
            "only-on-ftp surfaced as new"
        )
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
            stac_cli,
            "get_json",
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
            "dea",
            "veda",
            "usgs-landsat",
            "bdc",
            "eodc",
        ], "endpoints block preserved"
        assert data["available_collections"]["planetary-computer"] == ["only-one"]

    def test_preserves_header_comment(self, stac_catalog_copy, monkeypatch):
        """The file's leading comment block survives the rewrite."""
        monkeypatch.setattr(
            stac_cli, "get_json", lambda url: {"collections": [], "links": []}
        )
        refresh_one(_info("stac"), write=True)
        text = (stac_catalog_copy / "_index.yaml").read_text("utf-8")
        assert text.startswith("# STAC catalog index"), "header comment kept"

    def test_unsupported_writer_reports_detail(self, monkeypatch):
        """A provider that can read live but not write reports it in detail."""
        monkeypatch.setattr(
            stac_cli, "get_json", lambda url: {"collections": [], "links": []}
        )
        monkeypatch.delitem(refresh_mod._WRITERS, "stac")
        outcome = refresh_one(_info("stac"), write=True)
        assert outcome.status == "ok", "the live read still succeeded"
        assert "not supported" in outcome.detail, "write-unsupported noted"

    def test_write_error_is_captured(self, monkeypatch):
        """A write failure reports 'error' rather than raising."""
        monkeypatch.setattr(
            stac_cli,
            "get_json",
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
            stac_cli, "get_json", lambda url: {"collections": [], "links": []}
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


class TestComputedIndexWriters:
    """Tests for the sibling-index writers (openaq / worldpop / usgs_water)."""

    def test_openaq_writes_available_parameters_sibling(self, tmp_path, monkeypatch):
        """openaq --write persists the flat live parameter list to a sibling."""
        info, module, dst = _catalog_copy("openaq", tmp_path, monkeypatch)
        path = refresh_mod._WRITERS["openaq"](info, {"openaq": ["o3", "pm25"]})
        data = yaml.safe_load(pathlib.Path(path).read_text("utf-8"))
        assert data["available_parameters"] == ["o3", "pm25"], "flat list written"

    def test_refresh_one_write_reports_sibling_path(self, tmp_path, monkeypatch):
        """refresh_one(write=True) returns the sibling path for openaq."""
        info, module, dst = _catalog_copy("openaq", tmp_path, monkeypatch)
        monkeypatch.setattr(
            refresh_mod, "_get_json", lambda url, **kw: {"results": [{"name": "pm25"}]}
        )
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "openaq write ran"
        assert outcome.written.endswith("available_parameters.yaml"), "sibling path"


def _ecmwf_per_store_get_json(url, **kw):
    """Return a distinct single collection id per Copernicus store host."""
    if "ads.atmosphere" in url:
        cid = "cams-global-reanalysis-eac4"
    elif "ewds" in url:
        cid = "cems-glofas-forecast"
    else:
        cid = "reanalysis-era5-land"
    return {"collections": [{"id": cid}], "links": []}


def _ecmwf_paginated_get_json(url, **kw):
    """Two pages per Copernicus store — page 1 links to page 2 via `rel=next`."""
    store = "ads" if "ads.atmosphere" in url else "ewds" if "ewds" in url else "cds"
    prefix = {"cds": "reanalysis", "ads": "cams", "ewds": "cems"}[store]
    if "page2" in url:
        return {"collections": [{"id": f"{prefix}-two"}], "links": []}
    return {
        "collections": [{"id": f"{prefix}-one"}],
        "links": [{"rel": "next", "href": url + "?page2"}],
    }


class TestWriteEcmwfThroughRefreshOne:
    """Tests for refresh_one(write=True) on a generic-writer provider."""

    def test_writes_per_store_index_from_live_fetch(self, tmp_path, monkeypatch):
        """ecmwf --write persists per-store (cds/ads/ewds) ids into available_datasets."""
        info, module, dst = _catalog_copy("ecmwf", tmp_path, monkeypatch)
        monkeypatch.setattr(refresh_mod, "_get_json", _ecmwf_per_store_get_json)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "write succeeded"
        assert outcome.written.endswith("_index.yaml"), "index file written"
        module.clear_catalog_cache()
        data = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert data["available_datasets"] == {
            "cds": ["reanalysis-era5-land"],
            "ads": ["cams-global-reanalysis-eac4"],
            "ewds": ["cems-glofas-forecast"],
        }, "per-store ids persisted"
        # the loader unions every store's ids into the flat availability list
        catalog = load_catalog(info)
        for expected in (
            "reanalysis-era5-land",
            "cams-global-reanalysis-eac4",
            "cems-glofas-forecast",
        ):
            assert expected in catalog.available_datasets, f"{expected} unioned"

    def test_pagination_follows_rel_next_across_pages(self, tmp_path, monkeypatch):
        """`rel=next` is followed, so every page's ids land in the per-store index."""
        info, module, dst = _catalog_copy("ecmwf", tmp_path, monkeypatch)
        monkeypatch.setattr(refresh_mod, "_get_json", _ecmwf_paginated_get_json)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "write succeeded"
        module.clear_catalog_cache()
        data = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert data["available_datasets"] == {
            "cds": ["reanalysis-one", "reanalysis-two"],
            "ads": ["cams-one", "cams-two"],
            "ewds": ["cems-one", "cems-two"],
        }, "both pages' ids per store persisted"


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
            stac_cli,
            "get_json",
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

        monkeypatch.setattr(stac_cli, "get_json", boom)
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
            stac_cli,
            "get_json",
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

        monkeypatch.setattr(stac_cli, "get_json", fake)
        outcome = refresh_one(_info("stac"))
        assert {"a", "b"} <= set(outcome.new_ids), "both pages gathered"

    def test_network_error_is_captured(self, monkeypatch):
        """A failed request reports 'error' rather than raising."""

        def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(stac_cli, "get_json", boom)
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


class TestBiodiversityRefreshers:
    """Tests for the gbif / obis / wdpa / iucn refreshers (no network).

    The cluster catalogs follow the s3 pattern: the curated `available_datasets:`
    plus the catalog's `datasets:` keys *are* the universe — no anonymous live
    endpoint enumerates GBIF taxa or Protected Planet countries. Each refresher
    returns the combined sorted set so audit reports zero drift on a clean tree.
    """

    @pytest.mark.parametrize("provider", ["gbif", "obis", "wdpa", "iucn"])
    def test_refresh_reports_ok_with_curated_universe(self, provider):
        """Each cluster backend reports ok and counts the curated universe."""
        outcome = refresh_one(_info(provider))
        assert outcome.status == "ok", f"{provider} refresh ran: {outcome.detail}"
        assert outcome.live_count > 0, f"{provider} live universe is non-empty"

    @pytest.mark.parametrize("provider", ["gbif", "obis", "wdpa", "iucn"])
    def test_audit_reports_zero_drift_on_clean_catalog(self, provider):
        """Audit confirms the curated rows match the curated universe."""
        outcome = audit_one(_info(provider))
        assert outcome.status == "ok", f"{provider} audit ran: {outcome.detail}"
        assert outcome.broken == [], f"{provider} curated rows resolve upstream"
        assert outcome.untracked == [], f"{provider} no untracked drift"
