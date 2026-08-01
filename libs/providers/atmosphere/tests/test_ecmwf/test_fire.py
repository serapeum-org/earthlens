"""Unit tests for the CEMS fire-danger rows (C6).

`cems-fire-historical-v1` and `cems-fire-seasonal` load on EWDS, resolve the
CEMS provider, and build the valid-combo request their `constraints.json`
declares (historical needs `dataset_type: intermediate_dataset` + a `grid`).
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf.backend import ECMWF

pytestmark = [pytest.mark.unit]

_FIRE = ("cems-fire-historical-v1", "cems-fire-seasonal")


def _request(dataset):
    """Build the request for a fire dataset's first variable."""
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=pd.Timestamp("2024-01-01"),
        end_date=pd.Timestamp("2024-01-01"),
        resolution="D",
        dates=pd.date_range("2024-01-01", "2024-01-01", freq="D"),
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


class TestFireRows:
    """Both fire rows load on EWDS with the CEMS provider."""

    @pytest.mark.parametrize("dataset", _FIRE)
    def test_row_loads_on_ewds_with_cems_provider(self, dataset):
        """Each fire row is on the ewds endpoint, CEMS provider, no time-of-day."""
        record = Catalog().datasets[dataset]
        assert record.endpoint == "ewds"
        assert record.provider == "copernicus-cems"
        assert record.request_kind == "fire"


class TestFireRequestShapes:
    """Each fire family builds its valid-combo request."""

    def test_historical_carries_dataset_type_and_grid_no_time(self):
        """fire-historical sends dataset_type + grid + system_version, no time."""
        request = _request("cems-fire-historical-v1")
        assert request["dataset_type"] == "intermediate_dataset"
        assert request["grid"] == "0.25/0.25"
        assert request["system_version"] == ["4_1"]
        assert "time" not in request

    def test_seasonal_carries_release_version_and_leadtime_no_product_type(self):
        """fire-seasonal sends release_version + leadtime + day=01, no product_type."""
        request = _request("cems-fire-seasonal")
        assert request["release_version"] == ["5"]
        assert request["leadtime_hour"] == ["1020"]
        assert request["day"] == ["01"]
        assert "product_type" not in request
        assert "time" not in request
