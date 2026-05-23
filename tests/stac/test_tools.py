"""Unit tests for the STAC catalog tooling (`tools/stac/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "stac"
sys.path.insert(0, str(_TOOLS_DIR))

import audit_stac_catalog as audit  # noqa: E402
import probe_stac_assets as probe  # noqa: E402
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


@pytest.mark.stac
class TestProbeAssetSchema:
    """`_asset_schema` recovers per-asset band metadata from a STAC item."""

    def test_extracts_eo_and_raster_band_fields(self):
        """common_name comes from eo:bands, dtype/nodata from raster:bands."""
        item = {
            "assets": {
                "B04": {
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "eo:bands": [{"common_name": "red"}],
                    "raster:bands": [{"data_type": "uint16", "nodata": 0}],
                }
            }
        }
        schema = probe._asset_schema(item)
        assert schema["B04"] == {
            "media_type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "common_name": "red",
            "dtype": "uint16",
            "nodata": 0,
        }

    def test_missing_band_extensions_yield_none(self):
        """An asset without band extensions yields None fields, not an error."""
        schema = probe._asset_schema({"assets": {"data": {"type": "image/tiff"}}})
        assert schema["data"] == {
            "media_type": "image/tiff",
            "common_name": None,
            "dtype": None,
            "nodata": None,
        }

    def test_asset_fields_reads_pystac_like_object(self):
        """A pystac-like Asset (media_type + extra_fields) is normalised to a dict."""
        from types import SimpleNamespace

        asset = SimpleNamespace(
            media_type="image/tiff", extra_fields={"raster:bands": [{"data_type": "int16"}]}
        )
        fields = probe._asset_fields(asset)
        assert fields["type"] == "image/tiff"
        assert fields["raster:bands"][0]["data_type"] == "int16"


@pytest.mark.stac
class TestAuditDiff:
    """`_diff_collections` / `_curated_resolved` flag catalog-vs-live drift."""

    def test_diff_reports_missing_and_untracked(self):
        """Curated-not-live is 'missing'; live-not-curated is 'untracked'."""
        curated = {"e": {"a", "b"}}
        live = {"e": {"b", "c"}}
        report = audit._diff_collections(curated, live)
        assert report["e"]["missing"] == ["a"]
        assert report["e"]["untracked"] == ["c"]

    def test_diff_empty_when_in_sync(self):
        """No drift yields an empty report."""
        assert audit._diff_collections({"e": {"a"}}, {"e": {"a"}}) == {}

    def test_curated_resolved_applies_aliases(self):
        """Each endpoint maps to its curated collections' resolved ids."""
        from earthlens.stac.catalog import Catalog

        resolved = audit._curated_resolved(Catalog())
        assert "sentinel-2-c1-l2a" in resolved["earth-search"]
        assert "sentinel-2-l2a" in resolved["planetary-computer"]
        assert "sentinel-1-grd" in resolved["cdse"]
