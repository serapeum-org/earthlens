"""Tests for the WorldPop catalog refresh / validate tool (offline)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from earthlens.worldpop.catalog import Catalog
from tests.worldpop.conftest import _FakeResponse

pytestmark = pytest.mark.worldpop

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "worldpop"


def _load(name: str):
    """Import a worldpop tool module by filename stem from its file path."""
    spec = importlib.util.spec_from_file_location(
        f"_wp_{name}", _TOOLS_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool():
    """Import the refresh tool module (back-compat helper)."""
    return _load("refresh_worldpop_catalog")


def _fake_rest(top_aliases, subaliases, records=None):
    """Return a fake `get(url, ...)` dispatching over the REST scheme.

    Args:
        top_aliases: aliases the `/rest/data` listing returns.
        subaliases: `alias -> [sub-alias id, …]` for `/rest/data/{alias}`.
        records: optional `data` list for `/rest/data/{alias}/{sub}`.
    """
    from earthlens.worldpop.rest import BASE_URL

    def fake_get(url, params=None, timeout=None):
        if url == BASE_URL:
            return _FakeResponse(
                json_data={"data": [{"alias": a} for a in top_aliases]}
            )
        for alias, ids in subaliases.items():
            if url == f"{BASE_URL}/{alias}":
                return _FakeResponse(json_data={"data": [{"alias": i} for i in ids]})
        return _FakeResponse(json_data={"data": records or []})

    return fake_get


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
            json_data={
                "data": [{"alias": "wpgp"}, {"alias": " "}, {"alias": "wpgp1km"}]
            }
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
