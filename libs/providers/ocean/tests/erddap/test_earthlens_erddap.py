"""Integration tests for the ERDDAP backend through `EarthLens`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens
from earthlens.erddap import ERDDAP

pytestmark = [pytest.mark.erddap, pytest.mark.integration]

GRIDDAP_ID = "NOAA_DHW"
TABLEDAP_ID = "cwwcNDBCMet"


class TestRouting:
    """The facade resolves the erddap keys to the backend."""

    @pytest.mark.parametrize("key", ["erddap", "ioos"])
    def test_keys_present(self, key):
        """erddap and its aliases are registered keys."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", ["erddap", "ioos"])
    def test_keys_resolve_to_class(self, key):
        """Each key resolves to the ERDDAP backend class."""
        assert EarthLens.DataSources[key] is ERDDAP

    def test_facade_constructs_tabledap(self, tmp_path):
        """A tabledap dataset routes to a tabular ERDDAP instance."""
        el = EarthLens(
            data_source="erddap",
            dataset=TABLEDAP_ID,
            variables=["station", "time", "WTMP"],
            start="2023-01-01",
            end="2023-01-02",
            lat_lim=[36.0, 37.0],
            lon_lim=[-123.0, -122.0],
            path=str(tmp_path),
        )
        assert isinstance(el.datasource, ERDDAP)
        assert el.datasource.OUTPUT_KIND == "tabular"

    def test_facade_constructs_griddap(self, tmp_path):
        """A griddap dataset routes to a raster ERDDAP instance."""
        el = EarthLens(
            data_source="erddap",
            dataset=GRIDDAP_ID,
            start="2023-06-01",
            end="2023-06-01",
            lat_lim=[0.0, 1.0],
            lon_lim=[150.0, 151.0],
            path=str(tmp_path),
        )
        assert el.datasource.OUTPUT_KIND == "raster"


class TestFacadeAggregateGating:
    """The facade gates aggregate= purely on the per-instance OUTPUT_KIND."""

    def test_facade_rejects_aggregate_for_tabledap(self, tmp_path, fake_erddapy):
        """A tabledap dataset rejects aggregate= at the facade."""
        fake_erddapy.frame = pd.DataFrame({"station": ["46042"]})
        el = EarthLens(
            data_source="erddap",
            dataset=TABLEDAP_ID,
            variables=["station"],
            start="2023-01-01",
            end="2023-01-02",
            lat_lim=[36.0, 37.0],
            lon_lim=[-123.0, -122.0],
            path=str(tmp_path),
        )
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            el.download(progress_bar=False, aggregate=AggregationConfig(freq="1D"))

    def test_facade_forwards_aggregate_for_griddap(
        self, tmp_path, fake_nc_get, monkeypatch
    ):
        """A griddap dataset forwards aggregate= into the pyramids reduce."""
        seen = {}

        def _fake_aggregate(nc_path, var_info, config):
            seen["nc_path"] = nc_path
            return [("2023-06-01", None, tmp_path / "agg.tif")]

        monkeypatch.setattr("earthlens.aggregate.aggregate_netcdf", _fake_aggregate)
        el = EarthLens(
            data_source="erddap",
            dataset=GRIDDAP_ID,
            variables=["CRW_SSTANOMALY"],
            start="2023-06-01",
            end="2023-06-01",
            lat_lim=[0.0, 1.0],
            lon_lim=[150.0, 151.0],
            path=str(tmp_path),
        )
        result = el.download(progress_bar=False, aggregate=AggregationConfig(freq="1D"))
        assert result == [tmp_path / "agg.tif"]
        assert seen["nc_path"] == Path(tmp_path) / f"{GRIDDAP_ID}.nc"
