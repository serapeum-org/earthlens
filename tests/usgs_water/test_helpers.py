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
    out = _helpers.normalize(modern_long_frame(n=3), "waterdata", "daily", CODE_META)
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS
    assert out["site_no"].iloc[0] == "01646500"
    assert out["parameter_name"].iloc[0] == "Discharge"
    assert len(out) == 3


def test_normalize_legacy_wide_melts():
    """Legacy normalize melts the wide value/qualifier pair to long rows."""
    frame = legacy_wide_frame(n=4, stat="Mean")
    out = _helpers.normalize(frame, "nwis", "daily", CODE_META)
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
    out = _helpers.normalize(frame, "nwis", "daily", CODE_META)
    assert set(out["parameter_code"]) == {"00060", "00065"}
    assert len(out) == 4


def test_normalize_empty_returns_schema():
    """An empty frame normalizes to a zero-row canonical frame."""
    import pandas as pd

    out = _helpers.normalize(pd.DataFrame(), "waterdata", "daily", CODE_META)
    assert out.empty
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS


def test_samples_kwargs_camelcase():
    """The samples service builds WQP camelCase kwargs."""
    kw = _helpers.query_kwargs(
        service="samples",
        flavour="waterdata",
        codes=["00300"],
        sites=None,
        bbox=[-77.2, 38.9, -77.0, 39.0],
        start="2018-01-01",
        end="2018-12-31",
        limit=None,
    )
    assert kw["usgsPCode"] == "00300"
    assert kw["boundingBox"] == [-77.2, 38.9, -77.0, 39.0]
    assert kw["activityStartDateLower"] == "2018-01-01"


def test_normalize_samples_maps_result_columns():
    """Samples normalize maps the WQP result columns to the QW schema."""
    import pandas as pd

    frame = pd.DataFrame(
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
    out = _helpers.normalize(frame, "waterdata", "samples", CODE_META)
    assert list(out.columns) == _helpers.SAMPLES_COLUMNS
    assert out["site_no"].iloc[0] == "01646500"
    assert out["value"].iloc[0] == 8.5
    assert out["method"].iloc[0] == "EPA 360.1"


def test_normalize_statistics_modern_long():
    """Modern statistics normalize keeps percentile + value columns."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "monitoring_location_id": ["USGS-01646500"],
            "parameter_code": ["00060"],
            "time_of_year": ["01-01"],
            "value": [123.0],
            "percentile": [50],
            "computation": ["daily-mean"],
            "unit_of_measure": ["ft^3/s"],
        }
    )
    out = _helpers.normalize(frame, "waterdata", "statistics", CODE_META)
    assert list(out.columns) == _helpers.STATS_COLUMNS
    assert out["percentile"].iloc[0] == 50
    assert out["site_no"].iloc[0] == "01646500"


def test_normalize_statistics_legacy_monthly():
    """Legacy statistics normalize folds year/month into time_of_year."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "site_no": ["01646500"],
            "parameter_cd": ["00060"],
            "year_nu": [2023],
            "month_nu": [3],
            "mean_va": [13090.0],
        }
    )
    out = _helpers.normalize(frame, "nwis", "statistics", CODE_META)
    assert out["value"].iloc[0] == 13090.0
    assert out["time_of_year"].iloc[0] == "2023-03"


