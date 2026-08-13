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


class TestChcProbe:
    """Tests for the CHC FTP-sample prober (anonymous FTP)."""

    def test_lists_sample_filenames(self, monkeypatch):
        """chc probe lists a sample of filenames under the dataset's ftp_base."""
        from earthlens.cli.adapter import load_catalog

        monkeypatch.setattr(
            curate_mod, "_chc_sample_files", lambda base, limit=10: ["a.tif", "b.tif"]
        )
        dataset = next(iter(load_catalog(_info("chc")).datasets))
        result = probe_dataset(_info("chc"), dataset)
        assert result.status == "ok", "chc probe ran"
        assert "a.tif" in result.assets, "sample filename listed"

    def test_suggests_a_filename_pattern(self, monkeypatch):
        """chc probe adds a (suggested pattern) row inferred from the listing."""
        from earthlens.cli.adapter import load_catalog

        monkeypatch.setattr(
            curate_mod,
            "_chc_sample_files",
            lambda base, limit=10: ["chirps-v2.0.2009.01.01.tif"],
        )
        dataset = next(iter(load_catalog(_info("chc")).datasets))
        result = probe_dataset(_info("chc"), dataset)
        suggestion = result.assets.get("(suggested pattern)", {}).get("pattern", "")
        assert suggestion == "chirps-v2.0.{year}.{month}.{day}.tif", suggestion

    def test_suggest_pattern_empty_listing(self):
        """The pattern suggester returns empty for an empty listing."""
        assert curate_mod._suggest_pattern([]) == ""


class TestTropycalProbe:
    """Tests for the Tropycal basin prober (SDK)."""

    def test_reads_field_schema(self, monkeypatch):
        """tropycal probe records the to_dataframe() field dtypes."""
        from earthlens.cli.adapter import load_catalog

        monkeypatch.setattr(
            curate_mod, "_tropycal_fields", lambda b, s: {"vmax": {"dtype": "int64"}}
        )
        basin = next(iter(load_catalog(_info("tropycal")).datasets))
        result = probe_dataset(_info("tropycal"), basin)
        assert result.status == "ok", "tropycal probe ran"
        assert result.assets["vmax"]["dtype"] == "int64", "field dtype recorded"


