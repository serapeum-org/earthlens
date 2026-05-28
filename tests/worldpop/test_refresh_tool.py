"""Tests for the WorldPop catalog refresh / validate tool (offline)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from earthlens.worldpop.catalog import Catalog
from tests.worldpop.conftest import _FakeResponse

pytestmark = pytest.mark.worldpop

_TOOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "worldpop"
    / "refresh_worldpop_catalog.py"
)


def _load_tool():
    """Import the refresh tool module from its file path."""
    spec = importlib.util.spec_from_file_location("_wp_refresh", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_structure_passes_on_curated_catalog():
    """validate_structure finds no problems in the shipped catalog."""
    tool = _load_tool()
    assert tool.validate_structure(Catalog()) == []


def test_validate_structure_flags_missing_subaliases():
    """A product with no sub-aliases is flagged."""
    tool = _load_tool()
    from earthlens.worldpop.catalog import Product

    cat = Catalog(datasets={"x": Product(alias="x")})
    problems = tool.validate_structure(cat)
    assert any("no sub-aliases" in p for p in problems)


def test_refresh_builds_index_with_fake_http():
    """refresh crawls each alias and collects its sub-alias ids (blanks dropped)."""
    tool = _load_tool()

    def fake_get(url, timeout=None):
        return _FakeResponse(
            json_data={"data": [{"alias": "wpgp"}, {"alias": " "}, {"alias": "wpgp1km"}]}
        )

    index = tool.refresh(get=fake_get)
    assert index["pop"] == ["wpgp", "wpgp1km"]
    assert set(index) == set(tool.KNOWN_ALIASES)


def test_validate_live_flags_missing_upstream():
    """validate_live flags a curated sub-alias absent from the live list."""
    tool = _load_tool()

    def fake_get(url, timeout=None):
        return _FakeResponse(json_data={"data": [{"alias": "wpgp"}]})

    problems = tool.validate_live(Catalog(), get=fake_get)
    assert any("missing upstream" in p for p in problems)


def test_main_validate_returns_zero(capsys):
    """The validate CLI exits 0 on the curated catalog (offline)."""
    tool = _load_tool()
    assert tool.main(["validate"]) == 0
