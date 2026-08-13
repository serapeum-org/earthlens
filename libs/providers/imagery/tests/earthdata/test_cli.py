"""Tests for the Earthdata catalog-tooling handlers (`earthlens.earthdata.cli`).

Moved out of core's CLI test suite when the Earthdata handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import sys
import types

import pytest

import earthlens.earthdata.cli as earthdata_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.stanza import emit_stanza

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the earthdata backend."""
    return next(b for b in list_backends() if b.provider == "earthdata")


class TestRefresher:
    """Tests for the earthdata (CMR) lister."""

    def test_walks_providers_and_paginates(self, monkeypatch):
        """Each provider's CMR pages are gathered into the short-name set."""
        pages = {None: (["A", "B"], "cursor"), "cursor": (["C"], None)}
        monkeypatch.setattr(
            earthdata_cli, "_cmr_page", lambda provider, after: pages[after]
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "earthdata refresh ran"
        assert outcome.live_count == 3, "A/B/C gathered across two pages"


class TestCmrPage:
    """Tests for the Earthdata CMR pagination helper."""

    def test_reads_short_names_and_cursor(self, monkeypatch):
        """A CMR page yields its ShortNames and the next search-after cursor."""

        def fake_get(url, params=None, headers=None, timeout=None):
            return types.SimpleNamespace(
                json=lambda: {"items": [{"umm": {"ShortName": "GPM"}}, {"umm": {}}]},
                headers={"CMR-Search-After": "cursor2"},
                raise_for_status=lambda: None,
            )

        monkeypatch.setattr(earthdata_cli.requests, "get", fake_get)
        names, cursor = earthdata_cli._cmr_page("GES_DISC", None)
        assert names == ["GPM"], "only items with a ShortName are kept"
        assert cursor == "cursor2", "next cursor carried"


class TestProber:
    """Tests for the Earthdata UMM-Var prober (public CMR)."""

    def test_resolves_collection_then_variables(self, monkeypatch):
        """earthdata probe follows associations.variables to UMM-Var records."""

        def fake(url, **kw):
            if "collections" in url:
                return {"items": [{"meta": {"associations": {"variables": ["V1"]}}}]}
            return {
                "items": [
                    {
                        "umm": {
                            "Name": "precipitation",
                            "LongName": "Precipitation rate",
                            "Units": "mm/hr",
                            "DataType": "float32",
                        }
                    }
                ]
            }

        monkeypatch.setattr(earthdata_cli, "get_json", fake)
        result = probe_dataset(_info(), "GPM_3IMERGHH")
        assert result.status == "ok", "earthdata probe ran"
        assert result.assets["precipitation"]["units"] == "mm/hr", "UMM-Var parsed"

    def test_collection_with_no_variables_is_empty(self, monkeypatch):
        """A collection with no associated variables yields an empty schema."""
        monkeypatch.setattr(
            earthdata_cli,
            "get_json",
            lambda url, **kw: {"items": [{"meta": {"associations": {}}}]},
        )
        result = probe_dataset(_info(), "SOME_COLLECTION")
        assert result.status == "ok" and result.assets == {}, "empty UMM-Var"

    def test_reads_variable_records(self, monkeypatch):
        """A collection with associated variables yields their UMM-Var schema."""

        def fake_get(url, params=None):
            if "collections" in url:
                return {"items": [{"meta": {"associations": {"variables": ["V1"]}}}]}
            return {
                "items": [
                    {
                        "umm": {
                            "Name": "precip",
                            "LongName": "Precipitation",
                            "Units": "mm",
                            "DataType": "float32",
                        }
                    }
                ]
            }

        monkeypatch.setattr(earthdata_cli, "get_json", fake_get)
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert result.assets["precip"]["units"] == "mm", "variable units read"

    def test_no_collection_is_error(self, monkeypatch):
        """A short name CMR does not know reports 'error'."""
        monkeypatch.setattr(
            earthdata_cli, "get_json", lambda url, params=None: {"items": []}
        )
        result = probe_dataset(_info(), "NOPE")
        assert result.status == "error", "no collection -> error"

    def test_no_variables_yields_empty(self, monkeypatch):
        """A collection with no associated variables yields an empty schema."""
        monkeypatch.setattr(
            earthdata_cli,
            "get_json",
            lambda url, params=None: {"items": [{"meta": {"associations": {}}}]},
        )
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert result.status == "ok" and result.assets == {}, "no vars -> {}"


class TestDeepProber:
    """Tests for the credentialed `--deep` sampler (creds/network mocked)."""

    def test_deep_samples_granule(self, monkeypatch):
        """earthdata --deep records a sampled granule's format."""
        monkeypatch.setattr(
            earthdata_cli,
            "_deep_sample",
            lambda sn, v, p: {"g.nc4": {"format": "netcdf4", "output_kind": "raster"}},
        )
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset, deep=True)
        assert result.status == "ok", "earthdata deep probe ran"
        assert result.assets["g.nc4"]["format"] == "netcdf4", "granule format read"

    def test_deep_sample_reads_granule(self, monkeypatch):
        """_deep_sample logs in, searches, and reads a granule link."""
        fake = types.ModuleType("earthaccess")
        fake.login = lambda strategy=None: None
        fake.search_data = lambda **kw: [
            types.SimpleNamespace(data_links=lambda: ["https://h/g.nc4"])
        ]
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        out = earthdata_cli._deep_sample("GPM", "07", "GES_DISC")
        assert out["g.nc4"]["format"] == "netcdf4", "granule format inferred"

    def test_deep_sample_empty(self, monkeypatch):
        """No granules yields an empty schema."""
        fake = types.ModuleType("earthaccess")
        fake.login = lambda strategy=None: None
        fake.search_data = lambda **kw: []
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        assert earthdata_cli._deep_sample("X", "", "") == {}


class TestEmitter:
    """Tests for the Earthdata emitter (public CMR)."""

    def test_infers_format_and_output_kind(self, monkeypatch):
        """The CMR collection seeds format + output_kind."""
        monkeypatch.setattr(
            earthdata_cli,
            "get_json",
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
            _info(),
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
        monkeypatch.setattr(earthdata_cli, "get_json", lambda url, **kw: {"items": []})
        result = emit_stanza(_info(), "GEDI04_A", cmr_provider="ORNL_DAAC")
        assert result.row["output_kind"] == "vector", "GEDI -> vector"


class TestInferOutputKind:
    """Tests for the Earthdata output-kind heuristic."""

    def test_geojson_format_is_vector(self):
        """A geojson format maps to a vector output kind."""
        assert earthdata_cli._infer_output_kind("X", "geojson") == "vector", (
            "geojson -> vector"
        )

    def test_csv_format_is_tabular(self):
        """A csv format maps to a tabular output kind."""
        assert earthdata_cli._infer_output_kind("X", "csv") == "tabular", (
            "csv -> tabular"
        )

    def test_vector_short_name_hint(self):
        """A GEDI short name maps to vector even with no format hint."""
        assert earthdata_cli._infer_output_kind("GEDI02_A") == "vector", (
            "GEDI -> vector"
        )

    def test_default_is_raster(self):
        """An unhinted gridded product defaults to raster."""
        assert earthdata_cli._infer_output_kind("MOD11A1", "cog") == "raster", (
            "default raster"
        )
