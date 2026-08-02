"""Unit + gated-e2e tests for the EFAS suite (European Flood Awareness, C5).

The five EFAS rows load on the EWDS endpoint, resolve the CEMS provider, and
build the request shape each family's live `form.json` declares. The live
retrieve is gated (`-m e2e`) — every EFAS dataset needs its licence accepted.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_EFAS = (
    "efas-forecast",
    "efas-historical",
    "efas-reforecast",
    "efas-seasonal",
    "efas-seasonal-reforecast",
)


def _backend():
    """A stub ECMWF over a small European window for `_build_request`."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2022-01-01"),
        end_date=pd.Timestamp("2022-01-03"),
        resolution="D",
        dates=pd.date_range("2022-01-01", "2022-01-03", freq="D"),
    )
    backend.space = SpatialExtent(
        latitude_min=45.0,
        latitude_max=50.0,
        longitude_min=5.0,
        longitude_max=10.0,
        resolution=0.1,
    )
    backend.temporal_resolution = "daily"
    return backend


def _request(dataset):
    """Build the request for a dataset's first variable."""
    catalog = Catalog()
    var = catalog.get_variable(dataset, next(iter(catalog.datasets[dataset].variables)))
    return _backend()._build_request(var)


class TestEfasRows:
    """The five EFAS rows load on EWDS and resolve the CEMS provider."""

    @pytest.mark.parametrize("dataset", _EFAS)
    def test_row_loads_on_ewds_with_cems_provider(self, dataset):
        """Each EFAS row is present, on the ewds endpoint, CEMS provider."""
        record = Catalog().datasets[dataset]
        assert record.endpoint == "ewds"
        assert record.provider == "copernicus-cems"


class TestEfasRequestShapes:
    """Each EFAS family builds the date-key + time shape its form declares."""

    def test_forecast_keeps_ymd_and_time(self):
        """efas-forecast keys on year/month/day + time + leadtime."""
        request = _request("efas-forecast")
        assert {"year", "month", "day", "time", "leadtime_hour"} <= set(request)
        assert request["originating_centre"] == "ecmwf"

    def test_historical_uses_hindcast_keys_keeps_time_drops_product_type(self):
        """efas-historical remaps to hyear/hmonth/hday, keeps time, no product_type."""
        request = _request("efas-historical")
        assert {"hyear", "hmonth", "hday", "time"} <= set(request)
        assert "product_type" not in request
        assert "year" not in request

    def test_seasonal_reforecast_is_hyear_hmonth_no_day_time(self):
        """efas-seasonal-reforecast keys on hyear/hmonth + leadtime, no day/time."""
        request = _request("efas-seasonal-reforecast")
        assert {"hyear", "hmonth", "leadtime_hour"} <= set(request)
        assert not {"day", "hday", "time", "year", "month"} & set(request)
