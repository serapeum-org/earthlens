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
    """Tests for write_index (enriched `{id: {org, title}}` JSON)."""

    def test_writes_sorted_enriched_rows(self, tmp_path: Path):
        """The JSON index is written sorted with org/title per id."""
        import json

        out = tmp_path / "_available.json"
        rows = {
            "b": {"org": "o-b", "title": "B"},
            "a": {"org": "o-a", "title": "A"},
        }
        count = tool.write_index(rows, out)
        assert count == 2
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert list(payload["datasets"]) == ["a", "b"]
        assert payload["datasets"]["a"] == {"org": "o-a", "title": "A"}


class TestSearchMetadata:
    """Tests for search_metadata / all_metadata (faked CKAN client)."""

    def test_search_metadata_returns_org_title(self, fake_hdx: FakeHdx):
        """package_search rows map to {id: {org, title}}."""
        tool.configure()
        fake_hdx.add_dataset("ds-x", [], org="kontur")
        meta = tool.search_metadata("*:*")
        assert meta["ds-x"]["org"] == "kontur"
        assert meta["ds-x"]["title"] == "Title for ds-x"

    def test_search_metadata_org_filter(self, fake_hdx: FakeHdx):
        """An organization fq narrows the result."""
        tool.configure()
        fake_hdx.add_dataset("ds-k", [], org="kontur")
        fake_hdx.add_dataset("ds-h", [], org="hot")
        meta = tool.search_metadata("*:*", fq="organization:kontur")
        assert set(meta) == {"ds-k"}

    def test_all_metadata_covers_every_id(self, fake_hdx: FakeHdx):
        """all_metadata enriches search hits and keeps unsearchable ids thin."""
        tool.configure()
        fake_hdx.add_dataset("ds-enriched", [], org="kontur")
        meta = tool.all_metadata()
        assert meta["ds-enriched"]["org"] == "kontur"
        assert set(meta) == set(fake_hdx.Dataset.registry)


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

    def test_refresh_all_parsed(self):
        """The refresh subcommand parses --all (whole-catalogue mode)."""
        ns = tool.build_parser().parse_args(["refresh", "--all"])
        assert ns.command == "refresh" and ns.all is True


class TestAllDatasetNames:
    """Tests for all_dataset_names (whole-catalogue enumeration)."""

    def test_returns_every_registered_id(self, fake_hdx: FakeHdx):
        """all_dataset_names returns every id the SDK knows."""
        fake_hdx.add_dataset("ds-1", [])
        fake_hdx.add_dataset("ds-2", [])
        names = set(tool.all_dataset_names())
        assert {"ds-1", "ds-2", "kontur-population-dataset"} <= names

    def test_audit_strict_parsed(self):
        """The audit subcommand parses --strict."""
        ns = tool.build_parser().parse_args(["audit", "--strict"])
        assert ns.command == "audit" and ns.strict is True
