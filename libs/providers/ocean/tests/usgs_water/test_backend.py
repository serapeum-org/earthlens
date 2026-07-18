"""Tests for the USGSWater backend dispatch, fallbacks, and output."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.usgs_water import USGSWater
from .conftest import modern_long_frame

pytestmark = pytest.mark.usgs_water


def test_construct_validates_service(usgs_kwargs):
    """An unknown service= raises a clear ValueError."""
    with pytest.raises(ValueError, match="service must be one of"):
        USGSWater(**usgs_kwargs(service="nope"))


def test_construct_validates_api(usgs_kwargs):
    """An unknown api= raises a clear ValueError."""
    with pytest.raises(ValueError, match="api must be one of"):
        USGSWater(**usgs_kwargs(api="bogus"))


def test_construct_validates_output_format(usgs_kwargs):
    """An unknown output_format= raises a clear ValueError."""
    with pytest.raises(ValueError, match="output_format must be one of"):
        USGSWater(**usgs_kwargs(output_format="xml"))


def test_variables_mapping_rejected(usgs_kwargs):
    """A mapping for variables is rejected with a TypeError."""
    kwargs = usgs_kwargs()
    kwargs["variables"] = {"a": ["00060"]}
    with pytest.raises(TypeError, match="parameter codes"):
        USGSWater(**kwargs)


def test_output_kind_is_tabular(usgs_kwargs):
    """OUTPUT_KIND is tabular."""
    assert USGSWater(**usgs_kwargs()).OUTPUT_KIND == "tabular"


def test_resolved_codes_dedupes(usgs_kwargs):
    """Friendly names and codes resolve and de-duplicate, order-stable."""
    backend = USGSWater(**usgs_kwargs(variables=["discharge", "00060", "gage_height"]))
    assert backend._resolved_codes() == ["00060", "00065"]


def test_temporal_resolution_alias_selects_instantaneous(usgs_kwargs):
    """A sub-daily temporal_resolution maps the default service to instantaneous."""
    backend = USGSWater(**usgs_kwargs(temporal_resolution="hourly"))
    assert backend._service == "instantaneous"


def test_explicit_service_wins_over_temporal_resolution(usgs_kwargs):
    """An explicit service= is not overridden by temporal_resolution."""
    backend = USGSWater(**usgs_kwargs(service="samples", temporal_resolution="hourly"))
    assert backend._service == "samples"


@pytest.mark.parametrize(
    "resolution, expected",
    [
        ("hourly", "instantaneous"),
        ("instantaneous", "instantaneous"),
        ("raw", "instantaneous"),
        ("daily", "daily"),
        ("monthly", "daily"),
        ("yearly", "daily"),
        ("weekly", "daily"),
    ],
)
def test_temporal_resolution_alias_only_maps_subdaily(
    usgs_kwargs, resolution, expected
):
    """Only explicit sub-daily tokens alias to instantaneous; others stay daily."""
    backend = USGSWater(**usgs_kwargs(temporal_resolution=resolution))
    assert backend._service == expected


def test_daily_modern_calls_get_daily(fake_usgs, usgs_kwargs):
    """A daily download on api=auto calls waterdata.get_daily."""
    df = USGSWater(**usgs_kwargs(service="daily", sites="01646500")).download(
        progress_bar=False
    )
    assert fake_usgs.called() == ["get_daily"]
    assert df["site_no"].iloc[0] == "01646500"


def test_daily_legacy_calls_get_dv(fake_usgs, usgs_kwargs):
    """api=legacy routes daily to nwis.get_dv."""
    USGSWater(**usgs_kwargs(service="daily", sites="01646500", api="legacy")).download(
        progress_bar=False
    )
    assert fake_usgs.called() == ["get_dv"]


def test_instantaneous_modern_calls_get_continuous(fake_usgs, usgs_kwargs):
    """Instantaneous with explicit sites uses modern get_continuous."""
    USGSWater(**usgs_kwargs(service="instantaneous", sites="01646500")).download(
        progress_bar=False
    )
    assert fake_usgs.called() == ["get_continuous"]


def test_instantaneous_bbox_only_falls_back_to_legacy(fake_usgs, usgs_kwargs):
    """Instantaneous by bbox (no sites) on auto routes to legacy get_iv."""
    USGSWater(**usgs_kwargs(service="instantaneous")).download(progress_bar=False)
    assert fake_usgs.called() == ["get_iv"]


def test_instantaneous_bbox_forced_modern_errors(fake_usgs, usgs_kwargs):
    """api=waterdata + instantaneous + bbox-only raises a clear error."""
    backend = USGSWater(**usgs_kwargs(service="instantaneous", api="waterdata"))
    with pytest.raises(ValueError, match="cannot query service"):
        backend.download(progress_bar=False)


def test_429_falls_back_to_legacy(fake_usgs, usgs_kwargs, monkeypatch):
    """A modern 429 under anonymous auto retries on legacy."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    fake_usgs.rate_limit("get_daily")
    USGSWater(**usgs_kwargs(service="daily", sites="01646500")).download(
        progress_bar=False
    )
    assert fake_usgs.called() == ["get_daily", "get_dv"]


