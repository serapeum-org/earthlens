"""Unit tests for `earthlens.cli.curate` (network mocked)."""

from __future__ import annotations

import pytest

from earthlens.cli import curate as curate_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import (
    ProbeResult,
    _asset_fields,
    _asset_schema,
    probe_dataset,
    supported_providers,
)

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


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestAssetSchema:
    """Tests for _asset_schema."""

    def test_extracts_band_metadata(self):
        """media type / common name / dtype / nodata are recovered per asset."""
        schema = _asset_schema(_SAMPLE_ITEM["features"][0])
        assert schema["B04"] == {
            "media_type": "image/tiff",
            "common_name": "red",
            "dtype": "uint16",
            "nodata": 0,
        }

    def test_absent_extensions_are_none(self):
        """An asset with no band extensions yields None fields."""
        schema = _asset_schema(_SAMPLE_ITEM["features"][0])
        assert schema["thumbnail"]["dtype"] is None, "no raster:bands -> None"

    def test_pystac_like_asset_is_normalised(self):
        """A pystac-style asset (media_type/extra_fields) is read like a dict."""
        from types import SimpleNamespace

        asset = SimpleNamespace(
            media_type="image/tiff",
            extra_fields={"raster:bands": [{"data_type": "int16"}]},
        )
        fields = _asset_fields(asset)
        assert fields["type"] == "image/tiff", "media_type folded into 'type'"
        assert fields["raster:bands"][0]["data_type"] == "int16", "extra_fields kept"


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_probers_wired_up(self):
        """STAC, openEO and GEE all have curation probers."""
        assert {"stac", "openeo", "gee"} <= set(supported_providers())


class TestOpeneoProbe:
    """Tests for the openEO band prober."""

    def test_extracts_band_schema(self, monkeypatch):
        """openeo probe reads summaries.eo:bands into a band schema."""
        from earthlens.cli import curate as curate_mod

        monkeypatch.setattr(
            curate_mod,
            "_get_json",
            lambda url, **kw: {
                "summaries": {
                    "eo:bands": [
                        {"name": "B04", "common_name": "red", "data_type": "int16"}
                    ]
                }
            },
        )
        result = probe_dataset(_info("openeo"), "SENTINEL2_L2A")
        assert result.status == "ok", "openeo probe ran"
        assert result.assets["B04"]["common_name"] == "red", "band parsed"


class TestGeeProbe:
    """Tests for the GEE band prober."""

    def test_extracts_band_schema(self, monkeypatch):
        """gee probe reads its STAC doc's eo:bands (gee:units / gsd)."""
        from earthlens.cli import curate as curate_mod

        monkeypatch.setattr(
            curate_mod,
            "_get_json",
            lambda url, **kw: {
                "summaries": {
                    "eo:bands": [{"name": "hurs", "gee:units": "%", "gsd": [27830]}]
                }
            },
        )
        result = probe_dataset(_info("gee"), "NASA/GDDP-CMIP6")
        assert result.status == "ok", "gee probe ran"
        assert result.assets["hurs"]["units"] == "%", "units parsed"
        assert result.assets["hurs"]["gsd"] == 27830, "gsd unwrapped from list"


class TestProbeResult:
    """Tests for ProbeResult."""

    def test_to_dict_nests_assets(self):
        """to_dict exposes the nested asset schema."""
        result = ProbeResult("stac", "x", "ok", assets={"B04": {"common_name": "red"}})
        assert result.to_dict()["assets"]["B04"]["common_name"] == "red"


class TestProbeDataset:
    """Tests for probe_dataset."""

    def test_unsupported_provider(self):
        """A provider with no prober reports 'unsupported' (no network)."""
        result = probe_dataset(_info("chc"), "anything")
        assert result.status == "unsupported", "chc cannot be probed"

    def test_ok_with_mocked_sample(self, monkeypatch):
        """A live sample item is parsed into the asset schema."""
        monkeypatch.setattr(curate_mod, "_get_json", lambda url: _SAMPLE_ITEM)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a")
        assert result.status == "ok", "probe succeeded"
        assert result.assets["B04"]["common_name"] == "red", "band metadata parsed"

    def test_no_items_is_error(self, monkeypatch):
        """A collection that yields no sample item reports 'error'."""
        monkeypatch.setattr(curate_mod, "_get_json", lambda url: {"features": []})
        result = probe_dataset(_info("stac"), "empty-collection")
        assert result.status == "error", "no sample -> error"
        assert "no sample item" in result.detail, "reason preserved"

    def test_network_error_is_captured(self, monkeypatch):
        """A failed request (every endpoint) reports 'error', not raised."""

        def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(curate_mod, "_get_json", boom)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a")
        assert result.status == "error", "failure captured"
