"""Tests for the OpenAQ catalog-tooling handlers (`earthlens.openaq.cli`).

Moved out of core's CLI test suite when the OpenAQ handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import importlib
import pathlib
import shutil

import pytest
import yaml

import earthlens.openaq.cli as openaq_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import refresh_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the openaq backend."""
    return next(b for b in list_backends() if b.provider == "openaq")


def _catalog_copy(tmp_path, monkeypatch):
    """Copy openaq's catalog (dir or single file) and repoint CATALOG_PATH."""
    info = _info()
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


class TestRefresher:
    """Tests for the OpenAQ lister."""

    def test_lists_parameter_names(self, monkeypatch):
        """openaq refresh reads the v3 /parameters name list."""
        monkeypatch.setattr(
            openaq_cli,
            "get_json",
            lambda url, **kw: {"results": [{"name": "pm25"}, {"name": "o3"}]},
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "openaq refresh ran"
        assert outcome.live_count == 2, "two parameter names listed"


class TestWriter:
    """Tests for the OpenAQ sibling-index writer."""

    def test_writes_available_parameters_sibling(self, tmp_path, monkeypatch):
        """openaq --write persists the flat live parameter list to a sibling."""
        info, _module, _dst = _catalog_copy(tmp_path, monkeypatch)
        path = openaq_cli.writer(info, {"openaq": ["o3", "pm25"]})
        data = yaml.safe_load(pathlib.Path(path).read_text("utf-8"))
        assert data["available_parameters"] == ["o3", "pm25"], "flat list written"

    def test_refresh_one_write_reports_sibling_path(self, tmp_path, monkeypatch):
        """refresh_one(write=True) returns the sibling path for openaq."""
        info, _module, _dst = _catalog_copy(tmp_path, monkeypatch)
        monkeypatch.setattr(
            openaq_cli, "get_json", lambda url, **kw: {"results": [{"name": "pm25"}]}
        )
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "openaq write ran"
        assert outcome.written.endswith("available_parameters.yaml"), "sibling path"