def test_normalize_sites_legacy():
    """Legacy what_sites normalize maps to the site schema."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "site_no": ["01646500"],
            "station_nm": ["POTOMAC RIVER"],
            "dec_lat_va": [38.94],
            "dec_long_va": [-77.12],
            "huc_cd": ["02070008"],
            "site_tp_cd": ["ST"],
        }
    )
    out = _helpers.normalize(frame, "nwis", "sites", CODE_META)
    assert list(out.columns) == _helpers.SITE_COLUMNS
    assert out["station_name"].iloc[0] == "POTOMAC RIVER"
    assert out["latitude"].iloc[0] == 38.94


def test_normalize_peaks_legacy():
    """Legacy peaks normalize maps peak_va to peak_value."""
    import pandas as pd

    idx = pd.to_datetime(["1990-03-01"], utc=True)
    idx.name = "datetime"
    frame = pd.DataFrame(
        {
            "site_no": ["01646500"],
            "peak_va": [350000.0],
            "gage_ht": [20.1],
            "peak_cd": ["5"],
        },
        index=idx,
    )
    out = _helpers.normalize(frame, "nwis", "peaks", CODE_META)
    assert list(out.columns) == _helpers.PEAKS_COLUMNS
    assert out["peak_value"].iloc[0] == 350000.0
    assert out["gage_height"].iloc[0] == 20.1


def test_normalize_ratings_indep_dep_stor():
    """Ratings normalize maps INDEP/DEP/STOR to stage/discharge/storage."""
    import pandas as pd

    frame = pd.DataFrame({"INDEP": [1.0, 2.0], "DEP": [10.0, 40.0], "STOR": [0, 0]})
    out = _helpers.normalize(frame, "nwis", "ratings", CODE_META)
    assert list(out.columns) == _helpers.RATINGS_COLUMNS
    assert out["discharge"].iloc[1] == 40.0


def test_query_kwargs_legacy_sites_no_bbox():
    """Legacy values with explicit sites omit bBox and use sites=."""
    kw = _helpers.query_kwargs(
        service="daily",
        flavour="nwis",
        codes=["00060"],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    assert kw["sites"] == ["01646500"]
    assert "bBox" not in kw


def test_samples_kwargs_with_sites():
    """Samples with sites uses monitoringLocationIdentifier (prefixed)."""
    kw = _helpers.query_kwargs(
        service="samples",
        flavour="waterdata",
        codes=[],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2018-01-01",
        end="2018-12-31",
        limit=None,
    )
    assert kw["monitoringLocationIdentifier"] == ["USGS-01646500"]
    assert "boundingBox" not in kw


def test_statistics_modern_kwargs_with_sites():
    """Modern statistics builds parameter_code + monitoring_location_id."""
    kw = _helpers.query_kwargs(
        service="statistics",
        flavour="waterdata",
        codes=["00060"],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2020-01-01",
        end="2021-12-31",
        limit=None,
    )
    assert kw["parameter_code"] == "00060"
    assert kw["monitoring_location_id"] == ["USGS-01646500"]


def test_peaks_kwargs_modern_and_legacy():
    """Peaks builds monitoring_location_id (modern) / sites+start/end (legacy)."""
    modern = _helpers.query_kwargs(
        service="peaks",
        flavour="waterdata",
        codes=[],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="1990-01-01",
        end="2020-12-31",
        limit=None,
    )
    legacy = _helpers.query_kwargs(
        service="peaks",
        flavour="nwis",
        codes=[],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="1990-01-01",
        end="2020-12-31",
        limit=None,
    )
    assert modern["monitoring_location_id"] == ["USGS-01646500"]
    assert legacy["sites"] == ["01646500"]
    assert legacy["start"] == "1990-01-01"


def test_ratings_kwargs_modern_and_legacy():
    """Ratings is single-site: modern prefixes, legacy uses site=."""
    modern = _helpers.query_kwargs(
        service="ratings",
        flavour="waterdata",
        codes=[],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    legacy = _helpers.query_kwargs(
        service="ratings",
        flavour="nwis",
        codes=[],
        sites=["01646500"],
        bbox=[0, 0, 0, 0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    assert modern["monitoring_location_id"] == "USGS-01646500"
    assert legacy["site"] == "01646500"


def test_sites_kwargs_modern_with_limit_and_legacy():
    """Sites builds modern bbox+limit / legacy bBox string."""
    modern = _helpers.query_kwargs(
        service="sites",
        flavour="waterdata",
        codes=[],
        sites=None,
        bbox=[-77.2, 38.9, -77.0, 39.0],
        start="2023-01-01",
        end="2023-01-05",
        limit=5,
    )
    legacy = _helpers.query_kwargs(
        service="sites",
        flavour="nwis",
        codes=[],
        sites=None,
        bbox=[-77.2, 38.9, -77.0, 39.0],
        start="2023-01-01",
        end="2023-01-05",
        limit=None,
    )
    assert modern["bbox"] == [-77.2, 38.9, -77.0, 39.0]
    assert modern["limit"] == 5
    assert legacy["bBox"] == "-77.2,38.9,-77.0,39.0"


def test_normalize_peaks_modern():
    """Modern peaks normalize maps value->peak_value and strips prefix."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "monitoring_location_id": ["USGS-01646500"],
            "time": ["1990-03-01"],
            "value": [350000.0],
            "qualifier": ["5"],
        }
    )
    out = _helpers.normalize(frame, "waterdata", "peaks", CODE_META)
    assert out["site_no"].iloc[0] == "01646500"
    assert out["peak_value"].iloc[0] == 350000.0


def test_normalize_sites_modern():
    """Modern sites normalize maps monitoring-location columns."""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "monitoring_location_id": ["USGS-01646500"],
            "monitoring_location_name": ["POTOMAC"],
            "dec_lat_va": [38.9],
            "dec_long_va": [-77.1],
            "hydrologic_unit_code": ["02070008"],
            "site_type": ["Stream"],
        }
    )
    out = _helpers.normalize(frame, "waterdata", "sites", CODE_META)
    assert out["site_no"].iloc[0] == "01646500"
    assert out["station_name"].iloc[0] == "POTOMAC"


@pytest.mark.parametrize(
    "service", ["samples", "statistics", "sites", "peaks", "ratings"]
)
def test_normalize_empty_per_service(service):
    """Each service's normalize returns its own zero-row schema when empty."""
    import pandas as pd

    out = _helpers.normalize(pd.DataFrame(), "waterdata", service, CODE_META)
    assert out.empty
    assert len(out.columns) > 0


def test_normalize_legacy_wide_no_value_columns():
    """A legacy frame with no code columns normalizes to empty canonical."""
    import pandas as pd

    idx = pd.to_datetime(["2023-01-01"], utc=True)
    idx.name = "datetime"
    frame = pd.DataFrame({"site_no": ["01646500"]}, index=idx)
    out = _helpers.normalize(frame, "nwis", "daily", CODE_META)
    assert out.empty
    assert list(out.columns) == _helpers.CANONICAL_COLUMNS
