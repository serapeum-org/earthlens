"""Unit tests for the ECDS and XDS catalog rows.

Covers the two ECMWF-hosted stores added alongside CDS / ADS / EWDS: the XDS
fire-fuel rows, whose `day` / `time` (and, on the annual burned-area row,
`month`) template keys are dropped by explicit `null` opt-outs rather than a
bespoke `request_kind`, and the ECDS TIGGE row, whose request vocabulary is
taken from the live constraints rather than the MARS idiom.

Every curated variable's `nc_variable` and `units` were read out of a real
download rather than taken from the constraints or the documentation; each
shard's header records what was observed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.ecmwf import Catalog
from earthlens.ecmwf import constraints as constraints_module
from earthlens.ecmwf.backend import ECMWF
from earthlens.ecmwf.endpoints import constraints_base_url

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


def _daily_backend(stamp):
    """A stub ECMWF on a single day, for the date-parameterised rows."""
    when = pd.Timestamp(stamp)
    backend = ECMWF.__new__(ECMWF)
    backend.time = TemporalExtent(
        start_date=when,
        end_date=when,
        resolution="D",
        dates=pd.date_range(when, when, freq="D"),
    )
    backend.space = SpatialExtent(
        latitude_min=50.0,
        latitude_max=51.0,
        longitude_min=9.0,
        longitude_max=10.0,
        resolution=1.5,
    )
    backend.temporal_resolution = "daily"
    return backend


def _reforecast_variable():
    """The curated `s2s-reforecasts` row the guard tests build a request from."""
    return Catalog().get_variable(
        "s2s-reforecasts", "maximum-2m-temperature-in-the-last-6-hours"
    )


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
    def test_xds_rows_opt_out_of_data_format(self, dataset):
        """Neither XDS form has a `data_format` widget, so the key is dropped."""
        record = Catalog().datasets[dataset]
        variable = next(iter(record.variables.values()))
        assert "data_format" not in _backend()._build_request(variable)

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


class TestEcdsCatalogRows:
    """Catalog shape for the curated ECDS dataset."""

    def test_tigge_is_curated_on_the_ecds_endpoint(self):
        """`tigge-forecasts` loads and routes to ECDS."""
        record = Catalog().datasets["tigge-forecasts"]
        assert record.endpoint == "ecds"
        assert all(v.endpoint == "ecds" for v in record.variables.values())

    def test_tigge_variable_metadata_is_live_verified(self):
        """The 2 m temperature row carries its real NetCDF name and unit."""
        variable = Catalog().get_variable("tigge-forecasts", "2m-temperature")
        assert variable.cds_variable == "2_m_temperature"
        assert variable.nc_variable == "t2m"
        assert variable.units == "K"

    def test_tigge_uses_the_live_request_vocabulary(self):
        """The row pins the values the live constraints accept, not the MARS idiom."""
        request = _daily_backend("2024-01-01")._build_request(
            Catalog().get_variable("tigge-forecasts", "2m-temperature")
        )
        assert request["origin"] == ["ecmwf"]
        assert request["level_type"] == ["single_level"]
        assert request["variable"] == ["2_m_temperature"]

    @pytest.mark.parametrize(
        "dataset, variable_name, nc_variable",
        [
            ("s2s-forecasts", "2m-temperature", "t2m"),
            (
                "s2s-reforecasts",
                "maximum-2m-temperature-in-the-last-6-hours",
                "mx2t6",
            ),
        ],
    )
    def test_s2s_metadata_is_live_verified(self, dataset, variable_name, nc_variable):
        """Both S2S rows carry their real NetCDF names and units."""
        variable = Catalog().get_variable(dataset, variable_name)
        assert variable.nc_variable == nc_variable
        assert variable.units == "K"
        assert variable.endpoint == "ecds"

    def test_s2s_reforecasts_keeps_both_date_axes(self):
        """The reforecast request carries the model-cycle *and* reforecast dates.

        `glofas_hindcast` renames year->hyear, which would drop the model-cycle
        date this dataset also requires, so the row uses `s2s_reforecast`.
        """
        request = _daily_backend("2015-01-01")._build_request(
            Catalog().get_variable(
                "s2s-reforecasts", "maximum-2m-temperature-in-the-last-6-hours"
            )
        )
        for key in ("year", "month", "day", "hyear", "hmonth", "hday"):
            assert key in request, key

    @pytest.mark.parametrize("month, day", [("01", "01"), ("06", "15"), ("11", "30")])
    def test_reforecast_date_tracks_the_requested_month_and_day(self, month, day):
        """`hmonth`/`hday` follow the requested model cycle, never a fixed literal.

        A pinned reforecast date only matches when the request happens to fall
        on it; every other window is rejected by the live constraints.
        """
        request = _daily_backend(f"2015-{month}-{day}")._build_request(
            Catalog().get_variable(
                "s2s-reforecasts", "maximum-2m-temperature-in-the-last-6-hours"
            )
        )
        assert request["hmonth"] == request["month"] == [month]
        assert request["hday"] == request["day"] == [day]

    @pytest.mark.parametrize("end", ["2015-01-05", "2015-03-01"])
    def test_multi_day_window_is_rejected(self, end):
        """A window spanning >1 model-cycle day cannot express the date pairing.

        A CDS form treats every list as a cross-product axis, so an n-day
        window would submit n x n day/hday combinations of which only the n
        diagonal pairs exist.
        """
        backend = _daily_backend("2015-01-01")
        stamps = pd.date_range("2015-01-01", end, freq="D")
        backend.time = TemporalExtent(
            start_date=stamps[0],
            end_date=stamps[-1],
            resolution="D",
            dates=stamps,
        )
        variable = _reforecast_variable()
        with pytest.raises(ValueError, match="one model-cycle date at a time"):
            backend._build_request(variable)

    def test_reforecast_keys_are_copies_not_aliases(self):
        """`hmonth`/`hday` are distinct lists, so editing one cannot move the other."""
        request = _daily_backend("2015-06-01")._build_request(_reforecast_variable())
        assert request["day"] == request["hday"]
        assert request["day"] is not request["hday"]
        assert request["month"] is not request["hmonth"]

    def test_monthly_resolution_is_rejected(self):
        """The row selects by the model run's calendar day, so it needs a `day`."""
        backend, variable = _backend(), _reforecast_variable()
        with pytest.raises(ValueError, match="temporal_resolution='daily'"):
            backend._build_request(variable)

    def test_leap_day_against_a_non_leap_reforecast_year_is_rejected(self):
        """A 29 February cycle has no reforecast in the pinned non-leap 1995."""
        backend, variable = _daily_backend("2016-02-29"), _reforecast_variable()
        with pytest.raises(ValueError, match="non-leap"):
            backend._build_request(variable)

    def test_only_the_reforecast_year_is_pinned(self):
        """`hyear` stays a per-row choice; `hmonth`/`hday` are not pinned."""
        extras = Catalog().datasets["s2s-reforecasts"].extras
        assert extras["hyear"] == ["1995"]
        assert "hmonth" not in extras
        assert "hday" not in extras

    @pytest.mark.parametrize("dataset", ["s2s-forecasts", "s2s-reforecasts"])
    def test_s2s_is_curated(self, dataset):
        """Both S2S datasets are curated now that the licence is accepted."""
        catalog = Catalog()
        assert dataset in catalog.available_datasets
        assert dataset in catalog.datasets

    @pytest.mark.parametrize(
        "dataset, store",
        [
            ("tigge-forecasts", "ecds"),
            ("s2s-forecasts", "ecds"),
            ("s2s-reforecasts", "ecds"),
            ("derived-fire-fuel-biomass", "xds"),
            ("projections-fire-fuel-burned-area", "xds"),
        ],
    )
    def test_store_for_resolves_every_new_id(self, dataset, store):
        """Every ECDS/XDS id resolves to its store via the per-store index."""
        assert Catalog().store_for(dataset) == store


