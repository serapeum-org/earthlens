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


class TestEcmwfProbe:
    """Tests for the ECMWF constraints prober (public, no creds)."""

    def test_unions_variables_from_constraints(self, monkeypatch):
        """ecmwf probe unions the `variable` values across constraint rows."""
        monkeypatch.setattr(
            curate_mod,
            "_ecmwf_constraints",
            lambda d: [{"variable": ["2m_temperature", "tp"]}, {"variable": ["tp"]}],
        )
        result = probe_dataset(_info("ecmwf"), "reanalysis-era5-single-levels")
        assert result.status == "ok", "ecmwf probe ran"
        assert sorted(result.assets) == ["2m_temperature", "tp"], "vars unioned"


class TestDeepProbers:
    """Tests for the credentialed `--deep` samplers (creds/network mocked)."""

    def test_ecmwf_deep_reads_retrieved_netcdf(self, monkeypatch):
        """ecmwf --deep reads long_name/units from a retrieved NetCDF."""
        monkeypatch.setattr(
            curate_mod,
            "_ecmwf_deep_sample",
            lambda d: {"t2m": {"long_name": "2 metre temperature", "units": "K"}},
        )
        result = probe_dataset(
            _info("ecmwf"), "reanalysis-era5-single-levels", deep=True
        )
        assert result.status == "ok", "ecmwf deep probe ran"
        assert result.assets["t2m"]["units"] == "K", "retrieved var units read"

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


class TestEcmwfProberBranches:
    """Branch coverage for the ECMWF constraints prober."""

    def test_unions_variables_across_rows(self, monkeypatch):
        """The variable values across all constraint rows are unioned + sorted."""
        monkeypatch.setattr(
            curate_mod,
            "_ecmwf_constraints",
            lambda d: [{"variable": ["t2m", "sp"]}, {"variable": ["t2m", "msl"]}],
        )
        result = probe_dataset(_info("ecmwf"), "reanalysis-era5-single-levels")
        assert sorted(result.assets) == ["msl", "sp", "t2m"], "vars unioned + sorted"

    def test_constraints_helper_delegates(self, monkeypatch):
        """_ecmwf_constraints delegates to the package fetch_constraints."""
        import earthlens.ecmwf.constraints as constraints

        monkeypatch.setattr(
            constraints,
            "fetch_constraints",
            lambda d, base_url=None: [{"variable": []}],
        )
        assert curate_mod._ecmwf_constraints("x") == [{"variable": []}]


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


class TestDeepSamplers:
    """Cover the credentialed deep-sample SDK bodies (SDKs faked)."""

    def test_ecmwf_deep_sample_reads_netcdf(self, monkeypatch):
        """_ecmwf_deep_sample retrieves a tiny NetCDF and reads var metadata."""
        import sys
        import types

        monkeypatch.setattr(
            curate_mod,
            "_ecmwf_constraints",
            lambda d: [{"variable": ["2m_temperature"], "year": ["2020"]}],
        )
        cdsapi = types.ModuleType("cdsapi")
        cdsapi.Client = lambda: types.SimpleNamespace(
            retrieve=lambda ds, req, target: open(target, "w").close()
        )
        monkeypatch.setitem(sys.modules, "cdsapi", cdsapi)
        monkeypatch.setattr(
            curate_mod,
            "_read_netcdf_var_meta",
            lambda path: {"t2m": {"long_name": "2 metre temperature", "units": "K"}},
        )
        out = curate_mod._ecmwf_deep_sample("reanalysis-era5-single-levels")
        assert out["t2m"]["units"] == "K", "retrieved var units read"

    def test_ecmwf_deep_sample_carries_family_selectors(self, monkeypatch):
        """The sampled request forwards every selector of the chosen entry.

        A satellite CDR needs its sensor / version / aggregation selectors, not
        just year/month/day, or CDS rejects the combination. The sampler pins
        one value per enumerated selector and fabricates no key the entry omits.
        """
        import sys
        import types

        entry = {
            "variable": ["surface_soil_moisture"],
            "type_of_sensor": ["passive"],
            "time_aggregation": ["month_average"],
            "version": ["v202212"],
            "year": ["2023"],
            "month": ["01"],
            "day": ["01"],
        }
        monkeypatch.setattr(curate_mod, "_ecmwf_constraints", lambda d: [entry])
        captured: dict[str, object] = {}
        cdsapi = types.ModuleType("cdsapi")
        cdsapi.Client = lambda: types.SimpleNamespace(
            retrieve=lambda ds, req, target: (
                captured.update(req),
                open(target, "w").close(),
            )
        )
        monkeypatch.setitem(sys.modules, "cdsapi", cdsapi)
        monkeypatch.setattr(curate_mod, "_read_netcdf_var_meta", lambda path: {})
        curate_mod._ecmwf_deep_sample("satellite-soil-moisture")
        assert captured["type_of_sensor"] == ["passive"]
        assert captured["version"] == ["v202212"]
        assert captured["time_aggregation"] == ["month_average"]
        assert captured["data_format"] == "netcdf"
        assert "time" not in captured  # the entry enumerates none — fabricate none

    def test_ecmwf_deep_sample_defaults_variable_when_absent(self, monkeypatch):
        """An entry with no variable dimension still sends the widget's `all`."""
        import sys
        import types

        monkeypatch.setattr(
            curate_mod, "_ecmwf_constraints", lambda d: [{"lake": ["achit"]}]
        )
        captured: dict[str, object] = {}
        cdsapi = types.ModuleType("cdsapi")
        cdsapi.Client = lambda: types.SimpleNamespace(
            retrieve=lambda ds, req, target: (
                captured.update(req),
                open(target, "w").close(),
            )
        )
        monkeypatch.setitem(sys.modules, "cdsapi", cdsapi)
        monkeypatch.setattr(curate_mod, "_read_netcdf_var_meta", lambda path: {})
        curate_mod._ecmwf_deep_sample("satellite-lake-water-level")
        assert captured["variable"] == ["all"]
        assert captured["lake"] == ["achit"]

    def test_read_netcdf_var_meta_via_gdal(self, tmp_path):
        """_read_netcdf_var_meta reads long_name/units from a NetCDF via GDAL."""
        import numpy as np
        import xarray as xr

        path = tmp_path / "probe.nc"
        xr.Dataset(
            {
                "t2m": (
                    ("lat", "lon"),
                    np.ones((2, 2), "f4"),
                    {"units": "K", "long_name": "2 metre temperature"},
                )
            },
            coords={"lat": [1.0, 0.0], "lon": [0.0, 1.0]},
        ).to_netcdf(path)
        meta = curate_mod._read_netcdf_var_meta(str(path))
        assert meta["t2m"] == {"long_name": "2 metre temperature", "units": "K"}

    def test_ecmwf_deep_sample_no_constraints(self, monkeypatch):
        """No constraints rows yields an empty schema (after the SDK imports)."""
        import sys
        import types

        monkeypatch.setitem(sys.modules, "cdsapi", types.ModuleType("cdsapi"))
        monkeypatch.setitem(sys.modules, "netCDF4", types.ModuleType("netCDF4"))
        monkeypatch.setattr(curate_mod, "_ecmwf_constraints", lambda d: [])
        assert curate_mod._ecmwf_deep_sample("x") == {}


class TestBiodiversityProbers:
    """Tests for the gbif / obis / wdpa / iucn probers (offline)."""

    def test_cluster_probers_registered(self):
        """All four cluster backends appear in the probe registry."""
        for key in ("gbif", "obis", "wdpa", "iucn"):
            assert key in supported_providers(), f"{key} cluster prober wired"
