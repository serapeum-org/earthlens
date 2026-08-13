"""Unit tests for `earthlens.cli.stanza` (network mocked)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from earthlens.cli import stanza as stanza_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.stanza import StanzaResult, emit_stanza, supported_providers

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_emitters_wired_up(self):
        """Every add-* provider has an emitter."""
        assert set(supported_providers()) == {
            "ecmwf",
            "earthdata",
            "usgs_water",
            "hdx",
            "eumetsat",
            "gee",
            "jaxa",
            "erddap",
            "gbif",
            "obis",
            "wdpa",
            "iucn",
        }


class TestEmitStanza:
    """Tests for emit_stanza dispatch."""

    def test_unsupported_provider(self):
        """A provider with no emitter reports 'unsupported' (no network)."""
        assert emit_stanza(_info("chc"), "anything").status == "unsupported"

    def test_key_defaults_to_upstream_id(self):
        """An omitted key falls back to the upstream id."""
        result = emit_stanza(_info("usgs_water"), "00060")
        assert result.key == "00060", "key defaulted to the id"


class TestWriteStanza:
    """Tests for write_stanza (the curate --write file insertion)."""

    def test_usgs_water_appends_to_single_file(self, tmp_path, monkeypatch):
        """A single-file provider appends the row under parameters: in place."""
        import importlib

        import yaml

        from earthlens.cli import stanza as sm
        from earthlens.cli.adapter import load_catalog

        info = _info("usgs_water")
        module = importlib.import_module(f"{info.module}.catalog")
        import shutil

        dst = tmp_path / "usgs_water_data_catalog.yaml"
        shutil.copy(module.CATALOG_PATH, dst)
        monkeypatch.setattr(module, "CATALOG_PATH", dst)
        module.clear_catalog_cache()
        result = emit_stanza(info, "99999", key="my_param", name="Test", units="x")
        path = sm.write_stanza(info, result, None)
        module.clear_catalog_cache()
        catalog = load_catalog(info)
        assert catalog.datasets["my_param"].code == "99999", "row appended + reloads"
        assert yaml.safe_load(open(path))["parameters"]["my_param"], "under parameters:"

    def test_sharded_requires_target(self):
        """A sharded-catalog provider without --target (and no auto-pick) errors."""
        result = StanzaResult("earthdata", "x", "X", "ok", row={"short_name": "X"})
        with pytest.raises(ValueError, match="--target"):
            stanza_mod.write_stanza(_info("earthdata"), result, None)

    def test_gee_auto_categorises_target(self, tmp_path, monkeypatch):
        """gee without --target auto-picks the per-family file from the asset id."""
        import importlib

        info = _info("gee")
        module = importlib.import_module(f"{info.module}.catalog")
        monkeypatch.setattr(module, "CATALOG_PATH", tmp_path)
        result = StanzaResult(
            "gee",
            "s1grd",
            "COPERNICUS/S1_GRD",
            "ok",
            row={"title": "Sentinel-1 SAR GRD"},
        )
        written = stanza_mod.write_stanza(info, result, None)
        assert written.endswith("sar-radar.yaml"), "SAR asset routed to sar-radar"
        assert (tmp_path / "sar-radar.yaml").exists(), "the category file was written"

    def test_duplicate_key_rejected(self, tmp_path, monkeypatch):
        """Writing a key that already exists raises rather than duplicating."""
        import importlib
        import shutil

        from earthlens.cli import stanza as sm
        from earthlens.cli.adapter import load_catalog

        info = _info("usgs_water")
        module = importlib.import_module(f"{info.module}.catalog")
        dst = tmp_path / "usgs_water_data_catalog.yaml"
        shutil.copy(module.CATALOG_PATH, dst)
        monkeypatch.setattr(module, "CATALOG_PATH", dst)
        module.clear_catalog_cache()
        existing = next(iter(load_catalog(info).datasets))
        result = StanzaResult("usgs_water", existing, "x", "ok", row={"code": "1"})
        with pytest.raises(ValueError, match="already curated"):
            sm.write_stanza(info, result, None)


class TestStanzaResult:
    """Tests for StanzaResult."""

    def test_to_yaml_nests_under_datasets(self):
        """to_yaml renders the row under datasets:<key>."""
        text = StanzaResult("usgs_water", "q", "00060", "ok", row={"code": "00060"})
        assert "datasets:" in text.to_yaml() and "q:" in text.to_yaml()

    def test_to_yaml_empty_when_no_row(self):
        """An unsupported/error result renders no YAML."""
        assert StanzaResult("chc", "x", "x", "unsupported").to_yaml() == ""


class TestBiodiversityEmitters:
    """Tests for the gbif / obis / wdpa / iucn emitters (no network)."""

    def test_gbif_seeds_taxon_row(self):
        """`emit_stanza` for gbif seeds taxon_key + title + rank from args."""
        result = emit_stanza(
            _info("gbif"), "212", key="birds", title="Aves", rank="class"
        )
        assert result.status == "ok", f"emit ran: {result.detail}"
        assert result.row == {"taxon_key": 212, "title": "Aves", "rank": "class"}

    def test_obis_seeds_species_row(self):
        """`emit_stanza` for obis seeds scientific_name + title from args."""
        result = emit_stanza(
            _info("obis"), "Mola mola", key="ocean-sunfish", title="Ocean sunfish"
        )
        assert result.status == "ok", f"emit ran: {result.detail}"
        assert result.row == {"scientific_name": "Mola mola", "title": "Ocean sunfish"}

    def test_wdpa_seeds_country_row(self):
        """`emit_stanza` for wdpa seeds name + region from args."""
        result = emit_stanza(
            _info("wdpa"), "KEN", key="KEN", name="Kenya", region="Africa"
        )
        assert result.status == "ok", f"emit ran: {result.detail}"
        assert result.row == {"name": "Kenya", "region": "Africa"}

    def test_iucn_seeds_country_row(self):
        """`emit_stanza` for iucn seeds name + region from args."""
        result = emit_stanza(
            _info("iucn"), "KE", key="KE", name="Kenya", region="Africa"
        )
        assert result.status == "ok", f"emit ran: {result.detail}"
        assert result.row == {"name": "Kenya", "region": "Africa"}

    def test_cluster_blocks_register(self):
        """Cluster catalogs land under their own top-level YAML blocks."""
        assert stanza_mod._STANZA_BLOCK["gbif"] == "taxa"
        assert stanza_mod._STANZA_BLOCK["obis"] == "species"
        assert stanza_mod._STANZA_BLOCK["wdpa"] == "countries"
        assert stanza_mod._STANZA_BLOCK["iucn"] == "countries"
