"""Tests for the Tropycal maintainer tools (probe + audit).

The tools live under `tools/tropycal/` (not part of the installed
package), so they are loaded by file path. The audit logic is pure and
runs offline; the probe is exercised against the fake `tropycal` SDK.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from earthlens.tropycal import Basin, Catalog

pytestmark = pytest.mark.tropycal

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "tropycal"


def _load_tool(name: str) -> ModuleType:
    """Import a tools/tropycal/*.py module by file path."""
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_mod = _load_tool("audit_tropycal_catalog")
probe_mod = _load_tool("probe_tropycal_fields")


def _catalog(datasets: dict[str, Basin]) -> Catalog:
    """Build a Catalog from explicit basin rows (skips the disk load)."""
    return Catalog(datasets=datasets)


class TestAudit:
    """Tests for the audit drift report."""

    def test_clean_against_bundled_catalog(self):
        """The shipped catalog matches the SDK basin/source table (no drift)."""
        report = audit_mod.audit(Catalog(), None)
        assert report["basins_not_in_sdk"] == []
        assert report["sdk_basins_missing_from_catalog"] == []
        assert report["invalid_basin_source_pairs"] == []
        assert not audit_mod.has_drift(report)

    def test_basin_not_in_sdk(self):
        """A catalog basin tropycal does not serve is reported."""
        cat = _catalog(
            {**Catalog().datasets, "marsbasin": Basin(name="Mars", sources=["ibtracs"])}
        )
        report = audit_mod.audit(cat, None)
        assert report["basins_not_in_sdk"] == ["marsbasin"]
        assert audit_mod.has_drift(report)

    def test_missing_sdk_basin(self):
        """An SDK basin absent from the catalog is reported."""
        cat = _catalog({"north_atlantic": Basin(name="NA", sources=["ibtracs", "hurdat"])})
        report = audit_mod.audit(cat, None)
        assert "all" in report["sdk_basins_missing_from_catalog"]
        assert audit_mod.has_drift(report)

    def test_invalid_basin_source_pair(self):
        """A (basin, source) pair tropycal does not support is reported."""
        cat = _catalog({"west_pacific": Basin(name="WP", sources=["ibtracs", "hurdat"])})
        report = audit_mod.audit(cat, None)
        assert "west_pacific:hurdat" in report["invalid_basin_source_pairs"]

    def test_derived_field_not_flagged(self):
        """A derived catalog field (category) absent from the sample is not drift."""
        probe = {"basin": "north_atlantic", "fields": {"vmax": {}, "mslp": {}}}
        report = audit_mod.audit(Catalog(), probe)
        assert report["catalog_fields_absent_from_sample"] == []
        assert not audit_mod.has_drift(report)

    def test_probe_basin_not_in_catalog(self):
        """A probe for an unknown basin is reported."""
        probe = {"basin": "atlantis", "fields": {}}
        report = audit_mod.audit(Catalog(), probe)
        assert report["probe_basin_not_in_catalog"] == ["atlantis"]

    def test_sample_only_fields_are_informational(self):
        """Observed columns the catalog omits do not count as drift."""
        probe = {"basin": "north_atlantic", "fields": {"vmax": {}, "mslp": {}, "extra_obs": {}}}
        report = audit_mod.audit(Catalog(), probe)
        assert "extra_obs" in report["sample_fields_absent_from_catalog"]
        assert not audit_mod.has_drift(report)

    def test_format_markdown(self):
        """The Markdown renderer emits a table with an em-dash for clean rows."""
        table = audit_mod.format_markdown({"check_a": [], "check_b": ["x", "y"]})
        assert "| `check_a` | — |" in table
        assert "| `check_b` | x, y |" in table

    def test_main_strict_clean_returns_zero(self, capsys):
        """audit main() with --strict exits 0 when the catalog is clean."""
        assert audit_mod.main(["--strict"]) == 0

    def test_main_json_format(self, capsys):
        """audit main() --format json prints a JSON report."""
        audit_mod.main(["--format", "json"])
        out = capsys.readouterr().out
        assert '"basins_not_in_sdk"' in out


class TestProbe:
    """Tests for the probe field-discovery tool."""

    def test_stringify_timestamp(self):
        """_stringify renders a pandas Timestamp as ISO text."""
        out = probe_mod._stringify(pd.Timestamp("2005-08-25T06:00:00"))
        assert out.startswith("2005-08-25")

    def test_stringify_scalar(self):
        """_stringify renders a plain scalar via str()."""
        assert probe_mod._stringify(42) == "42"

    def test_probe_fields_against_fake_sdk(self, fake_tropycal):
        """probe_fields unions to_dataframe columns and flags no category column."""
        summary = probe_mod.probe_fields("north_atlantic", "hurdat", 2005, 5)
        assert summary["storms_sampled"] == 1
        assert summary["has_category_column"] is False
        assert "vmax" in summary["fields"]
        assert summary["fields"]["vmax"]["dtype"]

    def test_probe_main_writes_sidecar(self, fake_tropycal, tmp_path):
        """probe main() writes the JSON sidecar to --out."""
        out = tmp_path / "probe.json"
        assert probe_mod.main(["--out", str(out)]) == 0
        assert out.exists()
        assert '"fields"' in out.read_text(encoding="utf-8")
