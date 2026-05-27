"""Tests for the dispatch / query-kwarg / normalize helpers."""

from __future__ import annotations

import pytest

from earthlens.usgs_water import _helpers
from tests.usgs_water.conftest import legacy_wide_frame, modern_long_frame

pytestmark = pytest.mark.usgs_water

CODE_META = {"00060": ("Discharge", "ft3/s"), "00065": ("Gage height", "ft")}


def test_service_function_table():
    """The dispatch table maps services to the verified function names."""
    assert _helpers.service_function("daily", "waterdata") == "get_daily"
    assert _helpers.service_function("daily", "nwis") == "get_dv"
    assert _helpers.service_function("instantaneous", "waterdata") == "get_continuous"
    assert _helpers.service_function("peaks", "waterdata") == "get_peaks"


def test_modern_supports_bbox():
    """daily supports bbox; instantaneous does not."""
    assert _helpers.modern_supports_bbox("daily") is True
    assert _helpers.modern_supports_bbox("instantaneous") is False


@pytest.mark.parametrize(
    "exc, expected",
    [
        (type("QuotaExhausted", (Exception,), {})("x"), True),
        (type("RateLimited", (Exception,), {})("x"), True),
        (RuntimeError("HTTP 429 too many"), True),
        (RuntimeError("boom"), False),
    ],
)
def test_is_rate_limit_error(exc, expected):
    """429 / quota errors are detected; unrelated errors are not."""
    assert _helpers.is_rate_limit_error(exc) is expected


def test_query_kwargs_modern_bbox_daily():
    """Modern daily builds parameter_code + time + bbox kwargs."""
    kw = _helpers.query_kwargs(
        service="daily",
        flavour="waterdata",
        codes=["00060"],
        sites=None,
        bbox=[-77.2, 38.9, -77.0, 39.0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    assert kw["parameter_code"] == "00060"
    assert kw["bbox"] == [-77.2, 38.9, -77.0, 39.0]
    assert kw["time"] == "2023-01-01/2023-01-05"


def test_query_kwargs_modern_sites_prefixed():
    """Modern site queries prefix site numbers with USGS-."""
    kw = _helpers.query_kwargs(
        service="daily",
        flavour="waterdata",
        codes=["00060", "00065"],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2023-01-01",
        end="2023-01-05",
        limit=10,
    )
    assert kw["monitoring_location_id"] == ["USGS-01646500"]
    assert kw["parameter_code"] == ["00060", "00065"]
    assert kw["limit"] == 10
    assert "bbox" not in kw


def test_query_kwargs_legacy_bbox_string():
    """Legacy builds parameterCd + start/end + a bBox comma string."""
    kw = _helpers.query_kwargs(
        service="daily",
        flavour="nwis",
        codes=["00060"],
        sites=None,
        bbox=[-77.2, 38.9, -77.0, 39.0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    assert kw["parameterCd"] == "00060"
    assert kw["bBox"] == "-77.2,38.9,-77.0,39.0"
    assert kw["start"] == "2023-01-01"
    assert kw["end"] == "2023-01-05"


def test_query_kwargs_statistics_passes_stat_type():
    """Legacy statistics forwards stat_type as statReportType."""
    kw = _helpers.query_kwargs(
        service="statistics",
        flavour="nwis",
        codes=["00060"],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2023-01-01",
        end="2023-12-31",
        limit=None,
        stat_type="monthly",
    )
    assert kw["statReportType"] == "monthly"


def test_normalize_modern_long_strips_prefix():
    """Modern normalize strips the USGS- prefix and fills the name."""
    out = _helpers.normalize(modern_long_frame(n=3), "waterdata", CODE_META)
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS
    assert out["site_no"].iloc[0] == "01646500"
    assert out["parameter_name"].iloc[0] == "Discharge"
    assert len(out) == 3


def test_normalize_legacy_wide_melts():
    """Legacy normalize melts the wide value/qualifier pair to long rows."""
    out = _helpers.normalize(legacy_wide_frame(n=4, stat="Mean"), "nwis", CODE_META)
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS
    assert len(out) == 4
    assert out["statistic_id"].iloc[0] == "Mean"
    assert out["qualifier"].iloc[0] == "A"
    assert out["unit"].iloc[0] == "ft3/s"


def test_normalize_legacy_multi_code_melts_each():
    """A legacy frame with two parameter columns melts both into rows."""
    import pandas as pd

    idx = pd.to_datetime(pd.date_range("2023-01-01", periods=2), utc=True)
    idx.name = "datetime"
    frame = pd.DataFrame(
        {
            "00060_Mean": [1, 2],
            "00060_Mean_cd": ["A", "A"],
            "00065_Mean": [3, 4],
            "00065_Mean_cd": ["A", "P"],
            "site_no": ["01646500", "01646500"],
        },
        index=idx,
    )
    out = _helpers.normalize(frame, "nwis", CODE_META)
    assert set(out["parameter_code"]) == {"00060", "00065"}
    assert len(out) == 4


def test_normalize_empty_returns_schema():
    """An empty frame normalizes to a zero-row canonical frame."""
    import pandas as pd

    out = _helpers.normalize(pd.DataFrame(), "waterdata", CODE_META)
    assert out.empty
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS
