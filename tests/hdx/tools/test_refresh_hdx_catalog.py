"""Unit tests for the HDX catalog refresh / audit tooling (faked SDK)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from ..conftest import FakeHdx, FakeResource

pytestmark = pytest.mark.hdx

_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "hdx"


def _load_tool():
    """Import tools/hdx/refresh_hdx_catalog.py by path."""
    sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(
        "refresh_hdx_catalog", _TOOLS / "refresh_hdx_catalog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class TestKindForFormat:
    """Tests for kind_for_format (CKAN label -> output kind)."""

    @pytest.mark.parametrize(
        "fmt, expected",
        [
            ("GeoTIFF", "raster"),
            ("Geopackage", "vector"),
            ("SHP", "vector"),
            ("CSV", "tabular"),
            ("XLSX", "tabular"),
            ("Mystery", None),
        ],
    )
    def test_kind(self, fmt, expected):
        """Known format labels map to their output kind; unknown -> None."""
        assert tool.kind_for_format(fmt) == expected


class TestWriteIndex:
    """Tests for write_index."""

    def test_sorts_dedupes_and_counts(self, tmp_path: Path):
        """The JSON index is written sorted and de-duplicated."""
        import json

        out = tmp_path / "_available.json"
        count = tool.write_index(["b", "a", "b", "c"], out)
        assert count == 3
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["available_datasets"] == ["a", "b", "c"]


class TestSearchDatasets:
    """Tests for search_datasets (faked SDK)."""

    def test_returns_rows(self, fake_hdx: FakeHdx):
        """Each matching dataset becomes a lightweight row dict."""
        fake_hdx.add_dataset("ds-x", [FakeResource("a.csv", "CSV")])

        def fake_search(query, fq=None, page_size=1000):
            return [fake_hdx.Dataset.registry["ds-x"]]

        fake_hdx.Dataset.search_in_hdx = staticmethod(fake_search)
        rows = tool.search_datasets(org="org", with_formats=True)
        assert rows[0]["hdx_id"] == "ds-x"
        assert rows[0]["formats"] == ["CSV"]

    def test_skips_formats_by_default(self, fake_hdx: FakeHdx):
        """Without with_formats, the slow resource fetch is skipped."""
        fake_hdx.add_dataset("ds-y", [FakeResource("a.csv", "CSV")])

        def fake_search(query, fq=None, page_size=1000):
            return [fake_hdx.Dataset.registry["ds-y"]]

        fake_hdx.Dataset.search_in_hdx = staticmethod(fake_search)
        rows = tool.search_datasets()
        assert rows[0]["formats"] == [] and rows[0]["org"] == ""


class TestDatasetStanza:
    """Tests for dataset_stanza (faked SDK)."""

    def test_emits_stanza(self, fake_hdx: FakeHdx):
        """A stanza carries the id, inferred formats and output kinds."""
        fake_hdx.add_dataset("ds-x", [FakeResource("a.gpkg", "Geopackage")])
        stanza = tool.dataset_stanza("my-key", "ds-x")
        assert "my-key:" in stanza
        assert "hdx_id: ds-x" in stanza
        assert "Geopackage" in stanza
        assert "vector" in stanza

    def test_missing_dataset_raises(self, fake_hdx: FakeHdx):
        """An unknown id raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            tool.dataset_stanza("k", "ghost")


class TestAudit:
    """Tests for audit (faked SDK + real bundled catalog)."""

    def test_drift_strict_returns_1(self, fake_hdx: FakeHdx):
        """Unresolvable curated ids fail the strict audit."""
        assert tool.audit(strict=True) == 1

    def test_drift_non_strict_returns_0(self, fake_hdx: FakeHdx):
        """Non-strict audit reports drift but returns 0."""
        assert tool.audit(strict=False) == 0

    def test_clean_audit_returns_0(self, fake_hdx: FakeHdx):
        """When every curated id resolves, the audit is clean."""
        from earthlens.hdx import Catalog

        for row in Catalog().datasets.values():
            fake_hdx.add_dataset(row.hdx_id, [])
        assert tool.audit(strict=True) == 0


class TestParser:
    """Tests for the argparse CLI."""

    def test_refresh_parsed(self):
        """The refresh subcommand parses repeatable --org."""
        ns = tool.build_parser().parse_args(
            ["refresh", "--org", "kontur", "--org", "hot", "--include-curated"]
        )
        assert ns.command == "refresh" and ns.org == ["kontur", "hot"]
        assert ns.include_curated is True

    def test_audit_strict_parsed(self):
        """The audit subcommand parses --strict."""
        ns = tool.build_parser().parse_args(["audit", "--strict"])
        assert ns.command == "audit" and ns.strict is True
