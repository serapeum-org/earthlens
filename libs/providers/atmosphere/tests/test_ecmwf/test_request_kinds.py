"""Unit tests for the new ECMWF request-kind builders (C4).

`glofas_hindcast` (year/month/day → hyear/hmonth/hday), `seasonal` (year/month +
lead + centre, no day/time), and `cams_date` (a single `date` range string) —
all built offline via `_build_request`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog, Variable
from earthlens.ecmwf.backend import _REQUEST_KIND_STRIPS, ECMWF

pytestmark = [pytest.mark.unit]


class TestRequestKindIntegrity:
    """Every curated dataset uses a request kind the backend recognises."""

    def test_no_orphan_request_kinds(self):
        """No curated row names a request_kind with no handler / strip entry."""
        used = {record.request_kind for record in Catalog().datasets.values()}
        unknown = used - set(_REQUEST_KIND_STRIPS)
        assert not unknown, f"unrecognised request kinds in the catalog: {unknown}"


def _backend(resolution="daily"):
    """A stub ECMWF with a fixed daily/monthly window and bbox for _build_request."""
    backend = ECMWF.__new__(ECMWF)
    freq, dates = ("D", ("2022-01-01", "2022-01-03"))
    if resolution == "monthly":
        freq = "MS"
    backend.time = TemporalExtent(
        start_date=pd.Timestamp(dates[0]),
        end_date=pd.Timestamp(dates[1]),
        resolution=freq,
        dates=pd.date_range(dates[0], dates[1], freq=freq),
    )
    backend.space = SpatialExtent(
        latitude_min=0.0,
        latitude_max=1.0,
        longitude_min=0.0,
        longitude_max=1.0,
        resolution=0.1,
    )
    backend.temporal_resolution = resolution
    return backend


def _variable(request_kind, extras=None, **kw):
    """A Variable of the given request kind (defaults fill the required fields)."""
    return Variable(
        cds_dataset=kw.get("cds_dataset", "some-dataset"),
        cds_variable=kw.get("cds_variable", "river_discharge"),
        nc_variable=kw.get("nc_variable", "dis"),
        units=kw.get("units", "m3 s-1"),
        product_type=kw.get("product_type", ["control_reforecast"]),
        request_kind=request_kind,
        extras=extras or {},
    )


class TestGlofasHindcast:
    """`glofas_hindcast` remaps the date keys and drops the time-of-day."""

    def test_remaps_date_keys(self):
        """year/month/day become hyear/hmonth/hday; `time` is dropped."""
        request = _backend()._build_request(
            _variable("glofas_hindcast", extras={"leadtime_hour": ["24"]})
        )
        assert "hyear" in request and "year" not in request
        assert "hmonth" in request and "month" not in request
        assert "hday" in request and "day" not in request
        assert "time" not in request
        assert request["leadtime_hour"] == ["24"]

    def test_monthly_is_rejected(self):
        """A monthly hindcast request is rejected (needs the hday selector)."""
        with pytest.raises(ValueError, match="temporal_resolution='daily'"):
            _backend("monthly")._build_request(_variable("glofas_hindcast"))


class TestSeasonal:
    """`seasonal` keeps year/month but drops day + time-of-day."""

    def test_keeps_year_month_drops_day_time(self):
        """A seasonal request has year/month + the lead/centre extras, no day/time."""
        request = _backend()._build_request(
            _variable(
                "seasonal",
                extras={
                    "leadtime_month": ["1", "2", "3"],
                    "originating_centre": "ecmwf",
                    "system": ["51"],
                },
            )
        )
        assert "year" in request and "month" in request
        assert "day" not in request and "time" not in request
        assert request["originating_centre"] == "ecmwf"
        assert request["leadtime_month"] == ["1", "2", "3"]


class TestCamsDate:
    """`cams_date` replaces year/month/day with a single `date` range string."""

    def test_builds_date_range_and_drops_ymd(self):
        """A CAMS grid request keys on `date`='start/stop', not year/month/day."""
        request = _backend()._build_request(
            _variable(
                "cams_date",
                extras={"data_format": "netcdf_zip", "type": ["forecast"]},
                cds_dataset="cams-global-reanalysis-eac4",
                cds_variable="2m_temperature",
            )
        )
        assert request["date"] == "2022-01-01/2022-01-03"
        assert not {"year", "month", "day", "product_type"} & set(request)
        assert request["time"] == ["00:00"]
        assert request["type"] == ["forecast"]
        assert request["data_format"] == "netcdf_zip"
