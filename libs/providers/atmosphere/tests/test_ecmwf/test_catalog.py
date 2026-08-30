"""Unit tests for :class:`earthlens.ecmwf.Catalog`.

Covers the H2 / H5 rewiring (the catalog reads
`cds_data_catalog.yaml` and exposes per-variable
:class:`Variable` instances), the M2 fail-loud behaviour on
malformed YAML, and the no-MARS-keys invariant on the schema.
"""

from __future__ import annotations

import pydantic
import pytest

from earthlens.ecmwf import Catalog, Variable
from earthlens.ecmwf.catalog import (
    _TEMPORAL_AGGREGATE_TOKENS,
    _denotes_temporal_aggregate,
)

pytestmark = [pytest.mark.unit]


class TestCatalog:
    """Tests for :class:`Catalog` after the H2 / H5 / M1 / M2 work."""

    @pytest.mark.parametrize(
        "dataset_name, var_code, expected_variable",
        [
            (
                "reanalysis-era5-single-levels",
                "2m-temperature",
                "2m_temperature",
            ),
            (
                "reanalysis-era5-single-levels",
                "total-precipitation",
                "total_precipitation",
            ),
            (
                "reanalysis-era5-single-levels",
                "surface-pressure",
                "surface_pressure",
            ),
            (
                "reanalysis-era5-single-levels",
                "evaporation",
                "evaporation",
            ),
            (
                "reanalysis-era5-pressure-levels",
                "temperature",
                "temperature",
            ),
        ],
    )
    def test_get_variable_returns_new_schema(
        self, dataset_name, var_code, expected_variable
    ):
        """`get_variable(dataset, code)` returns the row for that pair."""
        spec = Catalog().get_variable(dataset_name, var_code)
        assert spec.cds_dataset == dataset_name
        assert spec.cds_variable == expected_variable

    def test_get_variable_returns_raw_era5_units(self):
        """2m-temperature carries the raw ERA5 unit (Kelvin)."""
        spec = Catalog().get_variable("reanalysis-era5-single-levels", "2m-temperature")
        assert spec.units == "K"

    def test_available_datasets_lists_cds_collection(self):
        """available_datasets exposes the informational dataset list."""
        cat = Catalog()
        assert isinstance(cat.available_datasets, list)
        assert "reanalysis-era5-single-levels" in cat.available_datasets
        assert len(cat.available_datasets) > 100

    def test_datasets_groups_variables_under_their_cds_dataset(self):
        """datasets nests each Variable under its parent CDS dataset."""
        cat = Catalog()
        single = cat.datasets["reanalysis-era5-single-levels"]
        assert single.monthly == "reanalysis-era5-single-levels-monthly-means"
        assert "2m-temperature" in single.variables
        assert single.variables["2m-temperature"].cds_dataset == (
            "reanalysis-era5-single-levels"
        )

    def test_pressure_level_var_carries_cds_pressure_level(self):
        """Pressure-level variables expose `cds_pressure_level`.

        T, Q, R live on reanalysis-era5-pressure-levels; their
        catalog entries must carry the `cds_pressure_level`
        attribute so :meth:`ECMWF._api` can forward it to CDS.
        """
        spec = Catalog().get_variable("reanalysis-era5-pressure-levels", "temperature")
        assert spec.cds_pressure_level == ["1000"]

    def test_get_variable_raises_key_error_for_unknown_dataset(self):
        """Unknown dataset names raise `KeyError`."""
        with pytest.raises(KeyError):
            Catalog().get_variable("bogus-dataset", "2m-temperature")

    def test_get_variable_raises_key_error_for_unknown_code(self):
        """Unknown variable codes (under a real dataset) raise `KeyError`."""
        with pytest.raises(KeyError):
            Catalog().get_variable(
                "reanalysis-era5-single-levels", "DEFINITELY_NOT_A_REAL_CODE"
            )

    def test_same_code_under_different_datasets_is_distinct(self):
        """`(dataset, code)` is the identity; same code, different datasets."""
        cat = Catalog()
        single = cat.get_variable("reanalysis-era5-single-levels", "2m-temperature")
        land = cat.get_variable("reanalysis-era5-land", "2m-temperature")
        assert single.cds_dataset == "reanalysis-era5-single-levels"
        assert land.cds_dataset == "reanalysis-era5-land"
        assert single is not land

    def test_get_dataset_returns_dataset_object(self):
        """`get_dataset(name)` returns the structural :class:`Dataset`."""
        cat = Catalog()
        ds = cat.get_dataset("reanalysis-era5-pressure-levels")
        assert ds.monthly == "reanalysis-era5-pressure-levels-monthly-means"
        assert "temperature" in ds.variables

    def test_get_dataset_raises_value_error_with_hint(self):
        """Unknown dataset names raise `ValueError` (with a did-you-mean hint when close)."""
        with pytest.raises(ValueError, match="is not in the CDS catalog"):
            Catalog().get_dataset("definitely-not-a-dataset")

    def test_no_mars_schema_keys_remain(self):
        """No Variable field is a stale MARS-style key."""
        forbidden = {"number_para", "download type", "var_name"}
        present = set(Variable.model_fields)
        assert not (forbidden & present)

    @pytest.mark.parametrize("mars_key", ["number_para", "download type", "var_name"])
    def test_no_mars_schema_keys_in_extras(self, monkeypatch, tmp_path, mars_key):
        """Legacy MARS keys are rejected inside `extras`."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    monthly: x\n"
            "    monthly_product_type: [monthly_averaged_reanalysis]\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n"
            "        extras:\n"
            f"          {mars_key!r}: '1'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        with pytest.raises(ValueError, match="legacy MARS keys"):
            Catalog()

    def test_unknown_top_level_key_still_fails_validation(self, monkeypatch, tmp_path):
        """An unknown key on a Variable row fails the catalog loader with the row name."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n"
            "        totally_unknown: boom\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        with pytest.raises(ValueError, match="totally_unknown"):
            Catalog()

    def test_duplicate_variable_in_same_dataset_rejected(self, monkeypatch, tmp_path):
        """Two `variables:` entries with the same code in one dataset fail.

        PyYAML's default loader silently merges duplicate mapping
        keys (last-wins). The catalog's strict loader must instead
        raise so a copy-paste typo in the YAML cannot let the second
        entry shadow the first.
        """
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n"
            "      2m-temperature:\n"
            "        nc_variable: foo\n"
            "        units: bar\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        with pytest.raises(ValueError, match="duplicate YAML key"):
            Catalog()

    def test_duplicate_dataset_name_rejected(self, monkeypatch, tmp_path):
        """Two top-level dataset entries with the same name fail loud."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n"
            "  reanalysis-era5-single-levels:\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      total-precipitation:\n"
            "        nc_variable: tp\n"
            "        units: m\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        with pytest.raises(ValueError, match="duplicate YAML key"):
            Catalog()

    def test_monthly_without_monthly_product_type_raises(self, monkeypatch, tmp_path):
        """`monthly:` without `monthly_product_type:` fails auto-synthesis."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    monthly: reanalysis-era5-single-levels-monthly-means\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        with pytest.raises(ValueError, match="monthly_product_type"):
            Catalog()

    def test_extras_propagate_from_parent_dataset(self, monkeypatch, tmp_path):
        """Parent `Dataset.extras` propagates into each child Variable."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-carra-single-levels:\n"
            "    product_type: [analysis]\n"
            "    extras:\n"
            "      domain: east\n"
            "      leadtime_hour: '1'\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        cat = Catalog()
        spec = cat.get_variable("reanalysis-carra-single-levels", "2m-temperature")
        assert spec.extras == {"domain": "east", "leadtime_hour": "1"}
        assert cat.datasets["reanalysis-carra-single-levels"].extras == {
            "domain": "east",
            "leadtime_hour": "1",
        }

    def test_row_extras_override_parent_extras(self, monkeypatch, tmp_path):
        """A per-row `extras:` key wins over the parent default."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  reanalysis-carra-single-levels:\n"
            "    product_type: [analysis]\n"
            "    extras:\n"
            "      domain: east\n"
            "      leadtime_hour: '1'\n"
            "    variables:\n"
            "      2m-temperature:\n"
            "        nc_variable: t2m\n"
            "        units: K\n"
            "        extras:\n"
            "          domain: west\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        cat = Catalog()
        spec = cat.get_variable("reanalysis-carra-single-levels", "2m-temperature")
        assert spec.extras == {"domain": "west", "leadtime_hour": "1"}

    def test_era5_land_loads(self):
        """ERA5-Land block round-trips through `Catalog`.

        Asserts the dataset is exposed under `datasets`, carries the
        correct monthly-aggregate variant, and that one of its
        unique-to-ERA5-Land rows resolves to the expected metadata.
        """
        cat = Catalog()
        ds = cat.datasets["reanalysis-era5-land"]
        assert ds.monthly == "reanalysis-era5-land-monthly-means"
        assert "evaporation-from-bare-soil" in ds.variables
        spec = ds.variables["evaporation-from-bare-soil"]
        assert spec.cds_dataset == "reanalysis-era5-land"
        assert spec.cds_variable == "evaporation_from_bare_soil"
        assert spec.nc_variable == "evabs"
        assert spec.units == "m of water equivalent"
        assert spec.types == "flux"

    def test_era5_land_carries_60_variables(self):
        """ERA5-Land covers all 60 variables CDS reports for the dataset."""
        ds = Catalog().datasets["reanalysis-era5-land"]
        assert len(ds.variables) == 60

    def test_derived_era5_land_daily_statistics_loads(self):
        """derived-era5-land-daily-statistics block round-trips through `Catalog`."""
        cat = Catalog()
        ds = cat.datasets["derived-era5-land-daily-statistics"]
        assert ds.monthly is None
        assert len(ds.variables) == 31
        # Parent extras carry the request defaults required by the dataset.
        assert ds.extras == {
            "daily_statistic": "daily_mean",
            "frequency": "1_hourly",
            "time_zone": "utc+00:00",
        }
        spec = ds.variables["2m-temperature-daily"]
        assert spec.cds_dataset == "derived-era5-land-daily-statistics"
        assert spec.cds_variable == "2m_temperature"
        assert spec.nc_variable == "t2m"
        assert spec.units == "K"
        # Per-variable extras inherit the parent defaults.
        assert spec.extras == {
            "daily_statistic": "daily_mean",
            "frequency": "1_hourly",
            "time_zone": "utc+00:00",
        }

    def test_era5_daily_statistics_load(self):
        """Both ERA5 daily-statistics datasets round-trip through `Catalog`."""
        cat = Catalog()
        single = cat.datasets["derived-era5-single-levels-daily-statistics"]
        press = cat.datasets["derived-era5-pressure-levels-daily-statistics"]
        assert len(single.variables) == 262
        assert len(press.variables) == 16
        assert single.extras["daily_statistic"] == "daily_mean"
        assert press.extras["frequency"] == "1_hourly"
        assert press.pressure_level == ["1000"]
        # Spot-check a known mapping in each
        spec = single.variables["2m-temperature-daily"]
        assert spec.cds_variable == "2m_temperature"
        assert spec.nc_variable == "t2m"
        assert spec.units == "K"
        spec = press.variables["temperature-daily"]
        assert spec.cds_variable == "temperature"
        assert spec.nc_variable == "t"
        assert spec.cds_pressure_level == ["1000"]

    def test_is_pre_aggregated_flags_daily_statistics_and_monthly_means(self):
        """`Variable.is_pre_aggregated` is True for the two pre-aggregated families (#43).

        Daily-statistics variables carry a `daily_statistic` extra and monthly-means
        variables a `monthly_averaged_*` product type; a raw hourly ERA5 variable
        (same physical quantity, same `types: flux`) is not pre-aggregated, so
        `op="auto"` still sums it.
        """
        cat = Catalog()
        daily_tp = cat.datasets[
            "derived-era5-single-levels-daily-statistics"
        ].variables["total-precipitation-daily"]
        monthly_tp = cat.datasets[
            "reanalysis-era5-single-levels-monthly-means"
        ].variables["total-precipitation"]
        raw_tp = cat.datasets["reanalysis-era5-single-levels"].variables[
            "total-precipitation"
        ]
        # All three are flux; only the pre-aggregated two must flag.
        assert daily_tp.is_flux, "daily-statistics tp should be flux"
        assert monthly_tp.is_flux, "monthly-means tp should be flux"
        assert raw_tp.is_flux, "raw ERA5 tp should be flux"
        assert daily_tp.is_pre_aggregated is True, "daily-statistics flux var"
        assert monthly_tp.is_pre_aggregated is True, "monthly-means flux var"
        assert raw_tp.is_pre_aggregated is False, "raw hourly ERA5 flux var"

    @pytest.mark.parametrize(
        "dataset, code",
        [
            ("ecv-for-climate-change", "precipitation-ecv"),
            (
                "projections-cmip5-monthly-single-levels",
                "mean-precipitation-flux-cmip5m",
            ),
            (
                "projections-cordex-domains-single-levels",
                "mean-precipitation-flux-cordex",
            ),
            ("reanalysis-carra-means", "10m-wind-gust-carra-means"),
            ("reanalysis-pan-carra-means", "evaporation-pancarra-means"),
            ("seasonal-monthly-single-levels", "total-precipitation-seasonal"),
        ],
    )
    def test_is_pre_aggregated_flags_extra_and_monthly_id_families(self, dataset, code):
        """Flux vars flagged via a temporal-aggregate extra or a -monthly id (#1097)."""
        var = Catalog().datasets[dataset].variables[code]
        assert var.is_flux, f"{dataset}::{code} should be flux"
        assert var.is_pre_aggregated is True, (
            f"{dataset}::{code} should be pre-aggregated"
        )

    @pytest.mark.parametrize(
        "dataset, code",
        [
            ("derived-near-surface-meteorological-variables", "rainfall-flux-nsmv"),
            ("reanalysis-cerra-single-levels", "evaporation-cerra"),
            ("reanalysis-carra-single-levels", "percolation-carra"),
            ("seasonal-original-single-levels", "evaporation-seasonal-orig"),
            ("reanalysis-era5-single-levels", "total-precipitation"),
        ],
    )
    def test_is_pre_aggregated_does_not_flag_raw_flux_datasets(self, dataset, code):
        """Raw flux datasets stay unflagged so `op="auto"` still sums them."""
        var = Catalog().datasets[dataset].variables[code]
        assert var.is_flux, f"{dataset}::{code} should be flux"
        assert var.is_pre_aggregated is False, (
            f"{dataset}::{code} must NOT be pre-aggregated"
        )

    def test_minimal_valid_request_picks_entry_with_variable(self, monkeypatch):
        """`minimal_valid_request` returns a known-valid request dict."""
        from earthlens.ecmwf import constraints as constraints_module

        constraints_module._CACHE.clear()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                import json

                return json.dumps(
                    [
                        # Entry without variables — should be skipped
                        {"experiment": ["historical"], "year": ["2000"]},
                        # Entry with variables — should be picked
                        {
                            "variable": ["2m_temperature", "skin_temperature"],
                            "year": ["2022"],
                            "month": ["01"],
                            "level_type": ["surface_or_atmosphere"],
                        },
                    ]
                ).encode("utf-8")

        monkeypatch.setattr(
            constraints_module.urllib.request,
            "urlopen",
            lambda *_a, **_kw: _Resp(),
        )
        request = Catalog().minimal_valid_request("ds")
        assert request["data_format"] == "netcdf"
        assert request["variable"] == ["2m_temperature"]
        assert request["year"] == ["2022"]
        assert request["month"] == ["01"]
        assert request["level_type"] == ["surface_or_atmosphere"]

    def test_minimal_valid_request_falls_back_for_no_variable_datasets(
        self, monkeypatch
    ):
        """For datasets without a `variable` field, return the first entry."""
        from earthlens.ecmwf import constraints as constraints_module

        constraints_module._CACHE.clear()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                import json

                # No entry has `variable` — caller gets first entry expanded
                return json.dumps(
                    [
                        {"cdr_type": ["esa_cci"], "region": ["nh"]},
                        {"cdr_type": ["osi_saf"], "region": ["sh"]},
                    ]
                ).encode("utf-8")

        monkeypatch.setattr(
            constraints_module.urllib.request,
            "urlopen",
            lambda *_a, **_kw: _Resp(),
        )
        request = Catalog().minimal_valid_request("satellite-no-variable-ds")
        assert request["cdr_type"] == ["esa_cci"]
        assert request["region"] == ["nh"]

    def test_minimal_valid_request_empty_constraints(self, monkeypatch):
        """Empty constraints return a near-empty request (just data_format)."""
        from earthlens.ecmwf import constraints as constraints_module

        constraints_module._CACHE.clear()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b"[]"

        monkeypatch.setattr(
            constraints_module.urllib.request,
            "urlopen",
            lambda *_a, **_kw: _Resp(),
        )
        request = Catalog().minimal_valid_request("non-addressable")
        assert request == {"data_format": "netcdf"}

    def test_list_recent_jobs_filters_by_age_and_status(self, monkeypatch, tmp_path):
        """`list_recent_jobs` returns jobs within `max_age_min` only."""
        import datetime

        rc = tmp_path / ".cdsapirc"
        rc.write_text("url: https://example.invalid/api\nkey: tok\n", encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        recent = (now - datetime.timedelta(minutes=10)).isoformat()
        old = (now - datetime.timedelta(minutes=120)).isoformat()
        payload = {
            "jobs": [
                {
                    "jobID": "abc",
                    "processID": "ds-a",
                    "status": "successful",
                    "created": recent,
                },
                {
                    "jobID": "def",
                    "processID": "ds-b",
                    "status": "successful",
                    "created": old,
                },
            ]
        }

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        import earthlens.ecmwf.catalog as cat_mod

        captured = {}

        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

        import requests as _req

        monkeypatch.setattr(_req, "get", _fake_get)
        jobs = Catalog().list_recent_jobs(status="successful", max_age_min=60)
        assert len(jobs) == 1
        assert jobs[0]["jobID"] == "abc"
        assert captured["params"]["status"] == "successful"

    def test_download_job_skips_if_target_exists(self, monkeypatch, tmp_path):
        """`download_job` is idempotent when the target file is already there."""
        rc = tmp_path / ".cdsapirc"
        rc.write_text("url: https://example.invalid/api\nkey: tok\n", encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        target = tmp_path / "x.nc"
        target.write_bytes(b"already here")
        # No mocked HTTP — proves we don't reach for the network.
        result = Catalog().download_job("any-job-id", target)
        assert result == target
        assert target.read_bytes() == b"already here"

    def test_download_job_raises_when_no_asset_href(self, monkeypatch, tmp_path):
        """`download_job` raises ValueError when results lack an asset href."""
        rc = tmp_path / ".cdsapirc"
        rc.write_text("url: https://example.invalid/api\nkey: tok\n", encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        class _Resp:
            status_code = 200
            headers: dict[str, str] = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"asset": {"value": {}}}  # no href

            def close(self):
                pass

        import requests as _req

        monkeypatch.setattr(_req, "get", lambda *a, **kw: _Resp())
        with pytest.raises(ValueError, match="no downloadable asset href"):
            Catalog().download_job("xyz", tmp_path / "out.nc")

    def test_describe_returns_dataset_metadata(self):
        """`Catalog.describe` returns a structured introspection record."""
        cat = Catalog()
        info = cat.describe("reanalysis-era5-land")
        assert info["dataset"] == "reanalysis-era5-land"
        assert info["monthly"] == "reanalysis-era5-land-monthly-means"
        assert info["pressure_level"] is None
        assert info["extras"] == {}
        assert "2m-temperature" in info["variables"]
        assert len(info["variables"]) == 60

    def test_describe_includes_parent_extras(self):
        """`describe` surfaces the dataset-level extras (e.g. ORAS5)."""
        info = Catalog().describe("reanalysis-oras5")
        assert info["extras"] == {"product_type": ["consolidated"]}
        assert len(info["variables"]) == 27

    def test_describe_raises_for_unknown_dataset(self):
        """Unknown dataset names propagate `get_dataset`'s `ValueError`."""
        with pytest.raises(ValueError, match="is not in the CDS catalog"):
            Catalog().describe("definitely-not-a-dataset")

    def test_era5_land_monthly_means_synthesized(self):
        """ERA5-Land's `monthly:` link materializes a sibling catalog entry.

        The catalog loader auto-synthesizes a first-class entry under
        the monthly dataset name, with each Variable's `cds_dataset`
        rebranded and `product_type` flipped to the monthly flavor.
        """
        cat = Catalog()
        parent = cat.datasets["reanalysis-era5-land"]
        assert parent.monthly == "reanalysis-era5-land-monthly-means"
        # Sibling entry exists under the monthly name.
        monthly_ds = cat.datasets["reanalysis-era5-land-monthly-means"]
        spec = monthly_ds.variables["2m-temperature"]
        assert spec.cds_dataset == "reanalysis-era5-land-monthly-means"
        assert spec.product_type == ["monthly_averaged_reanalysis"]
        # Daily row is unaffected.
        daily_spec = parent.variables["2m-temperature"]
        assert daily_spec.cds_dataset == "reanalysis-era5-land"
        assert daily_spec.product_type == ["reanalysis"]

    def test_era5_pressure_levels_monthly_means_synthesized(self):
        """ERA5 pressure-levels monthly sibling is synthesized correctly."""
        cat = Catalog()
        parent = cat.datasets["reanalysis-era5-pressure-levels"]
        assert parent.monthly == "reanalysis-era5-pressure-levels-monthly-means"
        monthly_ds = cat.datasets["reanalysis-era5-pressure-levels-monthly-means"]
        spec = monthly_ds.variables["temperature"]
        assert spec.cds_dataset == "reanalysis-era5-pressure-levels-monthly-means"
        assert spec.product_type == ["monthly_averaged_reanalysis"]
        assert spec.cds_pressure_level == ["1000"]

    def test_era5_single_levels_monthly_means_synthesized(self):
        """ERA5 single-levels monthly sibling is synthesized correctly."""
        cat = Catalog()
        parent = cat.datasets["reanalysis-era5-single-levels"]
        assert parent.monthly == "reanalysis-era5-single-levels-monthly-means"
        monthly_ds = cat.datasets["reanalysis-era5-single-levels-monthly-means"]
        spec = monthly_ds.variables["2m-temperature"]
        assert spec.cds_dataset == "reanalysis-era5-single-levels-monthly-means"
        assert spec.product_type == ["monthly_averaged_reanalysis"]

    def test_carra_means_partial_loads(self):
        """CARRA-means partial block (6 forecast-based single-level vars + 1 analysis_based override)."""
        cat = Catalog()
        ds = cat.datasets["reanalysis-carra-means"]
        assert ds.request_kind == "carra_means"
        assert ds.extras["product_type"] == ["forecast_based"]
        assert ds.extras["time_aggregation"] == "daily"
        assert len(ds.variables) == 112
        spec = ds.variables["maximum-2m-temperature-carra-means"]
        assert (
            spec.cds_variable == "maximum_2m_temperature_since_previous_post_processing"
        )
        assert spec.nc_variable == "mx2t"
        assert spec.units == "K"
        # Per-row extras override: analysis_based var flips product_type.
        analysis_spec = ds.variables["2m-specific-humidity-carra-means"]
        assert analysis_spec.extras["product_type"] == ["analysis_based"]

    def test_seasonal_monthly_single_partial_loads(self):
        """Seasonal monthly-single partial block (11 of 38 vars)."""
        cat = Catalog()
        ds = cat.datasets["seasonal-monthly-single-levels"]
        assert ds.extras["originating_centre"] == "ecmwf"
        assert ds.extras["system"] == "5"
        assert len(ds.variables) == 38
        spec = ds.variables["2m-temperature-seasonal"]
        assert spec.cds_variable == "2m_temperature"
        assert spec.nc_variable == "t2m"

    def test_carra_loads(self):
        """CARRA family: pressure, height, model, single-levels round-trip."""
        cat = Catalog()
        press = cat.datasets["reanalysis-carra-pressure-levels"]
        height = cat.datasets["reanalysis-carra-height-levels"]
        model = cat.datasets["reanalysis-carra-model-levels"]
        single = cat.datasets["reanalysis-carra-single-levels"]
        assert len(press.variables) == 14
        assert len(height.variables) == 7
        assert len(model.variables) == 11
        assert len(single.variables) == 67
        # Parent extras propagate to every variable.
        for ds in [press, height, model, single]:
            assert ds.extras["domain"] == "east_domain"
            assert ds.extras["product_type"] == ["analysis"]
        # Spot-check: CARRA pressure-levels temperature -> t (K)
        spec = press.variables["temperature-carra"]
        assert spec.cds_variable == "temperature"
        assert spec.nc_variable == "t"
        assert spec.cds_pressure_level == ["1000"]
        # Height-levels variables carry the height_level extra.
        spec = height.variables["temperature-carra-h"]
        assert spec.extras["height_level"] == ["100_m"]
        # Model-levels carry the model_level extra.
        spec = model.variables["temperature-carra-m"]
        assert spec.extras["model_level"] == ["1"]

    def test_cmip5_monthly_loads(self):
        """CMIP5 monthly single-levels + pressure-levels round-trip."""
        cat = Catalog()
        single = cat.datasets["projections-cmip5-monthly-single-levels"]
        press = cat.datasets["projections-cmip5-monthly-pressure-levels"]
        assert len(single.variables) == 9
        assert len(press.variables) == 5
        assert single.extras["model"] == "ec_earth"
        assert single.extras["experiment"] == "historical"
        spec = single.variables["2m-temperature-cmip5m"]
        assert spec.cds_variable == "2m_temperature"
        assert spec.nc_variable == "tas"
        spec = press.variables["temperature-cmip5m"]
        assert spec.cds_variable == "temperature"
        assert spec.nc_variable == "ta"
        assert spec.cds_pressure_level == ["1000"]

    def test_cordex_loads(self):
        """CORDEX block round-trips through `Catalog`.

        Asserts the dataset is exposed, the parent extras carry the
        EURO-CORDEX EC-Earth/RACMO22E historical defaults, and a
        sample variable resolves to its CMOR short name.
        """
        cat = Catalog()
        ds = cat.datasets["projections-cordex-domains-single-levels"]
        assert ds.extras["domain"] == "europe"
        assert ds.extras["gcm_model"] == "ichec_ec_earth"
        assert ds.extras["rcm_model"] == "knmi_racmo22e"
        assert ds.extras["experiment"] == "historical"
        spec = ds.variables["2m-air-temperature-cordex"]
        assert spec.cds_dataset == "projections-cordex-domains-single-levels"
        assert spec.cds_variable == "2m_air_temperature"
        assert spec.nc_variable == "tas"
        assert spec.units == "K"
        # Parent extras propagate to every variable row.
        assert spec.extras["gcm_model"] == "ichec_ec_earth"

    def test_cordex_carries_16_confirmed_variables(self):
        """CORDEX ships 16 of 25 catalogued variables (probe-confirmed)."""
        ds = Catalog().datasets["projections-cordex-domains-single-levels"]
        assert len(ds.variables) == 16
        # All variable keys end with the `-cordex` suffix to avoid
        # colliding with the same-named ERA5 single-levels rows.
        for code in ds.variables:
            assert code.endswith("-cordex")

    def test_oras5_loads(self):
        """ORAS5 ocean reanalysis block round-trips through `Catalog`.

        Asserts the dataset is exposed under `datasets`, has no
        monthly variant (it is monthly-only by design), and a known
        single-level variable resolves to the expected NEMO short name.
        """
        cat = Catalog()
        ds = cat.datasets["reanalysis-oras5"]
        assert ds.monthly is None
        assert len(ds.variables) == 27
        spec = ds.variables["sea-ice-thickness"]
        assert spec.cds_dataset == "reanalysis-oras5"
        assert spec.cds_variable == "sea_ice_thickness"
        assert spec.nc_variable == "iicethic"
        assert spec.units == "m"
        assert spec.extras["vertical_resolution"] == "single_level"
        assert spec.extras["product_type"] == ["consolidated"]

    def test_oras5_carries_oceanic_monthly_request_kind(self):
        """ORAS5 declares `request_kind=oceanic_monthly` so `_api()` can
        strip ERA5-specific defaults at retrieve time."""
        cat = Catalog()
        ds = cat.datasets["reanalysis-oras5"]
        assert ds.request_kind == "oceanic_monthly"
        # Propagated to every variable row.
        for var in ds.variables.values():
            assert var.request_kind == "oceanic_monthly"

    def test_default_request_kind_is_form(self):
        """Datasets that don't set `request_kind` default to `form`."""
        cat = Catalog()
        ds = cat.datasets["reanalysis-era5-single-levels"]
        assert ds.request_kind == "form"
        spec = ds.variables["2m-temperature"]
        assert spec.request_kind == "form"

    def test_oras5_all_levels_variables(self):
        """ORAS5's six 3-D fields override the parent default with all_levels."""
        ds = Catalog().datasets["reanalysis-oras5"]
        all_levels_vars = {
            code: var
            for code, var in ds.variables.items()
            if var.extras["vertical_resolution"] == "all_levels"
        }
        assert set(all_levels_vars) == {
            "meridional-velocity",
            "potential-temperature",
            "rotated-meridional-velocity",
            "rotated-zonal-velocity",
            "salinity",
            "zonal-velocity",
        }
        assert all_levels_vars["potential-temperature"].nc_variable == "votemper"
        assert all_levels_vars["salinity"].nc_variable == "vosaline"

    def test_era5_land_snow_depth_uses_sde_not_sd(self):
        """ERA5-Land's snow_depth maps to `sde` (m), not `sd` (m water equiv).

        ERA5-Land returns physical snow thickness as `sde` while
        single-levels uses `sd` for the water-equivalent depth. The
        two are distinct fields and must not collide in the catalog.
        """
        cat = Catalog()
        land_sd = cat.datasets["reanalysis-era5-land"].variables["snow-depth"]
        assert land_sd.nc_variable == "sde"
        assert land_sd.units == "m"
        land_sdwe = cat.datasets["reanalysis-era5-land"].variables[
            "snow-depth-water-equivalent"
        ]
        assert land_sdwe.nc_variable == "sd"
        assert land_sdwe.units == "m of water equivalent"

    def test_extras_roundtrip_through_yaml(self, monkeypatch, tmp_path):
        """Arbitrary extras survive a YAML load-and-read round trip."""
        from earthlens.ecmwf import catalog as catalog_module

        catalog_yaml = tmp_path / "cds_data_catalog.yaml"
        catalog_yaml.write_text(
            "datasets:\n"
            "  projections-cmip6:\n"
            "    product_type: [reanalysis]\n"
            "    extras:\n"
            "      experiment: ssp585\n"
            "      model: ec_earth3\n"
            "      temporal_resolution: monthly\n"
            "    variables:\n"
            "      near-surface-air-temperature:\n"
            "        nc_variable: tas\n"
            "        units: K\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog_module, "CATALOG_PATH", catalog_yaml)
        spec = Catalog().get_variable(
            "projections-cmip6", "near-surface-air-temperature"
        )
        assert spec.extras == {
            "experiment": "ssp585",
            "model": "ec_earth3",
            "temporal_resolution": "monthly",
        }

    def test_get_catalog_raises_on_empty_datasets(self, monkeypatch, tmp_path):
        """A YAML with no datasets raises ValueError."""
        empty_yaml = tmp_path / "cds_data_catalog.yaml"
        empty_yaml.write_text("version: 3\navailable_datasets: []\n", encoding="utf-8")
        from earthlens.ecmwf import catalog as catalog_module

        monkeypatch.setattr(catalog_module, "CATALOG_PATH", empty_yaml)
        with pytest.raises(ValueError, match="datasets"):
            Catalog()

    def test_get_catalog_raises_on_null_datasets(self, monkeypatch, tmp_path):
        """A YAML with datasets: null also raises ValueError."""
        null_yaml = tmp_path / "cds_data_catalog.yaml"
        null_yaml.write_text("datasets:\n", encoding="utf-8")
        from earthlens.ecmwf import catalog as catalog_module

        monkeypatch.setattr(catalog_module, "CATALOG_PATH", null_yaml)
        with pytest.raises(ValueError, match="datasets"):
            Catalog()

    def test_get_catalog_raises_when_no_variables_anywhere(self, monkeypatch, tmp_path):
        """A YAML with datasets but no variables under any of them raises."""
        no_vars = tmp_path / "cds_data_catalog.yaml"
        no_vars.write_text(
            "datasets:\n"
            "  reanalysis-era5-single-levels:\n"
            "    monthly: x\n"
            "    monthly_product_type: [monthly_averaged_reanalysis]\n"
            "    product_type: [reanalysis]\n"
            "    variables:\n",
            encoding="utf-8",
        )
        from earthlens.ecmwf import catalog as catalog_module

        monkeypatch.setattr(catalog_module, "CATALOG_PATH", no_vars)
        with pytest.raises(ValueError, match="no variables"):
            Catalog()

    def test_directory_rejects_duplicate_dataset_key(self, monkeypatch, tmp_path):
        """A dataset key declared in two family files raises ValueError."""
        a = tmp_path / "a.yaml"
        a.write_text(
            "datasets:\n  dup-ds:\n    variables:\n      v1:\n        nc_variable: a\n",
            encoding="utf-8",
        )
        b = tmp_path / "b.yaml"
        b.write_text(
            "datasets:\n  dup-ds:\n    variables:\n      v2:\n        nc_variable: b\n",
            encoding="utf-8",
        )
        from earthlens.ecmwf import catalog as catalog_module

        monkeypatch.setattr(catalog_module, "CATALOG_PATH", tmp_path)
        with pytest.raises(ValueError, match="duplicate dataset key"):
            Catalog()

    def test_missing_catalog_path_raises(self, monkeypatch, tmp_path):
        """A CATALOG_PATH that is neither a dir nor a file raises ValueError."""
        from earthlens.ecmwf import catalog as catalog_module

        monkeypatch.setattr(catalog_module, "CATALOG_PATH", tmp_path / "nope")
        with pytest.raises(ValueError, match="does not exist"):
            Catalog()


class TestGlofasIntermediate:
    """The GloFAS historical intermediate stream and its cds_dataset override."""

    _DISCHARGE = "average-river-discharge-in-the-last-24-hours"

    def test_intermediate_row_mirrors_consolidated_variables(self):
        """The intermediate row exposes the same four variables as consolidated."""
        cat = Catalog()
        inter = cat.datasets["cems-glofas-historical-intermediate"]
        cons = cat.datasets["cems-glofas-historical"]
        assert set(inter.variables) == set(cons.variables)
        assert inter.product_type == ["intermediate"]

    def test_discharge_variable_is_live_verified(self):
        """The discharge variable carries the live-verified avg_dis / m3 s-1."""
        v = Catalog().get_variable(
            "cems-glofas-historical-intermediate", self._DISCHARGE
        )
        assert v.nc_variable == "avg_dis"
        assert v.units == "m3 s-1"

    def test_cds_dataset_override_routes_to_consolidated(self):
        """Every intermediate variable retrieves from cems-glofas-historical."""
        inter = Catalog().datasets["cems-glofas-historical-intermediate"]
        for v in inter.variables.values():
            assert v.cds_dataset == "cems-glofas-historical"
            assert v.dataset_id == "cems-glofas-historical-intermediate"

    def test_output_stem_differs_from_consolidated(self):
        """Intermediate and consolidated discharge write to distinct files."""
        cat = Catalog()
        inter = cat.get_variable("cems-glofas-historical-intermediate", self._DISCHARGE)
        cons = cat.get_variable("cems-glofas-historical", self._DISCHARGE)
        assert inter.cds_dataset == cons.cds_dataset, "same retrieve target"
        assert inter.cds_variable == cons.cds_variable, "same CDS variable"
        assert f"{inter.cds_variable}_{inter.dataset_id}" != (
            f"{cons.cds_variable}_{cons.dataset_id}"
        ), "output stems must not collide"

    def test_ordinary_row_dataset_id_equals_cds_dataset(self):
        """A row without an override has dataset_id == cds_dataset."""
        v = Catalog().get_variable("reanalysis-era5-single-levels", "2m-temperature")
        assert v.dataset_id == v.cds_dataset == "reanalysis-era5-single-levels"


class TestCatalogHealth:
    """Tests for the Catalog.health() self-check."""

    def test_health_keys(self):
        """health() returns exactly the four defect / usage lists."""
        assert set(Catalog().health()) == {
            "variable_missing_nc_variable",
            "dataset_without_variables",
            "unregistered_provider",
            "unused_provider",
        }

    def test_shipped_catalog_has_no_defects(self):
        """The shipped catalog carries no missing-nc / empty / unregistered rows."""
        report = Catalog().health()
        assert report["variable_missing_nc_variable"] == [], "every var has an nc name"
        assert report["dataset_without_variables"] == [], "no empty datasets"
        assert report["unregistered_provider"] == [], "every provider is registered"

    def test_unused_provider_is_a_sorted_list(self):
        """unused_provider is an informational sorted list of registered-but-unused."""
        unused = Catalog().health()["unused_provider"]
        assert isinstance(unused, list)
        assert unused == sorted(unused), "unused providers are reported sorted"


class TestDenotesTemporalAggregate:
    """Tests for the `_denotes_temporal_aggregate` catalog helper (#1097)."""

    @pytest.mark.parametrize(
        "value",
        [
            "1_month_mean",
            "monthly_mean",
            "monthly",
            "daily",
            "1_day_mean",
            "seasonal",
            "annual",
            "yearly",
            "dekadal",
            "pentad",
            "weekly",
            "climatology",
            "time-average",
        ],
    )
    def test_daily_or_coarser_values_are_aggregates(self, value):
        """Daily-or-coarser mean strings are detected as aggregates."""
        assert _denotes_temporal_aggregate(value) is True, (
            f"{value!r} should count as a temporal aggregate"
        )

    @pytest.mark.parametrize("token", _TEMPORAL_AGGREGATE_TOKENS)
    def test_every_declared_token_triggers_detection(self, token):
        """Each declared token, on its own, marks a value as an aggregate."""
        assert _denotes_temporal_aggregate(token) is True, (
            f"declared token {token!r} should be detected"
        )

    @pytest.mark.parametrize("value", [None, "", [], (), 0, False])
    def test_empty_values_are_not_aggregates(self, value):
        """Falsy / empty inputs short-circuit to False."""
        assert _denotes_temporal_aggregate(value) is False, (
            f"empty value {value!r} should not be an aggregate"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "instantaneous",
            "instant",
            "1_hour",
            "hourly",
            "6_hourly",
            "3-hourly",
            "sub-daily",
            "sub_daily",
            "subdaily",
        ],
    )
    def test_sub_daily_and_instantaneous_values_are_not_aggregates(self, value):
        """Instantaneous / sub-daily values are treated as raw, not aggregates."""
        assert _denotes_temporal_aggregate(value) is False, (
            f"sub-daily/instant value {value!r} should not be an aggregate"
        )

    @pytest.mark.parametrize(
        "value",
        ["hourly_mean", "6_hourly_mean", "instantaneous_mean"],
    )
    def test_sub_daily_veto_wins_over_an_aggregate_token(self, value):
        """The sub-daily veto wins even when a mean token co-occurs."""
        assert _denotes_temporal_aggregate(value) is False, (
            f"{value!r} is sub-daily; the hour/instant veto must win"
        )

    @pytest.mark.parametrize("value", ["raw", "forecast", "analysis", "reanalysis"])
    def test_values_without_a_token_are_not_aggregates(self, value):
        """Strings carrying none of the tokens are not aggregates."""
        assert _denotes_temporal_aggregate(value) is False, (
            f"{value!r} carries no aggregate token"
        )

    @pytest.mark.parametrize("value", ["subseasonal", "subseasonal_mean"])
    def test_coarse_sub_cadence_is_not_vetoed(self, value):
        """A coarser-than-daily `sub*` cadence (subseasonal) is not vetoed as sub-daily."""
        assert _denotes_temporal_aggregate(value) is True, (
            f"{value!r} is coarser than daily and should flag"
        )

    @pytest.mark.parametrize("value", ["sum", "running_sum", "accumulated_sum"])
    def test_sum_type_values_are_not_aggregates(self, value):
        """A value whose only signal is a sum is not flagged (the flag forces mean)."""
        assert _denotes_temporal_aggregate(value) is False, (
            f"{value!r} is a sum, not a mean; it must not be flagged as pre-aggregated"
        )

    @pytest.mark.parametrize(
        "value, expected",
        [
            (["monthly_mean"], True),
            (["daily"], True),
            (("1_month_mean",), True),
            (["1", "daily"], True),
            (["instantaneous"], False),
            (["1_hour"], False),
            (["raw"], False),
        ],
    )
    def test_accepts_list_and_tuple_values(self, value, expected):
        """Scalar, list, and tuple forms of the extra are all handled."""
        assert _denotes_temporal_aggregate(value) is expected, (
            f"{value!r} should resolve to {expected}"
        )

    def test_detection_is_case_insensitive(self):
        """Detection is case-insensitive (values are lower-cased first)."""
        assert _denotes_temporal_aggregate("MONTHLY_MEAN") is True, "upper aggregate"
        assert _denotes_temporal_aggregate("Daily") is True, "mixed-case aggregate"
        assert _denotes_temporal_aggregate("1_HOUR") is False, "upper sub-daily vetoed"

    def test_tokens_constant_is_lowercase_tuple(self):
        """`_TEMPORAL_AGGREGATE_TOKENS` is a non-empty tuple of lowercase tokens."""
        assert isinstance(_TEMPORAL_AGGREGATE_TOKENS, tuple), "tokens are a tuple"
        assert _TEMPORAL_AGGREGATE_TOKENS, "tokens list is non-empty"
        assert all(t == t.lower() for t in _TEMPORAL_AGGREGATE_TOKENS), (
            "every token is lowercase so matching against lowercased text works"
        )


class TestUnhydratableField:
    """The catalog can say a row is terminal rather than merely unfilled."""

    def test_it_defaults_to_none(self):
        """Every existing row keeps meaning 'pending' without being touched."""
        from earthlens.ecmwf.catalog import Variable

        row = Variable(
            cds_dataset="d",
            cds_variable="v",
            nc_variable="n",
            units="K",
            product_type=["reanalysis"],
        )
        assert row.unhydratable is None

    def test_every_unfilled_pseudo_slug_row_is_marked(self):
        """Both spellings count; a filled one needs no mark, having resolved."""
        from earthlens.ecmwf import Catalog

        unmarked = [
            f"{name}/{slug}"
            for name, dataset in Catalog().datasets.items()
            for slug, row in dataset.variables.items()
            if slug in {"all", "all-variables"}
            and row.units == "unknown"
            and not row.unhydratable
        ]
        assert not unmarked, f"pseudo-slug rows left indistinguishable: {unmarked}"

    @pytest.mark.parametrize("typo", ["pseudo_slug", "pseudoslug", "Pseudo-Slug"])
    def test_a_misspelt_reason_is_refused(self, typo):
        """A truthy typo would skip the row for good and read as deliberate."""
        from earthlens.ecmwf.catalog import Variable

        with pytest.raises(pydantic.ValidationError):
            Variable.model_validate(
                {
                    "cds_dataset": "a-dataset",
                    "cds_variable": "a-variable",
                    "nc_variable": "v",
                    "units": "unknown",
                    "unhydratable": typo,
                }
            )

    def test_a_resolved_pseudo_slug_row_is_left_alone(self):
        """A dataset serving one variable resolves `all`, so the mark would lie."""
        from earthlens.ecmwf import Catalog

        catalog = Catalog()
        for name in ("satellite-precipitation", "satellite-sea-surface-temperature"):
            row = catalog.datasets[name].variables["all"]
            assert row.units != "unknown", f"{name}/all is expected to be hydrated"
            assert row.unhydratable is None, (
                f"{name}/all resolved to {row.nc_variable!r}, so it is not terminal"
            )
