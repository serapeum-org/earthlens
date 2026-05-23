"""Unit tests for the STAC catalog tooling (`tools/stac/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "stac"
sys.path.insert(0, str(_TOOLS_DIR))

import refresh_stac_catalog as refresh  # noqa: E402


@pytest.mark.stac
class TestRewriteAvailableCollections:
    """`_rewrite_available_collections` swaps the index block, keeps the rest."""

    def test_preserves_endpoints_block(self):
        """The endpoints block and header survive the rewrite untouched."""
        text = (
            "# header comment\n"
            "endpoints:\n  earth-search:\n    url: https://x\n    signer: anonymous\n"
            "available_collections:\n  earth-search:\n    - old-id\n"
        )
        out = refresh._rewrite_available_collections(text, {"earth-search": ["a", "b"]})
        assert "# header comment" in out
        parsed = yaml.safe_load(out)
        assert parsed["endpoints"]["earth-search"]["url"] == "https://x"
        assert parsed["available_collections"]["earth-search"] == ["a", "b"]

    def test_appends_when_no_block_present(self):
        """A source lacking the block gets one appended."""
        text = "endpoints:\n  e:\n    url: u\n"
        out = refresh._rewrite_available_collections(text, {"e": ["x"]})
        parsed = yaml.safe_load(out)
        assert parsed["available_collections"] == {"e": ["x"]}
        assert parsed["endpoints"]["e"]["url"] == "u"

    def test_roundtrips_through_real_index(self):
        """Rewriting the bundled index keeps it loadable by the Catalog."""
        from earthlens.stac.catalog import CATALOG_PATH

        text = (CATALOG_PATH / "_index.yaml").read_text(encoding="utf-8")
        out = refresh._rewrite_available_collections(
            text, {"planetary-computer": ["sentinel-2-l2a"]}
        )
        parsed = yaml.safe_load(out)
        assert parsed["available_collections"]["planetary-computer"] == ["sentinel-2-l2a"]
        assert "endpoints" in parsed
