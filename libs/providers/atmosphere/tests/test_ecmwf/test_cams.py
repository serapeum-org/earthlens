"""Unit tests for the ADS / CAMS families (greenfield store, C7).

The six CAMS rows load on the ADS endpoint, resolve the CAMS provider, and build
the `cams_date` (date-range) or `cams_inversion` (year/month) request shape.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_CAMS = (
    "cams-global-reanalysis-eac4",
    "cams-global-atmospheric-composition-forecasts",
    "cams-global-fire-emissions-gfas",
    "cams-global-greenhouse-gas-inversion",
    "cams-europe-air-quality-forecasts",
    "cams-europe-air-quality-reanalyses",
)


def _request(dataset):
    """Build the request for a CAMS dataset's first variable."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2023-01-01"),
        end_date=pd.Timestamp("2023-01-02"),
        resolution="D",
        dates=pd.date_range("2023-01-01", "2023-01-02", freq="D"),
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


class TestCamsRows:
    """All six CAMS rows load on ADS with the CAMS provider."""

    @pytest.mark.parametrize("dataset", _CAMS)
    def test_row_loads_on_ads_with_cams_provider(self, dataset):
        """Each CAMS row is on the ads endpoint, copernicus-cams provider."""
        record = Catalog().datasets[dataset]
        assert record.endpoint == "ads"
        assert record.provider == "copernicus-cams"


class TestCamsDateShape:
    """`cams_date` datasets key on a `date` range string, not year/month/day."""

    def test_eac4_builds_date_range_with_time(self):
        """EAC4 sends date='start/stop' + time + netcdf_zip, no year/month/day."""
        request = _request("cams-global-reanalysis-eac4")
        assert request["date"] == "2023-01-01/2023-01-02"
        assert request["time"] == ["00:00"]
        assert request["data_format"] == "netcdf_zip"
        assert not {"year", "month", "day", "product_type"} & set(request)

    def test_gfas_drops_time_and_area(self):
        """GFAS is date-only; its form has no `area` widget, so the bbox is stripped."""
        request = _request("cams-global-fire-emissions-gfas")
        assert "date" in request
        assert "time" not in request
        assert "area" not in request


class TestCamsInversionShape:
    """`cams_inversion` datasets key on year/month, no day/time/area."""

    def test_ghg_inversion_year_month_no_day_area(self):
        """GHG inversion sends year/month + quantity/version, no day/time/area."""
        request = _request("cams-global-greenhouse-gas-inversion")
        assert {"year", "month", "quantity", "version"} <= set(request)
        assert not {"day", "time", "area", "product_type", "data_format"} & set(request)

    def test_air_quality_reanalyses_year_month_model_level(self):
        """European AQ reanalyses send year/month + model/level/type, no day/time."""
        request = _request("cams-europe-air-quality-reanalyses")
        assert {"year", "month", "model", "level", "type"} <= set(request)
        assert not {"day", "time", "area"} & set(request)