class TestNwpProbe:
    """Tests for the NWP `.idx` band prober (Herbie template, no eccodes)."""

    def test_reports_band_presence(self, monkeypatch):
        """nwp probe flags which catalog band tokens appear in the live .idx."""
        from earthlens.cli.adapter import load_catalog

        catalog = load_catalog(_info("nwp"))
        model_key = next(
            key
            for key, model in catalog.datasets.items()
            if getattr(model, "model_family", None)
            not in curate_mod._NWP_NO_IDX_FAMILIES | curate_mod._NWP_NEEDS_EXTRA_ATTRS
            and (getattr(model, "bands", None) or {})
        )
        token = next(iter(catalog.datasets[model_key].bands.values()))
        monkeypatch.setattr(
            curate_mod, "_nwp_idx_body", lambda model: f"1:0:d=x:{token}:surface:\n"
        )
        result = probe_dataset(_info("nwp"), model_key)
        assert result.status == "ok", "nwp probe ran"
        assert any(v["present"] for v in result.assets.values()), "a band present"

    def test_no_idx_family_is_error(self):
        """An ECCC model (no .idx) reports 'error' with the reason."""
        from earthlens.cli.adapter import load_catalog

        catalog = load_catalog(_info("nwp"))
        eccc = next(
            (
                key
                for key, model in catalog.datasets.items()
                if getattr(model, "model_family", None)
                in curate_mod._NWP_NO_IDX_FAMILIES
            ),
            None,
        )
        if eccc is None:
            pytest.skip("no ECCC model in the catalog")
        result = probe_dataset(_info("nwp"), eccc)
        assert result.status == "error" and "no .idx" in result.detail


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

    def test_nwp_availability_direct_https_builds_url(self, monkeypatch):
        """_nwp_availability HEADs the first band's URL for a direct-https model."""
        import datetime as dt

        from earthlens.nwp.catalog import NWPModel

        calls = {}

        class _Resp:
            status_code = 200

        def fake_head(url, timeout=None, allow_redirects=None):
            calls["url"] = url
            return _Resp()

        monkeypatch.setattr(curate_mod.requests, "head", fake_head)
        model = NWPModel(
            provider="dwd-opendata",
            backend="direct-https",
            cycles_utc=[0],
            url_template="https://x/{var_lc}/f{step:03d}_{var}.bz2",
            bands={"temperature_2m": "T_2M"},
        )
        result = curate_mod._nwp_availability(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "HTTP 200" in result and calls["url"] == "https://x/t_2m/f000_T_2M.bz2"

    def test_nwp_availability_herbie_unavailable(self, monkeypatch):
        """_nwp_availability reports herbie missing rather than raising."""
        import datetime as dt
        import sys

        from earthlens.nwp.catalog import NWPModel

        monkeypatch.setitem(sys.modules, "herbie", None)
        model = NWPModel(provider="noaa-nodd", model_family="gfs", backend="herbie")
        result = curate_mod._nwp_availability(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "herbie unavailable" in result

    def test_nwp_deep_reports_live_availability(self, monkeypatch):
        """nwp --deep reports the model's live availability for a recent cycle."""
        from earthlens.cli.adapter import load_catalog

        monkeypatch.setattr(
            curate_mod, "_nwp_availability", lambda model, cycle, step: "HTTP 200 (ok)"
        )
        catalog = load_catalog(_info("nwp"))
        model_key = next(
            key
            for key, model in catalog.datasets.items()
            if getattr(model, "backend", None) == "direct-https"
        )
        result = probe_dataset(_info("nwp"), model_key, deep=True)
        assert result.status == "ok", "nwp deep probe ran"
        entry = next(iter(result.assets.values()))
        assert "HTTP 200" in entry["status"], "availability status reported"


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


class TestNwpHelpers:
    """Tests for the NWP availability dispatch + cycle helpers."""

    def test_recent_cycle_is_in_the_past(self):
        """_nwp_recent_cycle returns a datetime at or before ~now."""
        import datetime as dt

        from earthlens.nwp.catalog import NWPModel

        cycle = curate_mod._nwp_recent_cycle(
            NWPModel(provider="p", backend="direct-https", cycles_utc=[0, 12])
        )
        assert cycle <= dt.datetime.now(dt.UTC).replace(tzinfo=None), "cycle in past"

    def test_availability_unknown_backend(self):
        """An unrecognised backend reports that no probe exists."""
        import datetime as dt
        from types import SimpleNamespace

        model = SimpleNamespace(backend="mystery", bands={}, request_options={})
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "no live availability probe" in out, "unknown backend reported"

    def test_availability_direct_boto3_head_object(self, monkeypatch):
        """A direct-boto3 model HEADs the object and reports its size."""
        import datetime as dt
        import sys
        import types

        from earthlens.nwp.catalog import NWPModel

        boto3 = types.ModuleType("boto3")
        botocore = types.ModuleType("botocore")
        botocore_client = types.ModuleType("botocore.client")
        botocore.UNSIGNED = object()
        botocore_client.Config = lambda **kw: None

        class FakeS3:
            def head_object(self, Bucket, Key):
                return {"ContentLength": 1234}

        boto3.client = lambda *a, **kw: FakeS3()
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        monkeypatch.setitem(sys.modules, "botocore", botocore)
        monkeypatch.setitem(sys.modules, "botocore.client", botocore_client)
        model = NWPModel(
            provider="p",
            backend="direct-boto3",
            bands={"t": "T"},
            request_options={"bucket": "b", "key_template": "{var}.grib"},
        )
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "1234 bytes" in out, "head_object size reported"


class TestChcSampleFiles:
    """Tests for the anonymous-FTP CHC sampler."""

    def test_lists_directory(self, monkeypatch):
        """_chc_sample_files logs in, cds to the base, and returns sorted names."""
        import earthlens.cli.curate as cm

        class FakeFTP:
            def __init__(self, host, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self):
                pass

            def cwd(self, base):
                pass

            def nlst(self):
                return ["b.tif", "a.tif"]

        monkeypatch.setattr(cm, "FTP", FakeFTP, raising=False)
        import ftplib

        monkeypatch.setattr(ftplib, "FTP", FakeFTP)
        assert cm._chc_sample_files("/x", limit=1) == ["a.tif"], "sorted + capped"

    def test_availability_meteofrance_needs_key(self, monkeypatch):
        """A meteofrance model with no API key reports the missing-credential."""
        import datetime as dt

        from earthlens.nwp.catalog import NWPModel

        monkeypatch.delenv("METEO_FRANCE_API_KEY", raising=False)
        monkeypatch.delenv("MF_API_KEY", raising=False)
        model = NWPModel(
            provider="mf",
            backend="meteofrance-api",
            request_options={"api_base": "https://x", "coverage_service": "svc"},
        )
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "METEO_FRANCE_API_KEY" in out, "missing key reported"

    def test_availability_direct_boto3_missing_options(self):
        """A direct-boto3 model lacking bucket/key/bands reports the gap."""
        import datetime as dt

        from earthlens.nwp.catalog import NWPModel

        model = NWPModel(provider="p", backend="direct-boto3")
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "bucket" in out, "missing options reported"


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


class TestNwpAvailabilityBackends:
    """Cover the remaining _nwp_availability backend branches (SDKs faked)."""

    def test_ecmwf_opendata_latest(self, monkeypatch):
        """The ecmwf-opendata backend reports the latest cycle from the client."""
        import datetime as dt
        import sys
        import types

        from earthlens.nwp.catalog import NWPModel

        opendata = types.ModuleType("ecmwf.opendata")
        opendata.Client = lambda source=None, model=None: types.SimpleNamespace(
            latest=lambda **kw: dt.datetime(2024, 6, 1, 0)
        )
        monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
        monkeypatch.setitem(sys.modules, "ecmwf.opendata", opendata)
        model = NWPModel(
            provider="p", backend="ecmwf-opendata", request_options={"type": "fc"}
        )
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "latest cycle" in out, "latest cycle reported"

    def test_herbie_resolves_grib(self, monkeypatch):
        """The herbie backend reports the resolved GRIB path."""
        import datetime as dt
        import sys
        import types

        from earthlens.nwp.catalog import NWPModel

        herbie = types.ModuleType("herbie")
        herbie.Herbie = lambda cycle, **kw: types.SimpleNamespace(grib="s3://x.grib")
        monkeypatch.setitem(sys.modules, "herbie", herbie)
        model = NWPModel(provider="p", backend="herbie", model_family="gfs")
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "resolved" in out, "GRIB path reported"

    def test_direct_https_unreachable(self, monkeypatch):
        """A direct-https HEAD failure is reported as unreachable, not raised."""
        import datetime as dt

        from earthlens.nwp.catalog import NWPModel

        def boom(url, timeout=None, allow_redirects=None):
            raise RuntimeError("dns")

        monkeypatch.setattr(curate_mod.requests, "head", boom)
        model = NWPModel(
            provider="p",
            backend="direct-https",
            url_template="https://x/{var}",
            bands={"t": "T"},
        )
        out = curate_mod._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "unreachable" in out, "HEAD failure reported"

    def test_tropycal_fields_samples_season(self, monkeypatch):
        """_tropycal_fields samples a season's storms and records column dtypes."""
        import sys
        import types

        class _Frame:
            columns = ["vmax", "mslp"]

            def __getitem__(self, key):
                return types.SimpleNamespace(dtype="int64")

        td = types.SimpleNamespace(
            get_season=lambda year: types.SimpleNamespace(
                summary=lambda: {"id": ["AL012020"]}
            ),
            get_storm=lambda sid: types.SimpleNamespace(
                to_dataframe=lambda attrs_as_columns=False: _Frame()
            ),
        )
        tropycal = types.ModuleType("tropycal")
        tracks = types.ModuleType("tropycal.tracks")
        tracks.TrackDataset = lambda basin=None, source=None: td
        monkeypatch.setitem(sys.modules, "tropycal", tropycal)
        monkeypatch.setitem(sys.modules, "tropycal.tracks", tracks)
        out = curate_mod._tropycal_fields("north_atlantic", "hurdat")
        assert out["vmax"]["dtype"] == "int64", "column dtype recorded"


class TestNwpIdx:
    """Cover the Herbie `.idx` URL + body helpers (runpy / requests mocked)."""

    def test_idx_url_from_template(self, monkeypatch):
        """_nwp_idx_url evaluates the template against a stub to recover the URL."""
        import datetime as dt
        import pathlib
        import runpy

        from earthlens.nwp.catalog import NWPModel

        class _Tmpl:
            @staticmethod
            def template(stub):
                stub.SOURCES = {"aws": "https://aws/file"}

        monkeypatch.setattr(runpy, "run_path", lambda p: {"gfs": _Tmpl})
        model = NWPModel(
            provider="p", backend="direct-https", model_family="gfs", product=""
        )
        url = curate_mod._nwp_idx_url(
            pathlib.Path("/x"), model, dt.datetime(2024, 1, 1), 0
        )
        assert url == "https://aws/file.idx", "aws source + .idx suffix"

    def test_idx_body_returns_reachable_text(self, monkeypatch):
        """_nwp_idx_body returns the first reachable cycle's .idx text."""
        import pathlib
        import types

        from earthlens.nwp.catalog import NWPModel

        monkeypatch.setattr(
            curate_mod, "_herbie_models_dir", lambda: pathlib.Path("/x")
        )
        monkeypatch.setattr(curate_mod, "_nwp_idx_url", lambda md, m, c, s: "https://x")
        monkeypatch.setattr(
            curate_mod.requests,
            "get",
            lambda url, timeout=None: types.SimpleNamespace(
                status_code=200, text="1:0:VAR:\n"
            ),
        )
        model = NWPModel(
            provider="p", backend="direct-https", horizon_h=6, bands={"t": "VAR"}
        )
        assert curate_mod._nwp_idx_body(model) == "1:0:VAR:\n", "idx text returned"

    def test_idx_body_unreachable_raises(self, monkeypatch):
        """When no cycle is reachable, _nwp_idx_body raises ValueError."""
        import pathlib

        from earthlens.nwp.catalog import NWPModel

        monkeypatch.setattr(
            curate_mod, "_herbie_models_dir", lambda: pathlib.Path("/x")
        )
        monkeypatch.setattr(curate_mod, "_nwp_idx_url", lambda md, m, c, s: "https://x")

        def boom(url, timeout=None):
            raise RuntimeError("offline")

        monkeypatch.setattr(curate_mod.requests, "get", boom)
        model = NWPModel(provider="p", backend="direct-https", bands={"t": "VAR"})
        with pytest.raises(ValueError, match="no recent"):
            curate_mod._nwp_idx_body(model)

    def test_idx_url_rejects_unsafe_model_family(self):
        """A model_family that is not a bare identifier is refused before runpy."""
        import pathlib

        from earthlens.nwp.catalog import NWPModel

        model = NWPModel(
            provider="p", backend="direct-https", model_family="../evil", product=""
        )
        with pytest.raises(ValueError, match="unsafe model_family"):
            curate_mod._nwp_idx_url(pathlib.Path("/x"), model, "2024-01-01", 0)


class TestBiodiversityProbers:
    """Tests for the gbif / obis / wdpa / iucn probers (offline)."""

    def test_cluster_probers_registered(self):
        """All four cluster backends appear in the probe registry."""
        for key in ("gbif", "obis", "wdpa", "iucn"):
            assert key in supported_providers(), f"{key} cluster prober wired"
