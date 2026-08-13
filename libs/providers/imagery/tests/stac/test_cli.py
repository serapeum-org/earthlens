"""Tests for the STAC catalog-tooling handlers (`earthlens.stac.cli`).

Moved out of core's CLI test suite when the STAC handlers moved into this
distribution (issue #863). The refresh_one write-orchestration tests stay in
core (they exercise the command, not the handler).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.stac.cli as stac_cli

pytestmark = pytest.mark.cli

_SAMPLE_ITEM = {
    "features": [
        {
            "assets": {
                "B04": {
                    "type": "image/tiff",
                    "eo:bands": [{"common_name": "red"}],
                    "raster:bands": [{"data_type": "uint16", "nodata": 0}],
                },
                "thumbnail": {"type": "image/png"},
            }
        }
    ]
}


class TestAssetSchema:
    """Tests for the per-asset schema extractor."""

    def test_extracts_band_metadata(self):
        """media type / common name / dtype / nodata are recovered per asset."""
        schema = stac_cli._asset_schema(_SAMPLE_ITEM["features"][0])
        assert schema["B04"] == {
            "media_type": "image/tiff",
            "common_name": "red",
            "dtype": "uint16",
            "nodata": 0,
        }

    def test_absent_extensions_are_none(self):
        """An asset with no band extensions yields None fields."""
        schema = stac_cli._asset_schema(_SAMPLE_ITEM["features"][0])
        assert schema["thumbnail"]["dtype"] is None, "no raster:bands -> None"

    def test_pystac_like_asset_is_normalised(self):
        """A pystac-style asset (media_type/extra_fields) is read like a dict."""
        asset = SimpleNamespace(
            media_type="image/tiff",
            extra_fields={"raster:bands": [{"data_type": "int16"}]},
        )
        fields = stac_cli._asset_fields(asset)
        assert fields["type"] == "image/tiff", "media_type folded into 'type'"
        assert fields["raster:bands"][0]["data_type"] == "int16", "extra_fields kept"


class TestRefresher:
    """Tests for the per-endpoint collection lister."""

    def test_groups_collection_ids_per_endpoint(self, monkeypatch):
        """Each endpoint's /collections ids are collected and sorted."""
        monkeypatch.setattr(
            stac_cli,
            "get_json",
            lambda url: {"collections": [{"id": "b"}, {"id": "a"}], "links": []},
        )
        catalog = SimpleNamespace(
            endpoints={"ep": SimpleNamespace(url="https://x/stac")}
        )
        assert stac_cli.refresher(catalog) == {"ep": ["a", "b"]}
