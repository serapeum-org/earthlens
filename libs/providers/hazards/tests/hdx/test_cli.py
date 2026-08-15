"""Tests for the HDX catalog-tooling handlers (`earthlens.hdx.cli`).

Moved out of core's CLI test suite when the HDX refresh/write/probe/emit handlers
moved into this distribution (issue #863).
"""

from __future__ import annotations

import gzip
import json

import pytest
from typer.testing import CliRunner

import earthlens.hdx.cli as hdx_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.app import app
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.stanza import emit_stanza

pytestmark = pytest.mark.cli

runner = CliRunner()


def _info():
    """Return the BackendInfo for the hdx backend."""
    return next(b for b in list_backends() if b.provider == "hdx")


class TestRefresher:
    """Tests for the HDX (CKAN) lister."""

    def test_lists_package_names(self, monkeypatch):
        """hdx refresh reads CKAN package_list and audits by hdx_id."""
        monkeypatch.setattr(
            hdx_cli,
            "get_json",
            lambda url: {"result": ["kontur-boundaries", "kontur-population"]},
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "hdx refresh ran"
        assert outcome.live_count == 2, "two package names listed"


class TestWriter:
    """Tests for the merge-preserving gzipped HDX sidecar writer."""

    def test_merge_preserves_existing_rows(self, tmp_path, monkeypatch):
        """Surviving names keep their org/title; new names get a bare row."""
        monkeypatch.setattr(hdx_cli, "index_path", lambda info: tmp_path / "x.yaml")
        sidecar = tmp_path / "_available.json.gz"
        with gzip.open(sidecar, "wt", encoding="utf-8") as handle:
            json.dump({"datasets": {"keep": {"org": "o", "title": "t"}}}, handle)
        path = hdx_cli.writer(_info(), {"g": ["keep", "new"]})
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            datasets = json.load(handle)["datasets"]
        assert datasets["keep"] == {"org": "o", "title": "t"}, "existing row kept"
        assert datasets["new"] == {"org": "", "title": ""}, "new row bare"

    def test_drops_names_gone_upstream(self, tmp_path, monkeypatch):
        """A name absent from the live fetch is dropped from the sidecar."""
        monkeypatch.setattr(hdx_cli, "index_path", lambda info: tmp_path / "x.yaml")
        sidecar = tmp_path / "_available.json.gz"
        with gzip.open(sidecar, "wt", encoding="utf-8") as handle:
            json.dump(
                {"datasets": {"keep": {"org": "O", "title": "T"}, "gone": {}}}, handle
            )
        hdx_cli.writer(_info(), {"hdx": ["keep", "newone"]})
        with gzip.open(sidecar, "rt", encoding="utf-8") as handle:
            rows = json.load(handle)["datasets"]
        assert rows["keep"] == {"org": "O", "title": "T"}, "metadata preserved"
        assert rows["newone"] == {"org": "", "title": ""}, "new id bare"
        assert "gone" not in rows, "id absent upstream dropped"

    def test_no_sidecar_starts_fresh(self, tmp_path, monkeypatch):
        """With no existing sidecar, every live name gets a fresh bare row."""
        monkeypatch.setattr(hdx_cli, "index_path", lambda info: tmp_path / "x.yaml")
        path = hdx_cli.writer(_info(), {"g": ["a"]})
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            assert "a" in json.load(handle)["datasets"], "new row created"


class TestProber:
    """Tests for the HDX resource prober (public CKAN)."""

    def test_lists_resources(self, monkeypatch):
        """hdx probe reads package_show resources into a file/format schema."""
        monkeypatch.setattr(
            hdx_cli,
            "get_json",
            lambda url, **kw: {
                "result": {
                    "resources": [{"name": "pop.gpkg.gz", "format": "Geopackage"}]
                }
            },
        )
        result = probe_dataset(_info(), "kontur-population")
        assert result.status == "ok", "hdx probe ran"
        assert result.assets["pop.gpkg.gz"]["format"] == "Geopackage", "resource parsed"


class TestEmitter:
    """Tests for the HDX emitter (public CKAN)."""

    def test_infers_themes_from_resource_formats(self, monkeypatch):
        """Resource formats seed formats / themes / output_kinds."""
        monkeypatch.setattr(
            hdx_cli,
            "get_json",
            lambda url, **kw: {
                "result": {
                    "organization": {"name": "kontur"},
                    "title": "Population",
                    "resources": [
                        {"name": "a.gpkg", "format": "Geopackage"},
                        {"name": "b.csv", "format": "CSV"},
                    ],
                }
            },
        )
        result = emit_stanza(_info(), "kontur-population", key="kontur-pop")
        assert result.status == "ok", "hdx emitter ran"
        assert result.row["formats"] == ["CSV", "Geopackage"], "formats sorted"
        assert result.row["output_kinds"] == ["tabular", "vector"], "kinds inferred"
        assert result.row["org"] == "kontur", "org carried"

    def test_curate_json_output_via_cli(self, monkeypatch):
        """`datasets curate hdx --json` emits the seeded row object."""
        monkeypatch.setattr(
            hdx_cli,
            "get_json",
            lambda url, **kw: {
                "result": {"title": "P", "resources": [{"name": "a", "format": "CSV"}]}
            },
        )
        result = runner.invoke(
            app, ["datasets", "curate", "hdx", "kontur-population", "--json"]
        )
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["row"]["hdx_id"]
