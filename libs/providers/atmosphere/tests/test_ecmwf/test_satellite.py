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
