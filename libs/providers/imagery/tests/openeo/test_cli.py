"""Tests for the openEO catalog-tooling handlers (`earthlens.openeo.cli`).

Moved out of core's CLI test suite when the openEO handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import shutil

import pytest
import yaml

import earthlens.openeo.cli as openeo_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one
from earthlens.openeo import catalog as openeo_catalog

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the openeo backend."""
    return next(b for b in list_backends() if b.provider == "openeo")


class TestRefresher:
    """Tests for the openEO collection/process listers."""

    def test_lists_collection_ids(self, monkeypatch):
        """openeo refresh reads the public /collections id list."""
        monkeypatch.setattr(
            openeo_cli,
            "get_json",
            lambda url: {"collections": [{"id": "SENTINEL2_L2A"}, {"id": "S1_GRD"}]},
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "openeo refresh ran"
        assert outcome.live_count == 2, "two collection ids listed"

    def test_process_ids(self, monkeypatch):
        """Process ids with an id are collected + sorted; others dropped."""
        monkeypatch.setattr(
            openeo_cli,
            "get_json",
            lambda url: {"processes": [{"id": "ndvi"}, {"no": "id"}]},
        )
        assert openeo_cli._process_ids() == ["ndvi"]


class TestWriter:
    """Tests for the two-block index writer."""

    def test_writes_both_collections_and_processes(self, tmp_path, monkeypatch):
        """openeo --write rewrites available_collections AND available_processes."""
        dst = tmp_path / "catalog"
        shutil.copytree(openeo_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(openeo_catalog, "CATALOG_PATH", dst)
        monkeypatch.setattr(
            openeo_cli, "_process_ids", lambda: ["load_collection", "ndvi"]
        )
        openeo_cli.writer(_info(), {"openeo": ["ONLY_ONE"]})
        after = yaml.safe_load((dst / "_index.yaml").read_text("utf-8"))
        assert after["available_collections"] == ["ONLY_ONE"], "collections rewritten"
        assert after["available_processes"] == ["load_collection", "ndvi"], "procs too"


class TestProber:
    """Tests for the openEO band prober."""

    def test_extracts_band_schema(self, monkeypatch):
        """openeo probe reads summaries.eo:bands into a band schema."""
        monkeypatch.setattr(
            openeo_cli,
            "get_json",
            lambda url, **kw: {
                "summaries": {
                    "eo:bands": [
                        {"name": "B04", "common_name": "red", "data_type": "int16"}
                    ]
                }
            },
        )
        result = probe_dataset(_info(), "SENTINEL2_L2A")
        assert result.status == "ok", "openeo probe ran"
        assert result.assets["B04"]["common_name"] == "red", "band parsed"

    def test_surfaces_cube_dimensions(self, monkeypatch):
        """Non-band cube axes appear as dim: rows carrying type + extent."""
        monkeypatch.setattr(
            openeo_cli,
            "get_json",
            lambda url, **kw: {
                "summaries": {"eo:bands": [{"name": "B04"}]},
                "cube:dimensions": {
                    "t": {"type": "temporal", "extent": ["2015-01-01", None]},
                    "bands": {"type": "bands", "values": ["B04"]},
                },
            },
        )
        result = probe_dataset(_info(), "SENTINEL2_L2A")
        assert "B04" in result.assets, "band still listed"
        assert result.assets["dim:t"]["type"] == "temporal", "temporal axis surfaced"
        assert result.assets["dim:t"]["extent"] == ["2015-01-01", None], "extent kept"
        assert "dim:bands" not in result.assets, "bands axis not duplicated"


class TestLiveValidator:
    """Tests for the live recipe validator."""

    def test_is_live_only(self, monkeypatch):
        """openeo has no offline validator; --live checks recipes vs live."""
        assert validate_one(_info()).status == "unsupported"
        monkeypatch.setattr(openeo_cli, "_live_lists", lambda: (set(), set()))
        result = validate_one(_info(), live=True)
        assert result.status == "ok", "live openeo validator ran"

    def test_live_lists_unions_ids(self, monkeypatch):
        """_live_lists collects collection + process ids from the API."""

        def fake_get(url):
            if "processes" in url:
                return {"processes": [{"id": "ndvi"}, {"no": "id"}]}
            return {"collections": [{"id": "S2"}]}

        monkeypatch.setattr(openeo_cli, "get_json", fake_get)
        collections, processes = openeo_cli._live_lists()
        assert collections == {"S2"}, "ids unioned"
        assert processes == {"ndvi"}, "ids unioned"