class TestNewStoreConstraints:
    """Constraint fetching for the two ECMWF-hosted stores."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Reset the module-level constraints cache between tests."""
        constraints_module._CACHE.clear()
        yield
        constraints_module._CACHE.clear()

    @pytest.mark.parametrize(
        "dataset, endpoint, host",
        [
            ("tigge-forecasts", "ecds", "ecds.ecmwf.int"),
            ("derived-fire-fuel-biomass", "xds", "xds.ecmwf.int"),
        ],
    )
    def test_constraints_are_fetched_from_the_store_host(
        self, monkeypatch, dataset, endpoint, host
    ):
        """Constraints come from the dataset's own store, not the CDS default."""
        seen: list[str] = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b"[]"

        def _capture(url, *_a, **_kw):
            seen.append(getattr(url, "full_url", url))
            return _Resp()

        monkeypatch.setattr(constraints_module.urllib.request, "urlopen", _capture)
        constraints_module.fetch_constraints(
            dataset, base_url=constraints_base_url(endpoint)
        )
        assert seen, "no constraints request was issued"
        assert host in seen[0]
        assert dataset in seen[0]


class TestNewStoreLicenceRefusal:
    """A licence refusal names the store the dataset actually lives on."""

    @pytest.mark.parametrize(
        "endpoint, host",
        [("ecds", "ecds.ecmwf.int"), ("xds", "xds.ecmwf.int")],
    )
    def test_permission_error_points_at_the_right_store(
        self, ecmwf_stub, endpoint, host
    ):
        """The rewritten 403 links the dataset page on its own store."""
        from earthlens.ecmwf import Variable

        variable = Variable(
            cds_dataset="tigge-forecasts",
            cds_variable="2_m_temperature",
            nc_variable="t2m",
            units="K",
            endpoint=endpoint,
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError(
                "the request you have submitted is not valid. "
                "Required licences not accepted; please accept the terms of use."
            )

        ecmwf_stub.client.retrieve.side_effect = boom
        with pytest.raises(PermissionError) as excinfo:
            ecmwf_stub._api(variable)
        message = str(excinfo.value)
        assert host in message
        assert "tigge-forecasts" in message


class TestCuratedRowsAreComplete:
    """Offline guards on the five rows the live suite covers (review M6)."""

    #: The rows the ECDS/XDS live suite retrieves, and the NetCDF name each
    #: promises. The live tests assert the file contains these; this asserts
    #: the catalog still declares them, in the lane every PR runs.
    _EXPECTED = {
        ("tigge-forecasts", "2m-temperature"): "t2m",
        ("s2s-forecasts", "2m-temperature"): "t2m",
        ("s2s-reforecasts", "maximum-2m-temperature-in-the-last-6-hours"): "mx2t6",
        ("derived-fire-fuel-biomass", "live-fuel-moisture-content-group"): "LFMC",
        ("projections-fire-fuel-burned-area", "burned-area"): "BAF_pred",
    }

    @pytest.mark.parametrize("key, nc_variable", sorted(_EXPECTED.items()))
    def test_each_live_row_still_declares_its_nc_variable(self, key, nc_variable):
        """A rename would break the live suite; catch it in the offline lane."""
        dataset, variable = key
        assert Catalog().get_variable(dataset, variable).nc_variable == nc_variable

    @pytest.mark.parametrize("key", sorted(_EXPECTED))
    def test_no_live_row_ships_a_placeholder_unit(self, key):
        """A `units: unknown` row means the metadata was never verified."""
        dataset, variable = key
        assert Catalog().get_variable(dataset, variable).units != "unknown"
