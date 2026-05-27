"""Service dispatch, query-kwarg shaping, and schema normalization.

This module is the correctness core of the multi-service USGS Water
backend: it maps the `service=` selector to the per-module
`dataretrieval` function (:data:`_SERVICE_FN`), builds the per-module
query keyword arguments (:func:`query_kwargs`), and folds every
service's return frame — the modern `waterdata` **long/tidy** shape and
the legacy `nwis` **wide** shape — into one canonical long schema
(:func:`normalize`).

The two endpoint flavours differ in both kwargs and return shape:

* **modern** (`waterdata`): `monitoring_location_id` (prefixed
  `"USGS-…"`), `parameter_code`, `time="START/END"`, `bbox=[W,S,E,N]`;
  returns a long frame with `monitoring_location_id` / `parameter_code`
  / `time` / `value` / `unit_of_measure` / `qualifier` /
  `statistic_id` columns.
* **legacy** (`nwis`): `sites`, `start`, `end`, `parameterCd`,
  `bBox="W,S,E,N"`; returns a wide frame with `<code>_<stat>` value
  columns + `<code>_<stat>_cd` qualifier columns + a `site_no` column
  and a `datetime` index.

The canonical output columns are :data:`CANONICAL_COLUMNS`.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

#: Per-service `dataretrieval` function names, by endpoint flavour. A
#: `None` legacy entry means "modern-only — the legacy function was
#: removed in `dataretrieval` 1.1.5", so there is no legacy fallback
#: (a 429 on these surfaces an error advising a token). `samples` and
#: `field-measurements` lost their legacy `nwis` functions
#: (`get_qwdata` / `get_discharge_measurements`).
_SERVICE_FN: dict[str, dict[str, str | None]] = {
    "daily": {"waterdata": "get_daily", "nwis": "get_dv"},
    "instantaneous": {"waterdata": "get_continuous", "nwis": "get_iv"},
    "samples": {"waterdata": "get_samples", "nwis": None},
    "statistics": {"waterdata": "get_stats_por", "nwis": "get_stats"},
    "gwlevels": {"waterdata": "get_daily", "nwis": "get_dv"},
    "field-measurements": {"waterdata": "get_field_measurements", "nwis": None},
    "peaks": {"waterdata": "get_peaks", "nwis": "get_discharge_peaks"},
    "ratings": {"waterdata": "get_ratings", "nwis": "get_ratings"},
    "sites": {"waterdata": "get_monitoring_locations", "nwis": "what_sites"},
}

#: Modern functions that accept a `bbox=` filter. `get_continuous`
#: (instantaneous) does **not**, so a bbox-only instantaneous query must
#: discover sites first (or fall back to legacy, which supports `bBox`).
_MODERN_BBOX_SERVICES: frozenset[str] = frozenset(
    {"daily", "samples", "field-measurements", "peaks", "ratings", "sites"}
)

#: The canonical long-schema columns every `normalize` result carries
#: for the values-style services (daily / instantaneous / gwlevels).
CANONICAL_COLUMNS: list[str] = [
    "site_no",
    "datetime",
    "parameter_code",
    "parameter_name",
    "value",
    "unit",
    "qualifier",
    "statistic_id",
]

#: Matches a legacy wide value column: `<5-digit code>` optionally
#: followed by `_<statistic label>` (e.g. `00060`, `00060_Mean`). The
#: paired qualifier column is the same name with a `_cd` suffix.
_WIDE_VALUE_RE = re.compile(r"^(?P<code>\d{5})(?:_(?P<stat>.+))?$")


def service_function(service: str, flavour: str) -> str | None:
    """Return the `dataretrieval` function name for a service + flavour.

    Args:
        service: One of the keys of :data:`_SERVICE_FN`.
        flavour: `"waterdata"` (modern) or `"nwis"` (legacy).

    Returns:
        str | None: The function name on that module, or `None` when
            the modern module has no equivalent.
    """
    return _SERVICE_FN[service][flavour]


def modern_supports_bbox(service: str) -> bool:
    """Whether the modern function for `service` accepts a `bbox=` filter.

    Args:
        service: One of the keys of :data:`_SERVICE_FN`.

    Returns:
        bool: `True` when the modern endpoint can be queried by bbox.
    """
    return service in _MODERN_BBOX_SERVICES


def is_rate_limit_error(exc: BaseException) -> bool:
    """Return `True` when an exception is a USGS rate-limit (HTTP 429).

    The modern `api.waterdata.usgs.gov` endpoint raises
    `QuotaExhausted` / `RateLimited` (or surfaces `429` in the message)
    once the anonymous quota is spent. Detected structurally — by the
    exception class name or a `429` token in the message — to avoid
    importing the SDK's private exception types.

    Args:
        exc: The exception raised by a `dataretrieval` call.

    Returns:
        bool: `True` for a rate-limit / quota-exhausted error.
    """
    name = type(exc).__name__
    if name in {"QuotaExhausted", "RateLimited"}:
        return True
    return "429" in str(exc) or "rate limit" in str(exc).lower()


def _site_filter(flavour: str, sites: list[str]) -> dict[str, Any]:
    """Build the site-selection kwargs for a flavour.

    Args:
        flavour: `"waterdata"` or `"nwis"`.
        sites: Bare USGS site numbers (`["01646500"]`).

    Returns:
        dict[str, Any]: `{"monitoring_location_id": ["USGS-…", …]}` for
            modern, `{"sites": [...]}` for legacy.
    """
    if flavour == "waterdata":
        return {"monitoring_location_id": [f"USGS-{s}" for s in sites]}
    return {"sites": list(sites)}


def _maybe(value: list[str], single_ok: bool = True) -> Any:
    """Return a lone element for a 1-list, else the list (SDK convenience)."""
    return value[0] if single_ok and len(value) == 1 else list(value)


def query_kwargs(
    *,
    service: str,
    flavour: str,
    codes: list[str],
    sites: list[str] | None,
    bbox: list[float],
    start: str,
    end: str,
    limit: int | None,
    stat_type: str = "daily",
) -> dict[str, Any]:
    """Build the per-module, per-service query kwargs for one call.

    Each service shapes the time / parameter / site / bbox filters into
    the names its `dataretrieval` function expects. The values services
    (daily / instantaneous / gwlevels) and `field-measurements` share
    the modern `parameter_code` + `time` + `bbox` form; `samples` uses
    the WQP camelCase form (`usgsPCode` / `boundingBox` /
    `activityStartDate*`); `sites` / `peaks` / `ratings` / `statistics`
    each have their own shape.

    Args:
        service: The selected service plane.
        flavour: `"waterdata"` (modern) or `"nwis"` (legacy).
        codes: Resolved 5-digit parameter codes.
        sites: Explicit bare site numbers, or `None` for a bbox query.
        bbox: `[west, south, east, north]` in degrees.
        start: Inclusive start date (`"%Y-%m-%d"`).
        end: Inclusive end date.
        limit: Optional modern `limit=` cap.
        stat_type: For `statistics`, the legacy `statReportType`.

    Returns:
        dict[str, Any]: Keyword arguments to splat into the resolved
            `dataretrieval` function.
    """
    if service == "samples":
        return _samples_kwargs(codes, sites, bbox, start, end)
    if service == "statistics":
        return _statistics_kwargs(flavour, codes, sites, stat_type)
    if service in ("peaks", "ratings"):
        return _site_keyed_kwargs(service, flavour, sites, bbox, start, end)
    if service == "sites":
        return _sites_kwargs(flavour, bbox, limit)
    return _values_kwargs(flavour, service, codes, sites, bbox, start, end, limit)


def _values_kwargs(
    flavour: str,
    service: str,
    codes: list[str],
    sites: list[str] | None,
    bbox: list[float],
    start: str,
    end: str,
    limit: int | None,
) -> dict[str, Any]:
    """Kwargs for daily / instantaneous / gwlevels / field-measurements."""
    kwargs: dict[str, Any] = {}
    if flavour == "waterdata":
        if codes:
            kwargs["parameter_code"] = _maybe(codes)
        if sites:
            kwargs.update(_site_filter("waterdata", sites))
        elif modern_supports_bbox(service):
            kwargs["bbox"] = list(bbox)
        kwargs["time"] = f"{start}/{end}"
        if limit is not None:
            kwargs["limit"] = limit
        return kwargs
    if codes:
        kwargs["parameterCd"] = _maybe(codes)
    if sites:
        kwargs.update(_site_filter("nwis", sites))
    else:
        kwargs["bBox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    kwargs["start"] = start
    kwargs["end"] = end
    return kwargs


def _samples_kwargs(
    codes: list[str],
    sites: list[str] | None,
    bbox: list[float],
    start: str,
    end: str,
) -> dict[str, Any]:
    """WQP-style camelCase kwargs for the modern `get_samples` call."""
    kwargs: dict[str, Any] = {
        "activityStartDateLower": start,
        "activityStartDateUpper": end,
    }
    if codes:
        kwargs["usgsPCode"] = _maybe(codes)
    if sites:
        kwargs["monitoringLocationIdentifier"] = [f"USGS-{s}" for s in sites]
    else:
        kwargs["boundingBox"] = list(bbox)
    return kwargs


def _statistics_kwargs(
    flavour: str,
    codes: list[str],
    sites: list[str] | None,
    stat_type: str,
) -> dict[str, Any]:
    """Kwargs for the statistics service (modern por / legacy get_stats)."""
    if flavour == "waterdata":
        kwargs: dict[str, Any] = {}
        if codes:
            kwargs["parameter_code"] = _maybe(codes)
        if sites:
            kwargs.update(_site_filter("waterdata", sites))
        return kwargs
    kwargs = {"statReportType": stat_type}
    if codes:
        kwargs["parameterCd"] = _maybe(codes)
    if sites:
        kwargs.update(_site_filter("nwis", sites))
    return kwargs


def _site_keyed_kwargs(
    service: str,
    flavour: str,
    sites: list[str] | None,
    bbox: list[float],
    start: str,
    end: str,
) -> dict[str, Any]:
    """Kwargs for the site-keyed services (`peaks`, `ratings`)."""
    sites = sites or []
    if service == "ratings":
        if flavour == "waterdata":
            return {"monitoring_location_id": f"USGS-{sites[0]}"}
        return {"site": sites[0]}
    # peaks
    if flavour == "waterdata":
        return _site_filter("waterdata", sites)
    return {"sites": list(sites), "start": start, "end": end}


def _sites_kwargs(flavour: str, bbox: list[float], limit: int | None) -> dict[str, Any]:
    """Kwargs for the site-discovery service."""
    if flavour == "waterdata":
        kwargs: dict[str, Any] = {"bbox": list(bbox)}
        if limit is not None:
            kwargs["limit"] = limit
        return kwargs
    return {"bBox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"}


def _strip_site_prefix(value: Any) -> Any:
    """Strip the modern `"USGS-"` site prefix to a bare site number.

    Args:
        value: A `monitoring_location_id` cell (`"USGS-01646500"`) or
            any non-string value (returned unchanged).

    Returns:
        The bare site number (`"01646500"`) or `value` unchanged.
    """
    if isinstance(value, str) and value.startswith("USGS-"):
        return value[len("USGS-") :]
    return value


def normalize_modern_long(
    df: pd.DataFrame, code_meta: dict[str, tuple[str, str]]
) -> pd.DataFrame:
    """Fold a modern `waterdata` long frame into the canonical schema.

    Args:
        df: A modern long frame (columns include
            `monitoring_location_id`, `parameter_code`, `time`,
            `value`, `unit_of_measure`, `qualifier`, `statistic_id`).
        code_meta: Map from parameter code to `(name, units)`, used to
            fill `parameter_name` (and `unit` when the frame omits it).

    Returns:
        pd.DataFrame: A frame with the :data:`CANONICAL_COLUMNS`.
    """
    if df is None or df.empty:
        return empty_canonical()
    out = pd.DataFrame(index=df.index)
    out["site_no"] = df.get("monitoring_location_id").map(_strip_site_prefix)
    out["datetime"] = pd.to_datetime(df.get("time"), errors="coerce", utc=True)
    out["parameter_code"] = df.get("parameter_code").astype("string")
    out["value"] = pd.to_numeric(df.get("value"), errors="coerce")
    out["unit"] = df.get("unit_of_measure")
    out["qualifier"] = df.get("qualifier")
    out["statistic_id"] = df.get("statistic_id")
    out["parameter_name"] = out["parameter_code"].map(
        lambda c: code_meta.get(c, ("", ""))[0]
    )
    return out.reset_index(drop=True)[CANONICAL_COLUMNS]


def normalize_legacy_wide(
    df: pd.DataFrame, code_meta: dict[str, tuple[str, str]]
) -> pd.DataFrame:
    """Melt a legacy `nwis` wide frame into the canonical long schema.

    Each `<code>` / `<code>_<stat>` value column (with its paired
    `…_cd` qualifier column) becomes a block of rows carrying that
    code, statistic label, value, and qualifier. The datetime comes
    from the frame's index; the site from the `site_no` column.

    Args:
        df: A legacy wide frame (datetime index, `site_no` column, and
            `<code>[_<stat>]` value / `…_cd` qualifier column pairs).
        code_meta: Map from parameter code to `(name, units)`, used to
            fill `parameter_name` and `unit` (absent from legacy frames).

    Returns:
        pd.DataFrame: A frame with the :data:`CANONICAL_COLUMNS`.
    """
    if df is None or df.empty:
        return empty_canonical()
    frame = df.reset_index()
    datetime_col = "datetime" if "datetime" in frame.columns else frame.columns[0]
    site_col = "site_no" if "site_no" in frame.columns else None
    blocks: list[pd.DataFrame] = []
    for column in frame.columns:
        match = _WIDE_VALUE_RE.match(str(column))
        if not match or str(column).endswith("_cd"):
            continue
        code = match.group("code")
        stat = match.group("stat") or ""
        qualifier_col = f"{column}_cd"
        name, units = code_meta.get(code, ("", ""))
        block = pd.DataFrame(
            {
                "site_no": frame[site_col] if site_col else pd.NA,
                "datetime": pd.to_datetime(
                    frame[datetime_col], errors="coerce", utc=True
                ),
                "parameter_code": code,
                "parameter_name": name,
                "value": pd.to_numeric(frame[column], errors="coerce"),
                "unit": units,
                "qualifier": (
                    frame[qualifier_col] if qualifier_col in frame.columns else pd.NA
                ),
                "statistic_id": stat,
            }
        )
        blocks.append(block)
    if not blocks:
        return empty_canonical()
    return pd.concat(blocks, ignore_index=True)[CANONICAL_COLUMNS]


def normalize(
    df: pd.DataFrame,
    flavour: str,
    service: str,
    code_meta: dict[str, tuple[str, str]],
) -> pd.DataFrame:
    """Fold a service's frame into its canonical long schema.

    Dispatches per service: the values services (daily / instantaneous
    / gwlevels) share the modern-long / legacy-wide normalizers;
    `samples`, `statistics`, `sites`, `peaks`, and `ratings` each have
    a bespoke schema.

    Args:
        df: The frame returned by a `dataretrieval` call.
        flavour: `"waterdata"` (long) or `"nwis"` (wide / legacy).
        service: The selected service plane.
        code_meta: Map from parameter code to `(name, units)`.

    Returns:
        pd.DataFrame: The canonical frame for this service.
    """
    if service == "samples":
        return normalize_samples(df, code_meta)
    if service == "statistics":
        return normalize_statistics(df, flavour, code_meta)
    if service == "sites":
        return normalize_sites(df, flavour)
    if service == "peaks":
        return normalize_peaks(df, flavour)
    if service == "ratings":
        return normalize_ratings(df, flavour)
    if flavour == "waterdata":
        return normalize_modern_long(df, code_meta)
    return normalize_legacy_wide(df, code_meta)


#: Output columns for the water-quality `samples` service (canonical
#: core + QW result fields).
SAMPLES_COLUMNS: list[str] = [
    "site_no",
    "datetime",
    "parameter_code",
    "characteristic",
    "value",
    "unit",
    "qualifier",
    "detection_condition",
    "detection_limit",
    "detection_limit_unit",
    "method",
    "fraction",
    "medium",
]

#: Output columns for the `statistics` service.
STATS_COLUMNS: list[str] = [
    "site_no",
    "parameter_code",
    "parameter_name",
    "time_of_year",
    "value",
    "percentile",
    "statistic",
    "unit",
]

#: Output columns for the site-discovery (`sites`) service.
SITE_COLUMNS: list[str] = [
    "site_no",
    "station_name",
    "latitude",
    "longitude",
    "huc",
    "site_type",
]

#: Output columns for the annual-peak (`peaks`) service.
PEAKS_COLUMNS: list[str] = [
    "site_no",
    "datetime",
    "peak_value",
    "gage_height",
    "qualifier",
]

#: Output columns for the stage-discharge rating (`ratings`) service.
RATINGS_COLUMNS: list[str] = ["stage", "discharge", "storage"]


def _first_column(df: pd.DataFrame, names: list[str], default: Any = pd.NA) -> Any:
    """Return the first present column among `names`, else a scalar `default`."""
    for name in names:
        if name in df.columns:
            return df[name]
    return default


def normalize_samples(
    df: pd.DataFrame, code_meta: dict[str, tuple[str, str]]
) -> pd.DataFrame:
    """Fold a modern WQP `get_samples` profile into the samples schema.

    Maps the WQP result-level columns (`Result_Measure`,
    `Result_MeasureQualifierCode`, `DetectionLimit_MeasureA`,
    `ResultAnalyticalMethod_Name`, …) onto :data:`SAMPLES_COLUMNS`.

    Args:
        df: The modern `get_samples` frame (the WQP profile).
        code_meta: Map from parameter code to `(name, units)`
            (unused for value/unit, which come from the result row;
            kept for signature symmetry).

    Returns:
        pd.DataFrame: A frame with the :data:`SAMPLES_COLUMNS`.
    """
    if df is None or df.empty:
        return pd.DataFrame({column: [] for column in SAMPLES_COLUMNS})
    out = pd.DataFrame(index=df.index)
    out["site_no"] = _first_column(df, ["Location_Identifier"]).map(_strip_site_prefix)
    out["datetime"] = pd.to_datetime(
        _first_column(df, ["Activity_StartDateTime", "Activity_StartDate"]),
        errors="coerce",
        utc=True,
    )
    out["parameter_code"] = _first_column(df, ["USGSpcode"])
    out["characteristic"] = _first_column(df, ["Result_Characteristic"])
    out["value"] = pd.to_numeric(_first_column(df, ["Result_Measure"]), errors="coerce")
    out["unit"] = _first_column(df, ["Result_MeasureUnit"])
    out["qualifier"] = _first_column(df, ["Result_MeasureQualifierCode"])
    out["detection_condition"] = _first_column(df, ["Result_ResultDetectionCondition"])
    out["detection_limit"] = pd.to_numeric(
        _first_column(df, ["DetectionLimit_MeasureA"]), errors="coerce"
    )
    out["detection_limit_unit"] = _first_column(df, ["DetectionLimit_MeasureUnitA"])
    out["method"] = _first_column(df, ["ResultAnalyticalMethod_Name"])
    out["fraction"] = _first_column(df, ["Result_SampleFraction"])
    out["medium"] = _first_column(df, ["Activity_Media"])
    return out.reset_index(drop=True)[SAMPLES_COLUMNS]


def normalize_statistics(
    df: pd.DataFrame, flavour: str, code_meta: dict[str, tuple[str, str]]
) -> pd.DataFrame:
    """Fold a statistics frame (modern por / legacy get_stats) to long.

    Args:
        df: The statistics frame.
        flavour: `"waterdata"` (long: `value`/`percentile`/
            `time_of_year`) or `"nwis"` (`year_nu`/`month_nu`/`mean_va`).
        code_meta: Map from parameter code to `(name, units)`.

    Returns:
        pd.DataFrame: A frame with the :data:`STATS_COLUMNS`.
    """
    if df is None or df.empty:
        return pd.DataFrame({column: [] for column in STATS_COLUMNS})
    out = pd.DataFrame(index=df.index)
    if flavour == "waterdata":
        out["site_no"] = _first_column(df, ["monitoring_location_id"]).map(
            _strip_site_prefix
        )
        out["parameter_code"] = _first_column(df, ["parameter_code"])
        out["time_of_year"] = _first_column(df, ["time_of_year"])
        out["value"] = pd.to_numeric(_first_column(df, ["value"]), errors="coerce")
        out["percentile"] = _first_column(df, ["percentile"])
        out["statistic"] = _first_column(df, ["computation"])
        out["unit"] = _first_column(df, ["unit_of_measure"])
    else:
        out["site_no"] = _first_column(df, ["site_no"])
        out["parameter_code"] = _first_column(df, ["parameter_cd"])
        year = _first_column(df, ["year_nu"], default="")
        month = _first_column(df, ["month_nu"], default="")
        out["time_of_year"] = year.astype("string").fillna("") + _join_month(month)
        out["value"] = pd.to_numeric(_first_column(df, ["mean_va"]), errors="coerce")
        out["percentile"] = pd.NA
        out["statistic"] = "mean"
        out["unit"] = pd.NA
    out["parameter_name"] = out["parameter_code"].map(
        lambda c: code_meta.get(str(c), ("", ""))[0]
    )
    return out.reset_index(drop=True)[STATS_COLUMNS]


def _join_month(month: Any) -> Any:
    """Render a legacy `month_nu` column as a `-MM` suffix (or empty)."""
    if not isinstance(month, pd.Series):
        return ""
    return month.map(lambda m: f"-{int(m):02d}" if pd.notna(m) else "")


def normalize_sites(df: pd.DataFrame, flavour: str) -> pd.DataFrame:
    """Fold a site frame (modern monitoring-locations / legacy what_sites).

    Args:
        df: The site-metadata frame.
        flavour: `"waterdata"` or `"nwis"`.

    Returns:
        pd.DataFrame: A frame with the :data:`SITE_COLUMNS`.
    """
    if df is None or df.empty:
        return pd.DataFrame({column: [] for column in SITE_COLUMNS})
    out = pd.DataFrame(index=df.index)
    if flavour == "waterdata":
        out["site_no"] = _first_column(df, ["monitoring_location_id"]).map(
            _strip_site_prefix
        )
        out["station_name"] = _first_column(df, ["monitoring_location_name"])
        out["latitude"] = pd.to_numeric(
            _first_column(df, ["dec_lat_va"]), errors="coerce"
        )
        out["longitude"] = pd.to_numeric(
            _first_column(df, ["dec_long_va"]), errors="coerce"
        )
        out["huc"] = _first_column(df, ["hydrologic_unit_code"])
        out["site_type"] = _first_column(df, ["site_type"])
    else:
        out["site_no"] = _first_column(df, ["site_no"])
        out["station_name"] = _first_column(df, ["station_nm"])
        out["latitude"] = pd.to_numeric(
            _first_column(df, ["dec_lat_va"]), errors="coerce"
        )
        out["longitude"] = pd.to_numeric(
            _first_column(df, ["dec_long_va"]), errors="coerce"
        )
        out["huc"] = _first_column(df, ["huc_cd"])
        out["site_type"] = _first_column(df, ["site_tp_cd"])
    return out.reset_index(drop=True)[SITE_COLUMNS]


def normalize_peaks(df: pd.DataFrame, flavour: str) -> pd.DataFrame:
    """Fold an annual-peak frame (modern get_peaks / legacy peaks) to long.

    Args:
        df: The peaks frame.
        flavour: `"waterdata"` or `"nwis"`.

    Returns:
        pd.DataFrame: A frame with the :data:`PEAKS_COLUMNS`.
    """
    if df is None or df.empty:
        return pd.DataFrame({column: [] for column in PEAKS_COLUMNS})
    frame = df.reset_index()
    out = pd.DataFrame(index=frame.index)
    if flavour == "waterdata":
        out["site_no"] = _first_column(frame, ["monitoring_location_id"]).map(
            _strip_site_prefix
        )
        out["datetime"] = pd.to_datetime(
            _first_column(frame, ["time"]), errors="coerce", utc=True
        )
        out["peak_value"] = pd.to_numeric(
            _first_column(frame, ["value"]), errors="coerce"
        )
        out["gage_height"] = pd.NA
        out["qualifier"] = _first_column(frame, ["qualifier"])
    else:
        out["site_no"] = _first_column(frame, ["site_no"])
        out["datetime"] = pd.to_datetime(
            _first_column(frame, ["datetime", "peak_dt"]), errors="coerce", utc=True
        )
        out["peak_value"] = pd.to_numeric(
            _first_column(frame, ["peak_va"]), errors="coerce"
        )
        out["gage_height"] = pd.to_numeric(
            _first_column(frame, ["gage_ht"]), errors="coerce"
        )
        out["qualifier"] = _first_column(frame, ["peak_cd"])
    return out.reset_index(drop=True)[PEAKS_COLUMNS]


def normalize_ratings(df: pd.DataFrame, flavour: str) -> pd.DataFrame:
    """Fold a stage-discharge rating curve into `stage`/`discharge`/`storage`.

    Args:
        df: The ratings frame (legacy `INDEP`/`DEP`/`STOR`, or a modern
            ratings frame with the same triple).
        flavour: `"waterdata"` or `"nwis"`.

    Returns:
        pd.DataFrame: A frame with the :data:`RATINGS_COLUMNS`.
    """
    if df is None or df.empty:
        return pd.DataFrame({column: [] for column in RATINGS_COLUMNS})
    out = pd.DataFrame(index=df.index)
    out["stage"] = pd.to_numeric(_first_column(df, ["INDEP", "stage"]), errors="coerce")
    out["discharge"] = pd.to_numeric(
        _first_column(df, ["DEP", "discharge"]), errors="coerce"
    )
    out["storage"] = pd.to_numeric(
        _first_column(df, ["STOR", "storage"]), errors="coerce"
    )
    return out.reset_index(drop=True)[RATINGS_COLUMNS]


def empty_canonical() -> pd.DataFrame:
    """Return a zero-row frame with the canonical value-service columns.

    Returns:
        pd.DataFrame: Empty frame, :data:`CANONICAL_COLUMNS` columns.
    """
    return pd.DataFrame({column: [] for column in CANONICAL_COLUMNS})