def test_warn_legacy_fallback_only_once(usgs_kwargs):
    """The legacy-fallback warning is emitted once, then short-circuits."""
    backend = USGSWater(**usgs_kwargs())
    backend._warn_legacy_fallback()
    assert backend._used_legacy_fallback is True
    backend._warn_legacy_fallback()
    assert backend._used_legacy_fallback is True


def test_429_not_swallowed_when_token_present(fake_usgs, usgs_kwargs, monkeypatch):
    """With a token, a modern 429 is not silently downgraded to legacy."""
    monkeypatch.setenv("API_USGS_PAT", "tok")
    fake_usgs.rate_limit("get_daily")
    backend = USGSWater(**usgs_kwargs(service="daily", sites="01646500"))
    with pytest.raises(Exception, match="429"):
        backend.download(progress_bar=False)


def test_query_kwargs_flow_through(fake_usgs, usgs_kwargs):
    """The resolved codes + bbox reach the SDK call."""
    USGSWater(**usgs_kwargs(service="daily", variables=["discharge"])).download(
        progress_bar=False
    )
    kw = fake_usgs.kwargs_for("get_daily")
    assert kw["parameter_code"] == "00060"
    assert kw["bbox"] == [-77.2, 38.9, -77.0, 39.0]


def test_aggregate_rejected(usgs_kwargs):
    """download(aggregate=...) raises NotImplementedError for tabular."""
    backend = USGSWater(**usgs_kwargs())
    with pytest.raises(NotImplementedError, match="statistics"):
        backend.download(aggregate=object())


def test_writes_csv_by_default(fake_usgs, usgs_kwargs, tmp_path):
    """A download writes a CSV named for the service + codes."""
    USGSWater(**usgs_kwargs(service="daily", sites="01646500")).download(
        progress_bar=False
    )
    out = tmp_path / "usgs_daily_00060.csv"
    assert out.exists()
    assert len(pd.read_csv(out)) == 3


def test_writes_parquet_when_requested(fake_usgs, usgs_kwargs, tmp_path):
    """output_format=parquet writes a round-trippable parquet table."""
    USGSWater(
        **usgs_kwargs(service="daily", sites="01646500", output_format="parquet")
    ).download(progress_bar=False)
    out = tmp_path / "usgs_daily_00060.parquet"
    assert out.exists()
    assert len(pd.read_parquet(out)) == 3


