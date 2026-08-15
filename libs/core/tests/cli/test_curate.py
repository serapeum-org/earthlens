"""Unit tests for `earthlens.cli.curate` (network mocked)."""

from __future__ import annotations

import pytest

import earthlens.stac.cli as stac_cli
from earthlens.cli import curate as curate_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import (
    ProbeResult,
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


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_probers_wired_up(self):
        """The wired-up curation probers all appear."""
        assert {
            "stac",
            "openeo",
            "gee",
            "sentinel_hub",
            "cmems",
            "earthdata",
            "hdx",
            "firms",
            "jaxa",
        } <= set(supported_providers())


class TestGhslProbe:
    """Tests for the GHSL availability prober (offline, from the catalog)."""

    def test_enumerates_epoch_resolution_matrix(self):
        """ghsl probe reports the curated epoch x resolution blocks offline."""
        info = _info("ghsl")
        from earthlens.cli.adapter import load_catalog

        product = next(iter(load_catalog(info).datasets))
        result = probe_dataset(info, product)
        assert result.status == "ok", f"ghsl probe failed: {result.detail}"
        assert result.assets, "at least one (epoch, resolution) block"
        entry = next(iter(result.assets.values()))
        assert "release" in entry and "crs" in entry, "release + crs recorded"


class TestDeepProbers:
    """Tests for the credentialed `--deep` samplers (creds/network mocked)."""

    def test_deep_falls_back_to_light_prober(self, monkeypatch):
        """--deep on a provider with no deep sampler uses the light prober."""
        monkeypatch.setattr(stac_cli, "get_json", lambda url: _SAMPLE_ITEM)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a", deep=True)
        assert result.status == "ok", "stac --deep fell back to the light prober"


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
        result = probe_dataset(_info("gdacs"), "anything")
        assert result.status == "unsupported", "gdacs cannot be probed"

    def test_ok_with_mocked_sample(self, monkeypatch):
        """A live sample item is parsed into the asset schema."""
        monkeypatch.setattr(stac_cli, "get_json", lambda url: _SAMPLE_ITEM)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a")
        assert result.status == "ok", "probe succeeded"
        assert result.assets["B04"]["common_name"] == "red", "band metadata parsed"

    def test_no_items_is_error(self, monkeypatch):
        """A collection that yields no sample item reports 'error'."""
        monkeypatch.setattr(stac_cli, "get_json", lambda url: {"features": []})
        result = probe_dataset(_info("stac"), "empty-collection")
        assert result.status == "error", "no sample -> error"
        assert "no sample item" in result.detail, "reason preserved"

    def test_network_error_is_captured(self, monkeypatch):
        """A failed request (every endpoint) reports 'error', not raised."""
        import earthlens.stac.cli as stac_cli

        def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(stac_cli, "get_json", boom)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a")
        assert result.status == "error", "failure captured"


class TestInferDtype:
    """Tests for _infer_dtype."""

    @pytest.mark.parametrize(
        "value, expected",
        [("42", "int"), ("3.14", "float"), ("hot", "str"), ("", "str"), (None, "str")],
    )
    def test_classification(self, value, expected):
        """A sample string is classified int / float / str (blank -> str).

        Args:
            value: The sampled cell value.
            expected: The inferred coarse dtype.
        """
        assert curate_mod._infer_dtype(value) == expected, f"{value!r}->{expected}"


class TestGhslProberBranches:
    """Branch coverage for the offline GHSL matrix prober."""

    def test_enumerates_release_matrix(self):
        """A curated product reports its epoch@resolution -> release/crs matrix."""
        from earthlens.cli.adapter import load_catalog

        dataset = next(iter(load_catalog(_info("ghsl")).datasets))
        result = probe_dataset(_info("ghsl"), dataset)
        assert result.status == "ok" and result.assets, "matrix enumerated"
        first = next(iter(result.assets.values()))
        assert "release" in first and "crs" in first, "release + crs reported"

    def test_unknown_product_is_error(self):
        """An unknown GHSL product reports 'error'."""
        result = probe_dataset(_info("ghsl"), "not-a-ghsl-product")
        assert result.status == "error", "unknown product -> error"


class TestBiodiversityProbers:
    """Tests for the gbif / obis / wdpa / iucn probers (offline)."""

    def test_cluster_probers_registered(self):
        """All four cluster backends appear in the probe registry."""
        for key in ("gbif", "obis", "wdpa", "iucn"):
            assert key in supported_providers(), f"{key} cluster prober wired"
