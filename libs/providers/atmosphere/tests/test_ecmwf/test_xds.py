"""Unit tests for the XDS (ECMWF Cross Data Store) catalog rows.

The two fire-fuel rows load on the `xds` endpoint and build a request whose
`day` / `time` (and, for the annual burned-area row, `month`) template keys are
dropped by explicit `null` opt-outs rather than by a bespoke `request_kind`.
Variable metadata is live-verified; see
`planning/ecmwf/captures/ecds-xds/c2-real-retrieves-2026-08-16.md`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_XDS = ("derived-fire-fuel-biomass", "projections-fire-fuel-burned-area")


def _backend():
    """A stub ECMWF over a small European window for `_build_request`."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2000-01-01"),
        end_date=pd.Timestamp("2000-01-31"),
        resolution="MS",
        dates=pd.date_range("2000-01-01", "2000-01-31", freq="MS"),
    )
    backend.space = SpatialExtent(
        latitude_min=50.0,
        latitude_max=51.0,
        longitude_min=9.0,
        longitude_max=10.0,
        resolution=0.25,
    )
    backend.temporal_resolution = "monthly"
    return backend


class TestXdsCatalogRows:
    """Catalog shape for the two XDS datasets."""

    @pytest.mark.parametrize("dataset", _XDS)
    def test_dataset_is_curated(self, dataset):
        """Both XDS datasets resolve from the merged catalog."""
        assert dataset in Catalog().datasets

    @pytest.mark.parametrize("dataset", _XDS)
    def test_row_routes_to_the_xds_endpoint(self, dataset):
        """Each row and its variables carry `endpoint: xds`."""
        record = Catalog().datasets[dataset]
        assert record.endpoint == "xds"
        assert all(v.endpoint == "xds" for v in record.variables.values())

    def test_biomass_variable_metadata_is_live_verified(self):
        """The moisture-content row carries its real NetCDF name and unit."""
        variable = Catalog().get_variable(
            "derived-fire-fuel-biomass", "live-fuel-moisture-content-group"
        )
        assert variable.cds_variable == "live_fuel_moisture_content_group"
        assert variable.nc_variable == "LFMC"
        assert variable.units == "%"

    def test_burned_area_variable_metadata_is_live_verified(self):
        """The burned-area row uses the CF dimensionless unit for a fraction."""
        variable = Catalog().get_variable(
            "projections-fire-fuel-burned-area", "burned-area"
        )
        assert variable.cds_variable == "burned_area"
        assert variable.nc_variable == "BAF_pred"
        assert variable.units == "1"

    @pytest.mark.parametrize("dataset", _XDS)
    def test_no_placeholder_units(self, dataset):
        """No XDS variable ships an `unknown` unit placeholder."""
        record = Catalog().datasets[dataset]
        assert all(v.units != "unknown" for v in record.variables.values())


class TestXdsRequestShape:
    """Request building for the XDS rows."""

    def test_biomass_request_drops_day_and_time(self):
        """The monthly biomass row sends neither `day` nor `time`."""
        variable = Catalog().get_variable(
            "derived-fire-fuel-biomass", "live-fuel-moisture-content-group"
        )
        request = _backend()._build_request(variable)
        assert "day" not in request
        assert "time" not in request
        assert request["version"] == ["2"]

    def test_burned_area_request_drops_month_too(self):
        """The annual burned-area row sends no `month`, `day` or `time`."""
        variable = Catalog().get_variable(
            "projections-fire-fuel-burned-area", "burned-area"
        )
        request = _backend()._build_request(variable)
        for key in ("month", "day", "time"):
            assert key not in request
        assert request["experiment"] == ["historical"]
        assert request["version"] == ["1_0"]

    @pytest.mark.parametrize(
        "dataset, variable_name",
        list(zip(_XDS, ("live-fuel-moisture-content-group", "burned-area"))),
    )
    def test_request_carries_area_and_variable(self, dataset, variable_name):
        """Every XDS request still carries the bbox and the CDS variable name."""
        variable = Catalog().get_variable(dataset, variable_name)
        request = _backend()._build_request(variable)
        assert request["variable"] == [variable.cds_variable]
        assert request["area"] == [51.0, 9.0, 50.0, 10.0]