def test_empty_result_writes_schema_only(fake_usgs, usgs_kwargs, tmp_path):
    """An empty service frame writes a zero-row schema-only table."""
    fake_usgs.set_return("get_daily", modern_long_frame(n=0))
    df = USGSWater(**usgs_kwargs(service="daily", sites="01646500")).download(
        progress_bar=False
    )
    assert df.empty
    assert (tmp_path / "usgs_daily_00060.csv").exists()


def test_samples_calls_get_samples(fake_usgs, usgs_kwargs):
    """The samples service calls modern waterdata.get_samples."""
    fake_usgs.set_return("get_samples", _samples_frame())
    df = USGSWater(
        **usgs_kwargs(service="samples", variables=["dissolved_oxygen"])
    ).download(progress_bar=False)
    assert fake_usgs.called() == ["get_samples"]
    assert "detection_limit" in df.columns
    assert df["value"].iloc[0] == 8.5


def test_samples_has_no_legacy_fallback(fake_usgs, usgs_kwargs):
    """api=legacy on the modern-only samples service errors clearly."""
    backend = USGSWater(**usgs_kwargs(service="samples", api="legacy"))
    with pytest.raises(ValueError, match="no legacy endpoint"):
        backend.download(progress_bar=False)


def test_samples_429_no_fallback_errors(fake_usgs, usgs_kwargs, monkeypatch):
    """A 429 on modern-only samples (anonymous) raises with token advice."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    fake_usgs.rate_limit("get_samples")
    backend = USGSWater(**usgs_kwargs(service="samples"))
    with pytest.raises(RuntimeError, match="API_USGS_PAT"):
        backend.download(progress_bar=False)


def test_statistics_modern_calls_get_stats_date_range(fake_usgs, usgs_kwargs):
    """The statistics service calls modern get_stats_date_range, honouring the window."""
    fake_usgs.set_return("get_stats_date_range", _stats_modern_frame())
    df = USGSWater(**usgs_kwargs(service="statistics", sites="01646500")).download(
        progress_bar=False
    )
    assert fake_usgs.called() == ["get_stats_date_range"]
    assert "percentile" in df.columns
    # the caller's window is forwarded as start_date / end_date
    kw = fake_usgs.kwargs_for("get_stats_date_range")
    assert kw["start_date"] == "2023-01-01" and kw["end_date"] == "2023-01-05"


def test_statistics_legacy_forwards_stat_type(fake_usgs, usgs_kwargs):
    """Legacy statistics forwards stat_type as statReportType."""
    fake_usgs.set_return("get_stats", _stats_legacy_frame())
    USGSWater(
        **usgs_kwargs(
            service="statistics", sites="01646500", api="legacy", stat_type="monthly"
        )
    ).download(progress_bar=False)
    assert fake_usgs.kwargs_for("get_stats")["statReportType"] == "monthly"


def test_gwlevels_routes_through_daily(fake_usgs, usgs_kwargs):
    """gwlevels is a parameter family served by get_daily."""
    USGSWater(
        **usgs_kwargs(
            service="gwlevels", variables=["groundwater_level"], sites="375907091432201"
        )
    ).download(progress_bar=False)
    assert fake_usgs.called() == ["get_daily"]


def test_peaks_requires_sites(fake_usgs, usgs_kwargs):
    """The site-keyed peaks service errors without sites=."""
    backend = USGSWater(**usgs_kwargs(service="peaks"))
    with pytest.raises(ValueError, match="requires an explicit sites"):
        backend.download(progress_bar=False)


def test_ratings_requires_sites(fake_usgs, usgs_kwargs):
    """The site-keyed ratings service errors without sites=."""
    backend = USGSWater(**usgs_kwargs(service="ratings"))
    with pytest.raises(ValueError, match="requires an explicit sites"):
        backend.download(progress_bar=False)


def test_statistics_requires_sites(fake_usgs, usgs_kwargs):
    """Statistics errors without sites= (no bbox filter on either endpoint)."""
    backend = USGSWater(**usgs_kwargs(service="statistics"))
    with pytest.raises(ValueError, match="requires an explicit sites"):
        backend.download(progress_bar=False)


def test_statistics_still_resolves_codes(usgs_kwargs):
    """Statistics keeps parameter codes (not site-keyed), unlike peaks/ratings."""
    backend = USGSWater(**usgs_kwargs(service="statistics", sites="01646500"))
    products = backend._search()
    assert products[0].metadata["codes"] == ["00060"]


def test_peaks_legacy_normalizes(fake_usgs, usgs_kwargs):
    """A legacy peaks pull normalizes peak_va to peak_value."""
    fake_usgs.set_return("get_discharge_peaks", _peaks_legacy_frame())
    df = USGSWater(
        **usgs_kwargs(service="peaks", sites="01646500", api="legacy")
    ).download(progress_bar=False)
    assert fake_usgs.called() == ["get_discharge_peaks"]
    assert df["peak_value"].iloc[0] == 350000.0


def test_sites_discovery_calls_monitoring_locations(fake_usgs, usgs_kwargs):
    """service=sites calls modern get_monitoring_locations and writes sites."""
    fake_usgs.set_return("get_monitoring_locations", _sites_modern_frame())
    df = USGSWater(**usgs_kwargs(service="sites")).download(progress_bar=False)
    assert fake_usgs.called() == ["get_monitoring_locations"]
    assert "latitude" in df.columns


def _samples_frame():
    """A minimal modern WQP samples frame for one dissolved-oxygen result."""
    return pd.DataFrame(
        {
            "Location_Identifier": ["USGS-01646500"],
            "Activity_StartDateTime": ["2018-05-01T12:00:00Z"],
            "USGSpcode": ["00300"],
            "Result_Characteristic": ["Dissolved oxygen"],
            "Result_Measure": ["8.5"],
            "Result_MeasureUnit": ["mg/l"],
            "Result_MeasureQualifierCode": [None],
            "Result_ResultDetectionCondition": [None],
            "DetectionLimit_MeasureA": [None],
            "DetectionLimit_MeasureUnitA": [None],
            "ResultAnalyticalMethod_Name": ["EPA 360.1"],
            "Result_SampleFraction": ["Dissolved"],
            "Activity_Media": ["Water"],
        }
    )


def _stats_modern_frame():
    """A minimal modern get_stats_date_range frame (windowed interval stats)."""
    return pd.DataFrame(
        {
            "monitoring_location_id": ["USGS-01646500"],
            "parameter_code": ["00060"],
            "start_date": ["2023-01-01"],
            "end_date": ["2023-01-31"],
            "interval_type": ["month"],
            "value": [123.0],
            "percentile": [50],
            "computation": ["arithmetic_mean"],
            "unit_of_measure": ["ft^3/s"],
        }
    )


def _stats_legacy_frame():
    """A minimal legacy get_stats monthly frame."""
    return pd.DataFrame(
        {
            "site_no": ["01646500"],
            "parameter_cd": ["00060"],
            "year_nu": [2023],
            "month_nu": [3],
            "mean_va": [13090.0],
        }
    )


def _peaks_legacy_frame():
    """A minimal legacy discharge-peaks frame (datetime index)."""
    idx = pd.to_datetime(["1990-03-01"], utc=True)
    idx.name = "datetime"
    return pd.DataFrame(
        {
            "site_no": ["01646500"],
            "peak_va": [350000.0],
            "gage_ht": [20.1],
            "peak_cd": ["5"],
        },
        index=idx,
    )


def _sites_modern_frame():
    """A minimal modern monitoring-locations frame."""
    return pd.DataFrame(
        {
            "monitoring_location_id": ["USGS-01646500"],
            "monitoring_location_name": ["POTOMAC RIVER"],
            "dec_lat_va": [38.94],
            "dec_long_va": [-77.12],
            "hydrologic_unit_code": ["02070008"],
            "site_type": ["Stream"],
        }
    )
