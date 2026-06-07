"""Unit tests for `earthlens.cli.stanza` (network mocked)."""

from __future__ import annotations

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
            "earthdata",
            "usgs_water",
            "hdx",
            "eumetsat",
            "gee",
        }


class TestUsgsWaterEmitter:
    """Tests for the USGS Water emitter (pure args, no network)."""

    def test_seeds_row_from_args(self):
        """The row is built from the code + options without any fetch."""
        result = emit_stanza(
            _info("usgs_water"),
            "00060",
            key="discharge",
            name="Discharge",
            units="ft3/s",
            services=["daily"],
        )
        assert result.status == "ok", "usgs emitter ran"
        assert result.row == {
            "code": "00060",
            "name": "Discharge",
            "units": "ft3/s",
            "group": "Physical",
            "services": ["daily"],
        }

    def test_name_defaults_from_key(self):
        """An omitted name is titled from the key."""
        result = emit_stanza(_info("usgs_water"), "00010", key="water_temp")
        assert result.row["name"] == "Water Temp", "key titled into a name"


class TestEarthdataEmitter:
    """Tests for the Earthdata emitter (public CMR)."""

    def test_infers_format_and_output_kind(self, monkeypatch):
        """The CMR collection seeds format + output_kind."""
        monkeypatch.setattr(
            stanza_mod,
            "_get_json",
            lambda url, **kw: {
                "items": [
                    {
                        "umm": {
                            "EntryTitle": "GPM IMERG",
                            "ArchiveAndDistributionInformation": "x.nc4",
                        }
                    }
                ]
            },
        )
        result = emit_stanza(
            _info("earthdata"),
            "GPM_3IMERGHH",
            key="imerg",
            version="07",
            cmr_provider="GES_DISC",
        )
        assert result.status == "ok", "earthdata emitter ran"
        assert result.row["format"] == "netcdf4", "extension mapped"
        assert result.row["output_kind"] == "raster", "gridded -> raster"
        assert result.row["daac"] == "GES_DISC", "daac defaults to provider"

    def test_vector_hint_overrides_output_kind(self, monkeypatch):
        """A GEDI short name seeds a vector output_kind."""
        monkeypatch.setattr(stanza_mod, "_get_json", lambda url, **kw: {"items": []})
        result = emit_stanza(_info("earthdata"), "GEDI04_A", cmr_provider="ORNL_DAAC")
        assert result.row["output_kind"] == "vector", "GEDI -> vector"


class TestHdxEmitter:
    """Tests for the HDX emitter (public CKAN)."""

    def test_infers_themes_from_resource_formats(self, monkeypatch):
        """Resource formats seed formats / themes / output_kinds."""
        monkeypatch.setattr(
            stanza_mod,
            "_get_json",
            lambda url, **kw: {
                "result": {
                    "organization": {"name": "kontur"},
                    "title": "Population",
                    "resources": [
                        {"name": "a.gpkg", "format": "Geopackage"},
                        {"name": "b.csv", "format": "CSV"},
                    ],
                }
            },
        )
        result = emit_stanza(_info("hdx"), "kontur-population", key="kontur-pop")
        assert result.status == "ok", "hdx emitter ran"
        assert result.row["formats"] == ["CSV", "Geopackage"], "formats sorted"
        assert result.row["output_kinds"] == ["tabular", "vector"], "kinds inferred"
        assert result.row["org"] == "kontur", "org carried"


class TestEumetsatEmitter:
    """Tests for the EUMETSAT emitter (public browse)."""

    def test_seeds_collection_row(self, monkeypatch):
        """The browse fetch validates the id and the row carries the group."""
        monkeypatch.setattr(
            stanza_mod,
            "_get_json",
            lambda url, **kw: {"collection": {"properties": {"title": "HRSEVIRI"}}},
        )
        result = emit_stanza(
            _info("eumetsat"), "EO:EUM:DAT:MSG:HRSEVIRI", key="msg", group="MSG"
        )
        assert result.status == "ok", "eumetsat emitter ran"
        assert result.row["collection_id"] == "EO:EUM:DAT:MSG:HRSEVIRI"
        assert result.row["group"] == "MSG" and result.row["output_kind"] == "raster"


