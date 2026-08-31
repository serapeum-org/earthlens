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


class TestCoverageOne:
    """Tests for coverage_one (the `audit --coverage` driver)."""

    def test_unsupported_provider(self):
        """A provider with no classifier reports unsupported."""
        assert coverage_one(_info("gdacs")).status == "unsupported"

    def test_to_dict_carries_counts(self):
        """CoverageOutcome.to_dict round-trips the buckets."""
        data = CoverageOutcome("gee", "ok", counts={"DONE": 2}).to_dict()
        assert data["counts"] == {"DONE": 2}


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


def _raise_dds_error(record):
    """A variable-lister stand-in that fails the way a bad `.dds` fetch would."""
    raise RuntimeError("dds fetch failed")


class TestAuditVariables:
    """Tests for the variable-drift dimension (_audit_variables)."""

    @staticmethod
    def _catalog(**variables_by_key):
        """A fake catalog whose records carry the given variable lists."""
        datasets = {
            key: SimpleNamespace(variables=list(names))
            for key, names in variables_by_key.items()
        }
        return SimpleNamespace(datasets=datasets)

    def test_unsupported_when_no_lister(self):
        """A provider with no variable-lister reports 'unsupported'."""
        status, drift = refresh_mod._audit_variables(self._catalog(a=["x"]), "gdacs")
        assert status == "unsupported" and drift == []

    def test_reports_drift_for_unserved_variable(self, monkeypatch):
        """A curated variable the provider no longer serves is drift."""
        monkeypatch.setitem(
            refresh_mod._VARIABLE_LISTERS, "erddap", lambda rec: {"WTMP"}
        )
        status, drift = refresh_mod._audit_variables(
            self._catalog(cwwcNDBCMet=["wtmp"]), "erddap"
        )
        assert status == "ok"
        assert drift == ["cwwcNDBCMet:wtmp"], "re-cased variable reported as drift"

    def test_no_drift_when_all_served(self, monkeypatch):
        """A curated variable still served is not drift."""
        monkeypatch.setitem(
            refresh_mod._VARIABLE_LISTERS, "erddap", lambda rec: {"WTMP", "ATMP"}
        )
        status, drift = refresh_mod._audit_variables(
            self._catalog(cwwcNDBCMet=["WTMP"]), "erddap"
        )
        assert status == "ok" and drift == []

    def test_fetch_error_is_captured(self, monkeypatch):
        """A lister that raises reports 'error', never propagates."""
        monkeypatch.setitem(refresh_mod._VARIABLE_LISTERS, "erddap", _raise_dds_error)
        status, drift = refresh_mod._audit_variables(self._catalog(x=["v"]), "erddap")
        assert status == "error" and drift == []


class TestAuditOutcome:
    """Tests for AuditOutcome."""

    def test_to_dict_exposes_broken(self):
        """to_dict carries the broken-drift list."""
        assert AuditOutcome("stac", "ok", broken=["gone"]).to_dict()["broken"] == [
            "gone"
        ]

    def test_to_dict_carries_variable_dimension(self):
        """to_dict exposes the variable_status and variable_drift fields."""
        data = AuditOutcome(
            "erddap", "ok", variable_status="ok", variable_drift=["cwwcNDBCMet:wtmp"]
        ).to_dict()
        assert data["variable_status"] == "ok"
        assert data["variable_drift"] == ["cwwcNDBCMet:wtmp"]


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
