"""Unit tests for the satellite CDR families (C8).

The drought/hydrology CDRs load on CDS, build the `satellite_cdr` shape
(year/month/day + per-CDR selectors, no time-of-day, no `data_format`).
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_CDRS = (
    "satellite-soil-moisture",
    "satellite-precipitation",
    "satellite-sea-surface-temperature",
)


def _request(dataset):
    """Build the request for a CDR dataset's first variable."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2023-01-01"),
        end_date=pd.Timestamp("2023-01-01"),
        resolution="D",
        dates=pd.date_range("2023-01-01", "2023-01-01", freq="D"),
    )
    backend.space = SpatialExtent(
        latitude_min=0.0,
        latitude_max=1.0,
        longitude_min=0.0,
        longitude_max=1.0,
        resolution=0.1,
    )
    backend.temporal_resolution = "daily"
    catalog = Catalog()
    var = catalog.get_variable(dataset, next(iter(catalog.datasets[dataset].variables)))
    return backend._build_request(var)


class TestSatelliteRows:
    """The CDR rows load on CDS with the satellite_cdr kind."""

    @pytest.mark.parametrize("dataset", _CDRS)
    def test_row_loads_on_cds(self, dataset):
        """Each CDR row is on the cds endpoint with the satellite_cdr kind."""
        record = Catalog().datasets[dataset]
        assert record.endpoint == "cds"
        assert record.request_kind == "satellite_cdr"


class TestSatelliteRequestShape:
    """`satellite_cdr` builds year/month/day + selectors, no time/data_format."""

    @pytest.mark.parametrize("dataset", _CDRS)
    def test_no_time_no_data_format(self, dataset):
        """A CDR request keys on year/month/day, no time-of-day, no data_format."""
        request = _request(dataset)
        assert {"year", "month", "day"} <= set(request)
        assert "time" not in request
        assert "data_format" not in request

    def test_soil_moisture_carries_sensor_and_version(self):
        """soil-moisture sends the ESA-CCI sensor/record/version selectors."""
        request = _request("satellite-soil-moisture")
        assert request["type_of_sensor"] == ["passive"]
        assert request["type_of_record"] == ["icdr"]
        assert request["version"] == ["v202212"]

    @pytest.mark.parametrize(
        "dataset", ["satellite-soil-moisture", "satellite-sea-surface-temperature"]
    )
    def test_no_area_when_form_lacks_the_widget(self, dataset):
        """A CDR whose form has no `area` widget strips the template bbox."""
        assert "area" not in _request(dataset)

    def test_precipitation_keeps_area(self):
        """satellite-precipitation has an `area` widget, so it keeps the bbox."""
        assert "area" in _request("satellite-precipitation")


#: Newly curated CDRs whose extras were derived from live `constraints.json`.
_CURATED = (
    "satellite-aerosol-properties",
    "satellite-albedo",
    "satellite-carbon-dioxide",
    "satellite-cloud-properties",
    "satellite-sea-ice-drift",
    "satellite-total-column-water-vapour-ocean",
)


class TestCuratedSatelliteRows:
    """The constraints-derived CDRs carry their family selectors + shape."""

    @pytest.mark.parametrize("dataset", _CURATED)
    def test_extras_reach_the_request(self, dataset):
        """Each curated row forwards non-empty catalog extras into the request."""
        extras = Catalog().datasets[dataset].extras
        assert extras, f"{dataset} has no curated extras"
        request = _request(dataset)
        # Every non-null extras selector lands in the built request; a
        # `None` opt-out drops its key instead.
        for key, value in extras.items():
            if value is None:
                assert key not in request, f"{dataset}: {key} should be dropped"
            else:
                assert key in request, f"{dataset}: {key} missing from request"

    @pytest.mark.parametrize("dataset", _CURATED)
    def test_no_time_no_data_format(self, dataset):
        """A curated CDR strips the time-of-day and the data_format choice."""
        request = _request(dataset)
        assert "time" not in request
        assert "data_format" not in request

    def test_carbon_dioxide_strips_all_dates(self):
        """The obs4mips CO2 record is not date-partitioned: no year/month/day."""
        request = _request("satellite-carbon-dioxide")
        assert not {"year", "month", "day"} & set(request)
        assert request["sensor_and_algorithm"] == ["merged_obs4mips"]
        assert request["version"] == ["3"]

    def test_albedo_uses_nominal_day(self):
        """satellite-albedo keys on `nominal_day`, not the template `day`."""
        request = _request("satellite-albedo")
        assert request["nominal_day"] == ["03"]
        assert "day" not in request

    def test_aerosol_pins_monthly_nominal_day(self):
        """A monthly aerosol request pins `day` to the nominal 01, no bbox."""
        request = _request("satellite-aerosol-properties")
        assert request["day"] == ["01"]
        assert request["time_aggregation"] == ["monthly_average"]
        assert "area" not in request
