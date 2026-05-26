"""Offline tests for the EUMETSAT catalog tools (refresh / audit logic)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eumetsat

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "eumetsat"


def _load(name: str):
    """Import a tools module by file path (tools/ is not a package)."""
    sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diff_catalog_reports_each_category():
    """diff_catalog splits findings into gone / index_gone / new."""
    store = _load("_store")
    findings = store.diff_catalog(
        live_ids={"A", "B"},
        curated_ids={"A", "C"},  # C is gone
        available_ids={"A", "D"},  # D index-gone; B is new
    )
    assert findings["gone"] == ["C"]
    assert findings["index_gone"] == ["D"]
    assert findings["new"] == ["B"]


def test_diff_catalog_clean_when_in_sync():
    """No drift when curated/index ids are all live."""
    store = _load("_store")
    findings = store.diff_catalog({"A", "B"}, {"A"}, {"A", "B"})
    assert findings == {"gone": [], "index_gone": [], "new": []}


def test_audit_strict_exit_code_via_diff():
    """A drift finding maps to a non-zero strict exit code (logic check)."""
    store = _load("_store")
    findings = store.diff_catalog({"A"}, {"A", "B"}, {"A"})
    assert sum(len(v) for v in findings.values()) == 1


def test_probe_resolve_passthrough_for_collection_id():
    """probe._resolve_to_id returns a real EO:EUM:DAT id unchanged (no network)."""
    probe = _load("probe_eumetsat_product")
    assert probe._resolve_to_id("EO:EUM:DAT:MSG:HRSEVIRI") == "EO:EUM:DAT:MSG:HRSEVIRI"


def test_probe_resolve_curated_key():
    """probe._resolve_to_id maps a curated key to its collection id (no network)."""
    probe = _load("probe_eumetsat_product")
    assert probe._resolve_to_id("msg-hrseviri") == "EO:EUM:DAT:MSG:HRSEVIRI"


def test_tool_clis_parse_help():
    """All three CLIs build their argument parsers without error."""
    refresh = _load("refresh_eumetsat_catalog")
    audit = _load("audit_eumetsat_catalog")
    probe = _load("probe_eumetsat_product")
    with pytest.raises(SystemExit):
        refresh.main(["--help"])
    with pytest.raises(SystemExit):
        probe.main(["--help"])
    with pytest.raises(SystemExit):
        audit.main(["--help"])
