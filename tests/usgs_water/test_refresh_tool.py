"""Tests for the USGS Water catalog refresh tool (offline)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.usgs_water

_TOOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "usgs_water"
    / "refresh_usgs_catalog.py"
)


def _load_tool():
    """Import the refresh tool module from its file path."""
    spec = importlib.util.spec_from_file_location("_usgs_refresh", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_passes_on_curated_catalog():
    """validate returns 0 for the shipped curated catalog."""
    tool = _load_tool()
    assert tool.main(["validate"]) == 0


def test_add_parameter_appends_and_reloads(tmp_path, monkeypatch):
    """add-parameter writes a stanza that reloads cleanly."""
    tool = _load_tool()
    catalog = tmp_path / "cat.yaml"
    catalog.write_text(
        "parameters:\n  discharge: {code: '00060', services: [daily]}\n"
    )
    monkeypatch.setattr(tool, "CATALOG_PATH", catalog)
    monkeypatch.setattr(tool.Catalog, "load", classmethod(lambda cls: None))
    rc = tool.main(["add-parameter", "ph", "00400", "--units", "std units"])
    assert rc == 0
    assert "ph" in catalog.read_text()


def test_refresh_rate_limit_message(monkeypatch):
    """A 429 from the reference table surfaces a token-advice SystemExit."""
    tool = _load_tool()

    def _boom(**_kwargs):
        raise RuntimeError("HTTP 429 quota exhausted")

    monkeypatch.setattr(tool, "_import_reference_table", lambda: _boom)
    with pytest.raises(SystemExit, match="API_USGS_PAT"):
        tool.main(["refresh"])
