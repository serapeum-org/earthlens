"""Unit tests for `earthlens.cli.refresh` (network mocked, writes to tmp)."""

from __future__ import annotations

import shutil

import pytest
import yaml

import earthlens.stac.catalog as stac_catalog
from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import (
    AuditOutcome,
    RefreshOutcome,
    _curated_collection_ids,
    _diff,
    _flatten,
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
        assert {"stac", "openeo", "hdx", "earthdata", "openaq", "cmems"} <= set(
            supported_providers()
        )


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
