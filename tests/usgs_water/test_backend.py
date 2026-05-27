"""Tests for the USGSWater backend dispatch, fallbacks, and output."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.usgs_water import USGSWater
from tests.usgs_water.conftest import modern_long_frame

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
