"""Unit tests for the curated other.yaml request extras.

The sis / insitu / projections / reanalysis-uerra / oras5-timeseries placeholder
rows had their family selectors and temporal shape derived from the live
constraints.json rows[0]. These tests build each row's request offline and
prove the curated extras land in the request with the right date / time /
format shape.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]


def _request(dataset):
    """Build the request for a dataset's first variable at daily resolution."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2015-01-01"),
        end_date=pd.Timestamp("2015-01-01"),
        resolution="D",
        dates=pd.date_range("2015-01-01", "2015-01-01", freq="D"),
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


#: Curated rows whose extras were derived from live constraints rows[0].
_CURATED = (
    "projections-cmip6",
    "reanalysis-uerra-europe-single-levels",
    "sis-agroclimatic-indicators",
    "sis-european-wind-storm-indicators",
    "insitu-observations-surface-land",
    "sis-energy-global-reanalysis",
    "reanalysis-oras5-timeseries",
)

#: In-scope rows whose constraints.json is empty; left as seeds (no extras).
_SEEDS = (
    "derived-reanalysis-energy-moisture-budget",
    "insitu-gridded-observations-alpine-precipitation",
    "reanalysis-uerra-europe-complete",
    "sis-health-vector",
    "sis-temperature-statistics",
)


class TestCuratedOtherRows:
    """Each curated row forwards its extras into the built request."""

    @pytest.mark.parametrize("dataset", _CURATED)
    def test_extras_reach_the_request(self, dataset):
        """Every non-null extras selector lands; a null opt-out drops its key."""
        extras = Catalog().datasets[dataset].extras
        assert extras, f"{dataset} has no curated extras"
        request = _request(dataset)
        for key, value in extras.items():
            if value is None:
                assert key not in request, f"{dataset}: {key} should be dropped"
            else:
                assert key in request, f"{dataset}: {key} missing from request"

    @pytest.mark.parametrize("dataset", _SEEDS)
    def test_empty_constraints_left_as_seed(self, dataset):
        """A dataset with empty constraints keeps no curated extras."""
        assert not Catalog().datasets[dataset].extras


class TestCuratedOtherShape:
    """The curated rows build the date / time / format shape from constraints."""

    def test_uerra_pins_time_and_keeps_day(self):
        """UERRA enumerates time, so the request pins it and keeps year/month/day."""
        request = _request("reanalysis-uerra-europe-single-levels")
        assert request["time"] == ["00:00"]
        assert {"year", "month", "day"} <= set(request)
        assert request["origin"] == ["uerra_harmonie"]

    def test_period_dataset_strips_every_date(self):
        """A period-only indicator carries no year/month/day and no time."""
        request = _request("sis-agroclimatic-indicators")
        assert not {"year", "month", "day", "time"} & set(request)
        assert request["temporal_aggregation"] == ["10_day"]

    def test_cmip6_strips_day_keeps_year_month(self):
        """CMIP6 keys on year/month with no day or time-of-day."""
        request = _request("projections-cmip6")
        assert {"year", "month"} <= set(request)
        assert not {"day", "time"} & set(request)
        assert request["model"] == ["access_cm2"]

    def test_oras5_timeseries_uses_date_range(self):
        """The oras5 timeseries row keys on a date range, not year/month/day."""
        request = _request("reanalysis-oras5-timeseries")
        assert request["date"] == "2015-01-01/2015-01-01"
        assert not {"year", "month", "day"} & set(request)
        assert request["product_type"] == ["operational"]

    @pytest.mark.parametrize("dataset", _CURATED)
    def test_no_data_format_no_area(self, dataset):
        """None of the curated rows send the ERA5 data_format or area bbox."""
        request = _request(dataset)
        assert "data_format" not in request
        assert "area" not in request
