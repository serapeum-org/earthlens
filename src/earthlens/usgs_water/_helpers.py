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
#: `None` modern entry means "no modern function — always use legacy"
#: (none today; every service exists on both as of `dataretrieval`
#: 1.1.5, with `gwlevels` routed through the daily/continuous fns).
_SERVICE_FN: dict[str, dict[str, str | None]] = {
    "daily": {"waterdata": "get_daily", "nwis": "get_dv"},
    "instantaneous": {"waterdata": "get_continuous", "nwis": "get_iv"},
    "samples": {"waterdata": "get_samples", "nwis": "get_qwdata"},
    "statistics": {"waterdata": "get_stats_por", "nwis": "get_stats"},
    "gwlevels": {"waterdata": "get_daily", "nwis": "get_dv"},
    "field-measurements": {
        "waterdata": "get_field_measurements",
        "nwis": "get_discharge_measurements",
    },
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
    """Build the per-module query kwargs for one service call.

    Shapes the time filter, parameter-code filter, and site/bbox filter
    into the names each module expects (modern `parameter_code` +
    `time` + `bbox`; legacy `parameterCd` + `start`/`end` + `bBox`).

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
    kwargs: dict[str, Any] = {}
    if flavour == "waterdata":
        if codes:
            kwargs["parameter_code"] = codes if len(codes) > 1 else codes[0]
        if sites:
            kwargs.update(_site_filter("waterdata", sites))
        elif modern_supports_bbox(service):
            kwargs["bbox"] = list(bbox)
        if service not in ("statistics", "ratings", "peaks"):
            kwargs["time"] = f"{start}/{end}"
        if limit is not None:
            kwargs["limit"] = limit
        return kwargs

    # legacy nwis
    if codes:
        kwargs["parameterCd"] = codes if len(codes) > 1 else codes[0]
    if sites:
        kwargs.update(_site_filter("nwis", sites))
    elif service != "statistics":
        kwargs["bBox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    if service in ("daily", "instantaneous", "gwlevels", "peaks"):
        kwargs["start"] = start
        kwargs["end"] = end
    if service == "statistics":
        kwargs["statReportType"] = stat_type
    return kwargs


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
    code_meta: dict[str, tuple[str, str]],
) -> pd.DataFrame:
    """Dispatch a frame to the modern-long or legacy-wide normalizer.

    Args:
        df: The frame returned by a `dataretrieval` values call.
        flavour: `"waterdata"` (long) or `"nwis"` (wide).
        code_meta: Map from parameter code to `(name, units)`.

    Returns:
        pd.DataFrame: The canonical long-schema frame.
    """
    if flavour == "waterdata":
        return normalize_modern_long(df, code_meta)
    return normalize_legacy_wide(df, code_meta)


def empty_canonical() -> pd.DataFrame:
    """Return a zero-row frame with the canonical value-service columns.

    Returns:
        pd.DataFrame: Empty frame, :data:`CANONICAL_COLUMNS` columns.
    """
    return pd.DataFrame({column: [] for column in CANONICAL_COLUMNS})