class TestGeeEmitter:
    """Tests for the GEE emitter (public EE STAC)."""

    def test_seeds_bands_and_extent(self, monkeypatch):
        """The STAC doc seeds title / cadence / resolution / bands."""
        monkeypatch.setattr(
            stanza_mod,
            "_get_json",
            lambda url, **kw: {
                "title": "GDDP-CMIP6\nsecond line",
                "gee:type": "image_collection",
                "gee:interval": {"interval": 1, "unit": "day"},
                "extent": {
                    "temporal": {"interval": [["2015-01-01T00:00:00Z", None]]},
                    "spatial": {"bbox": [[-180, -90, 180, 90]]},
                },
                "summaries": {
                    "eo:bands": [
                        {
                            "name": "tas",
                            "description": "temp",
                            "gee:units": "K",
                            "gsd": [27830],
                        }
                    ]
                },
                "providers": [{"name": "NASA"}],
            },
        )
        result = emit_stanza(_info("gee"), "NASA/GDDP-CMIP6")
        assert result.status == "ok", "gee emitter ran"
        assert result.row["title"] == "GDDP-CMIP6", "first title line only"
        assert result.row["cadence"] == {"interval": 1, "unit": "day"}
        assert result.row["spatial_resolution"] == 27830.0, "gsd unwrapped"
        assert result.row["bands"]["tas"]["units"] == "K", "band units kept"
        assert "bbox" not in result.row["extent"], "global bbox dropped"

    def test_minimal_skips_fetch(self):
        """--minimal emits a placeholder row with empty bands and no network."""
        result = emit_stanza(_info("gee"), "projects/foo/bar", minimal=True)
        assert result.status == "ok" and result.row["bands"] == {}

    def test_hydrate_reads_bands_from_earth_engine(self, monkeypatch):
        """--hydrate seeds bands from a live Earth Engine query (creds-gated)."""
        monkeypatch.setattr(
            stanza_mod,
            "_gee_live_bands",
            lambda asset_id: ("image", {"B1": {}, "B2": {}}),
        )
        result = emit_stanza(_info("gee"), "projects/foo/bar", hydrate=True)
        assert result.status == "ok", "gee hydrate ran"
        assert result.row["ee_type"] == "image", "ee_type from EE asset"
        assert sorted(result.row["bands"]) == ["B1", "B2"], "live bands seeded"


class TestEmitStanza:
    """Tests for emit_stanza dispatch."""

    def test_unsupported_provider(self):
        """A provider with no emitter reports 'unsupported' (no network)."""
        assert emit_stanza(_info("chc"), "anything").status == "unsupported"

    def test_error_is_captured(self, monkeypatch):
        """A failed fetch reports 'error', not raised."""

        def boom(url, **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(stanza_mod, "_get_json", boom)
        assert emit_stanza(_info("hdx"), "x").status == "error"

    def test_key_defaults_to_upstream_id(self):
        """An omitted key falls back to the upstream id."""
        result = emit_stanza(_info("usgs_water"), "00060")
        assert result.key == "00060", "key defaulted to the id"


class TestStanzaResult:
    """Tests for StanzaResult."""

    def test_to_yaml_nests_under_datasets(self):
        """to_yaml renders the row under datasets:<key>."""
        text = StanzaResult("usgs_water", "q", "00060", "ok", row={"code": "00060"})
        assert "datasets:" in text.to_yaml() and "q:" in text.to_yaml()

    def test_to_yaml_empty_when_no_row(self):
        """An unsupported/error result renders no YAML."""
        assert StanzaResult("chc", "x", "x", "unsupported").to_yaml() == ""
