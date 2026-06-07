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
        } <= set(supported_providers())


class TestFirmsProbe:
    """Tests for the FIRMS CSV-column prober."""

    def test_columns_and_inferred_dtypes(self, monkeypatch):
        """firms probe reads the CSV header and infers each column's dtype."""
        from earthlens.cli import curate as curate_mod

        monkeypatch.setattr(
            curate_mod,
            "_firms_csv_lines",
            lambda code: ["latitude,frp,satellite", "1.5,10,N"],
        )
        result = probe_dataset(_info("firms"), "VIIRS_SNPP_NRT")
        assert result.status == "ok", "firms probe ran"
        assert result.assets["latitude"]["dtype"] == "float", "float inferred"
        assert result.assets["satellite"]["dtype"] == "str", "str inferred"


class TestHdxProbe:
    """Tests for the HDX resource prober (public CKAN)."""

    def test_lists_resources(self, monkeypatch):
        """hdx probe reads package_show resources into a file/format schema."""
        from earthlens.cli import curate as curate_mod

        monkeypatch.setattr(
            curate_mod,
            "_get_json",
            lambda url, **kw: {
                "result": {
                    "resources": [{"name": "pop.gpkg.gz", "format": "Geopackage"}]
                }
            },
        )
        result = probe_dataset(_info("hdx"), "kontur-population")
        assert result.status == "ok", "hdx probe ran"
        assert result.assets["pop.gpkg.gz"]["format"] == "Geopackage", "resource parsed"


class TestEarthdataProbe:
    """Tests for the Earthdata UMM-Var prober (public CMR)."""

    def test_resolves_collection_then_variables(self, monkeypatch):
        """earthdata probe follows associations.variables to UMM-Var records."""
        from earthlens.cli import curate as curate_mod

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

        monkeypatch.setattr(curate_mod, "_get_json", fake)
        result = probe_dataset(_info("earthdata"), "GPM_3IMERGHH")
        assert result.status == "ok", "earthdata probe ran"
        assert result.assets["precipitation"]["units"] == "mm/hr", "UMM-Var parsed"

    def test_collection_with_no_variables_is_empty(self, monkeypatch):
        """A collection with no associated variables yields an empty schema."""
        from earthlens.cli import curate as curate_mod

        monkeypatch.setattr(
            curate_mod,
            "_get_json",
            lambda url, **kw: {"items": [{"meta": {"associations": {}}}]},
        )
        result = probe_dataset(_info("earthdata"), "SOME_COLLECTION")
        assert result.status == "ok" and result.assets == {}, "empty UMM-Var"


class TestCmemsProbe:
    """Tests for the CMEMS variable prober (SDK describe)."""

    def test_walks_nested_variables(self, monkeypatch):
        """cmems probe flattens the nested products→…→variables to a schema."""
        from types import SimpleNamespace

        from earthlens.cli import curate as curate_mod

        variable = SimpleNamespace(
            short_name="thetao", standard_name="sea_water_temp", units="degC"
        )
        service = SimpleNamespace(variables=[variable])
        part = SimpleNamespace(services=[service])
        version = SimpleNamespace(parts=[part])
        entry = SimpleNamespace(versions=[version])
        catalogue = SimpleNamespace(products=[SimpleNamespace(datasets=[entry])])
        monkeypatch.setattr(
            curate_mod, "_cmems_describe_dataset", lambda dataset_id: catalogue
        )
        result = probe_dataset(_info("cmems"), "cmems_mod_glo_phy")
        assert result.status == "ok", "cmems probe ran"
        assert result.assets["thetao"]["units"] == "degC", "variable units parsed"


class TestSentinelHubProbe:
    """Tests for the Sentinel Hub band prober (offline SDK)."""

    def test_resolves_curated_key_to_bands(self):
        """A curated key resolves to the SDK collection's bands (offline)."""
        result = probe_dataset(_info("sentinel_hub"), "sentinel-2-l2a")
        assert result.status == "ok", f"sentinel_hub probe failed: {result.detail}"
        assert "B04" in result.assets, "Sentinel-2 bands probed"
        assert result.assets["B04"]["units"], "band units recorded"


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


