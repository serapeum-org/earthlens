"""Tests for the Sentinel Hub catalog-tooling handlers (`earthlens.sentinel_hub.cli`).

Moved out of core's CLI test suite when the Sentinel Hub handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.sentinel_hub.cli as sh_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the sentinel_hub backend."""
    return next(b for b in list_backends() if b.provider == "sentinel_hub")


class TestRefresher:
    """Tests for the Sentinel Hub (SDK enum) lister."""

    def test_lists_data_collection_names(self, monkeypatch):
        """sentinel_hub refresh reads the DataCollection enum names."""
        monkeypatch.setattr(
            sh_cli, "_sh_data_collection_names", lambda: ["SENTINEL2_L2A", "DEM"]
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "sentinel_hub refresh ran"
        assert outcome.live_count == 2, "two collection names listed"

    def test_grouped_sorts_names(self, monkeypatch):
        """sentinel_hub grouped wraps the DataCollection enum names, sorted."""
        monkeypatch.setattr(sh_cli, "_sh_data_collection_names", lambda: ["S2", "S1"])
        assert sh_cli.refresher(None) == {"sentinel_hub": ["S1", "S2"]}


class TestProber:
    """Tests for the Sentinel Hub band prober (offline SDK)."""

    def test_resolves_curated_key_to_bands(self):
        """A curated key resolves to the SDK collection's bands (offline)."""
        result = probe_dataset(_info(), "sentinel-2-l2a")
        assert result.status == "ok", f"sentinel_hub probe failed: {result.detail}"
        assert "B04" in result.assets, "Sentinel-2 bands probed"
        assert result.assets["B04"]["units"], "band units recorded"

    def test_curated_key_adds_collection_row(self):
        """A curated key also surfaces a collection: row with sh_collection."""
        result = probe_dataset(_info(), "sentinel-2-l2a")
        row = result.assets.get("collection:sentinel-2-l2a")
        assert row and row["sh_collection"], "collection summary row present"


class TestValidator:
    """Tests for the Sentinel Hub offline evalscript validator."""

    def test_validates_clean(self):
        """Every curated Sentinel Hub recipe's evalscript is well-formed."""
        result = validate_one(_info())
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "recipes were checked"

    def test_bad_evalscript_flagged(self, monkeypatch):
        """A recipe whose evalscript lacks //VERSION=3 + dataMask is flagged."""
        monkeypatch.setattr(
            "earthlens.sentinel_hub.read_evalscript",
            lambda name: "// not versioned\nreturn x;",
        )
        catalog = SimpleNamespace(
            recipes={
                "r": SimpleNamespace(evalscript="r.js", kind="stats"),
                "blank": SimpleNamespace(evalscript=None, kind="render"),
            }
        )
        _checked, issues = sh_cli.validator(catalog)
        assert any("//VERSION=3" in i for i in issues), "version header flagged"
        assert any("dataMask" in i for i in issues), "stats dataMask flagged"

    def test_missing_evalscript_file(self, monkeypatch):
        """A recipe whose evalscript file is missing is flagged."""

        def missing(name):
            raise FileNotFoundError(f"{name} not found")

        monkeypatch.setattr("earthlens.sentinel_hub.read_evalscript", missing)
        catalog = SimpleNamespace(
            recipes={"r": SimpleNamespace(evalscript="gone.js", kind="render")}
        )
        _checked, issues = sh_cli.validator(catalog)
        assert any("gone.js" in i for i in issues), "missing file flagged"
