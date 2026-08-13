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


class TestEcmwfEmitter:
    """Tests for the ECMWF emitter (seeds from the live CADS form.json, mocked)."""

    _HINDCAST_FORM = [
        {"name": "hyear", "details": {}},
        {"name": "hmonth", "details": {}},
        {"name": "hday", "details": {}},
        {"name": "leadtime_hour", "details": {}},
        {
            "name": "variable",
            "details": {"values": ["river_discharge_in_the_last_24_hours"]},
        },
    ]

    def test_seeds_hindcast_row_with_ewds_endpoint(self, monkeypatch):
        """A `hyear`/`hday` form seeds a glofas_hindcast row on the ewds store."""
        monkeypatch.setattr(
            stanza_mod, "_get_json", lambda url, **kw: self._HINDCAST_FORM
        )
        result = emit_stanza(_info("ecmwf"), "cems-glofas-reforecast")
        assert result.status == "ok"
        assert result.row["endpoint"] == "ewds"
        assert result.row["request_kind"] == "glofas_hindcast"
        assert "river-discharge-in-the-last-24-hours" in result.row["variables"]

    def test_cams_date_form_seeds_ads_endpoint(self, monkeypatch):
        """A `date`-range form on a `cams-*` id seeds a cams_date row on ads."""
        form = [
            {"name": "date", "details": {}},
            {"name": "variable", "details": {"values": ["total_column_ozone"]}},
        ]
        monkeypatch.setattr(stanza_mod, "_get_json", lambda url, **kw: form)
        result = emit_stanza(_info("ecmwf"), "cams-global-reanalysis-eac4")
        assert result.status == "ok"
        assert result.row["endpoint"] == "ads"
        assert result.row["request_kind"] == "cams_date"

    def test_fire_form_seeds_fire_not_satellite(self, monkeypatch):
        """A grid + `dataset_type` form (no leadtime_hour) seeds a `fire` row."""
        form = [
            {"name": "dataset_type", "details": {}},
            {"name": "grid", "details": {}},
            {"name": "variable", "details": {"values": ["fire_weather_index"]}},
        ]
        monkeypatch.setattr(stanza_mod, "_get_json", lambda url, **kw: form)
        result = emit_stanza(_info("ecmwf"), "cems-fire-historical-v1")
        assert result.status == "ok"
        assert result.row["request_kind"] == "fire"

    def test_satellite_id_seeds_satellite_cdr(self, monkeypatch):
        """A `satellite-*` id seeds satellite_cdr from its real (grid-less) form."""
        form = [
            {"name": "type_of_sensor", "details": {}},
            {"name": "time_aggregation", "details": {}},
            {"name": "year", "details": {}},
            {"name": "month", "details": {}},
            {"name": "day", "details": {}},
            {
                "name": "variable",
                "details": {"values": ["surface_soil_moisture_volumetric"]},
            },
        ]
        monkeypatch.setattr(stanza_mod, "_get_json", lambda url, **kw: form)
        result = emit_stanza(_info("ecmwf"), "satellite-soil-moisture")
        assert result.row["request_kind"] == "satellite_cdr"

    def test_seeds_every_variable_the_form_exposes(self, monkeypatch):
        """A multi-variable form seeds one row per variable, all as placeholders."""
        form = [
            {"name": "year", "details": {}},
            {"name": "month", "details": {}},
            {"name": "day", "details": {}},
            {"name": "time", "details": {}},
            {
                "name": "variable",
                "details": {"values": ["2m_temperature", "total_precipitation"]},
            },
        ]
        monkeypatch.setattr(stanza_mod, "_get_json", lambda url, **kw: form)
        result = emit_stanza(_info("ecmwf"), "reanalysis-era5-single-levels")
        assert result.status == "ok"
        variables = result.row["variables"]
        assert set(variables) == {"2m-temperature", "total-precipitation"}
        assert variables["2m-temperature"]["cds_variable"] == "2m_temperature"
        assert all(v["units"] == "unknown" for v in variables.values())


class TestEcmwfRequestKind:
    """`_ecmwf_request_kind` maps a form's fields (+ dataset id) to a request kind."""

    @pytest.mark.parametrize(
        "upstream_id, field_names, expected",
        [
            (
                "satellite-soil-moisture",
                ["type_of_sensor", "year", "day"],
                "satellite_cdr",
            ),
            ("cems-glofas-reforecast", ["hyear", "hmonth", "hday"], "glofas_hindcast"),
            ("efas-seasonal-reforecast", ["hyear", "hmonth"], "seasonal_hindcast"),
            ("cams-global-reanalysis-eac4", ["date", "variable"], "cams_date"),
            ("cams-ghg-inversion", ["quantity", "year", "month"], "cams_inversion"),
            (
                "cams-europe-air-quality-reanalyses",
                ["year", "month", "model"],
                "cams_inversion",
            ),
            ("cems-glofas-seasonal", ["leadtime_month", "year", "month"], "seasonal"),
            # A year/month-only form with no leadtime_month is NOT seasonal (was
            # mis-seeded as `seasonal`); a projections-* `model` is not CAMS.
            ("cams-global-emission-inventories", ["year", "month"], "form"),
            ("projections-cmip6", ["year", "month", "model"], "form"),
            (
                "cems-fire-historical-v1",
                ["grid", "dataset_type", "year", "day"],
                "fire",
            ),
            ("cems-fire-seasonal", ["leadtime_hour", "year", "month"], "fire"),
            ("grid-only-cdr", ["grid", "year", "day"], "satellite_cdr"),
            ("reanalysis-era5-single-levels", ["year", "month", "day", "time"], "form"),
        ],
    )
    def test_kind_from_id_and_fields(self, upstream_id, field_names, expected):
        """Each id/field-set combination maps to the documented request kind.

        Args:
            upstream_id: The dataset id (a `satellite-*` id short-circuits).
            field_names: The `form.json` field names present.
            expected: The request kind the heuristic should return.
        """
        form = [{"name": name} for name in field_names]
        result = stanza_mod._ecmwf_request_kind(form, upstream_id)
        assert result == expected, (
            f"{upstream_id}/{field_names} → {result}, want {expected}"
        )

    def test_glofas_forecast_grid_absent_falls_through_to_form(self):
        """A leadtime_hour form with no grid is not misread as a grid kind."""
        form = [{"name": "year"}, {"name": "day"}, {"name": "leadtime_hour"}]
        assert stanza_mod._ecmwf_request_kind(form, "cems-glofas-forecast") == "form"


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

    def test_ecmwf_auto_categorises_target(self, tmp_path, monkeypatch):
        """ecmwf without --target auto-picks the per-family shard from the id."""
        import importlib

        info = _info("ecmwf")
        module = importlib.import_module(f"{info.module}.catalog")
        monkeypatch.setattr(module, "CATALOG_PATH", tmp_path)
        result = StanzaResult(
            "ecmwf",
            "reanalysis-era5-complete",
            "reanalysis-era5-complete",
            "ok",
            row={"endpoint": "cds", "request_kind": "form"},
        )
        written = stanza_mod.write_stanza(info, result, None)
        assert written.endswith("era5.yaml"), "era5 id routed to era5.yaml"
        assert (tmp_path / "era5.yaml").exists(), "the shard file was written"

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