class TestEumetsatProbe:
    """Tests for the EUMETSAT browse prober (public, no auth)."""

    def test_reads_browse_metadata(self, monkeypatch):
        """eumetsat probe reads the public browse title/abstract/date."""
        monkeypatch.setattr(
            curate_mod,
            "_get_json",
            lambda url, **kw: {
                "collection": {"properties": {"title": "HRSEVIRI", "date": "2020"}}
            },
        )
        result = probe_dataset(_info("eumetsat"), "EO:EUM:DAT:MSG:HRSEVIRI")
        assert result.status == "ok", "eumetsat probe ran"
        entry = next(iter(result.assets.values()))
        assert entry["title"] == "HRSEVIRI", "title parsed"


class TestWorldpopProbe:
    """Tests for the WorldPop REST prober (public)."""

    def test_samples_record_fields(self, monkeypatch):
        """worldpop probe records each REST record field's dtype + popyears."""
        monkeypatch.setattr(
            curate_mod,
            "_worldpop_records",
            lambda alias, sub, iso3: [
                {"id": 1, "title": "t", "popyear": "2020"},
                {"id": 2, "popyear": "2021"},
            ],
        )
        info = _info("worldpop")
        from earthlens.cli.adapter import load_catalog

        alias = next(iter(load_catalog(info).datasets))
        result = probe_dataset(info, alias)
        assert result.status == "ok", "worldpop probe ran"
        assert result.assets["popyears"]["values"] == ["2020", "2021"], "years unioned"


class TestOvertureProbe:
    """Tests for the Overture column prober (public SDK)."""

    def test_reads_column_dtypes(self, monkeypatch):
        """overture probe records each column's dtype from a tiny bbox."""
        monkeypatch.setattr(
            curate_mod,
            "_overture_columns",
            lambda overture_type: {"id": "object", "height": "float64"},
        )
        info = _info("overture")
        from earthlens.cli.adapter import load_catalog

        key = next(iter(load_catalog(info).datasets))
        result = probe_dataset(info, key)
        assert result.status == "ok", "overture probe ran"
        assert result.assets["height"]["dtype"] == "float64", "dtype recorded"


class TestS3Probe:
    """Tests for the S3 bucket prober (unsigned boto3)."""

    def test_lists_sample_keys(self, monkeypatch):
        """s3 probe lists a few object keys under the dataset's bucket."""
        monkeypatch.setattr(
            curate_mod, "_s3_sample_keys", lambda b, p, region: ["a/2020.tif"]
        )
        info = _info("s3")
        from earthlens.cli.adapter import load_catalog

        key = next(iter(load_catalog(info).datasets))
        result = probe_dataset(info, key)
        assert result.status == "ok", "s3 probe ran"
        assert "a/2020.tif" in result.assets, "object key listed"


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

    def test_cmems_deep_reads_netcdf_vars(self, monkeypatch):
        """cmems --deep reads the real NetCDF variable schema."""
        monkeypatch.setattr(
            curate_mod,
            "_cmems_deep_sample",
            lambda dsid: {"thetao": {"units": "degC", "dtype": "float32"}},
        )
        result = probe_dataset(_info("cmems"), "cmems_mod_glo_phy", deep=True)
        assert result.status == "ok", "cmems deep probe ran"
        assert result.assets["thetao"]["units"] == "degC", "real var units read"

    def test_earthdata_deep_samples_granule(self, monkeypatch):
        """earthdata --deep records a sampled granule's format."""
        from earthlens.cli.adapter import load_catalog

        monkeypatch.setattr(
            curate_mod,
            "_earthdata_deep_sample",
            lambda sn, v, p: {"g.nc4": {"format": "netcdf4", "output_kind": "raster"}},
        )
        dataset = next(iter(load_catalog(_info("earthdata")).datasets))
        result = probe_dataset(_info("earthdata"), dataset, deep=True)
        assert result.status == "ok", "earthdata deep probe ran"
        assert result.assets["g.nc4"]["format"] == "netcdf4", "granule format read"

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
        monkeypatch.setattr(curate_mod, "_get_json", lambda url: _SAMPLE_ITEM)
        result = probe_dataset(_info("stac"), "sentinel-2-l2a", deep=True)
        assert result.status == "ok", "stac --deep fell back to the light prober"

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
