"""Stateless helpers for the openEO backend.

Hosts the small, pure functions and constant tables the backend and auth
modules share: the openEO endpoint registry, the lazy `openeo` SDK importer
(which turns a missing `[openeo]` extra into a friendly `ImportError`), and the
pandas-frequency → openEO calendar-`period` mapping that lets a request's
`aggregate=AggregationConfig(freq=...)` translate into a server-side
`aggregate_temporal_period` node.

None of these touch the network or hold state, so they are trivially unit
testable and safe to import without the `openeo` SDK installed.
"""

from __future__ import annotations

from typing import Any

#: Named openEO endpoints. Keys are the friendly aliases accepted by the
#: `endpoint=` kwarg; values are the STAC-style API root URLs verified against
#: the live services (A1). The default is CDSE core (free with a CDSE account).
OPENEO_ENDPOINTS: dict[str, str] = {
    "cdse": "https://openeo.dataspace.copernicus.eu",
    "cdse-federation": "https://openeofed.dataspace.copernicus.eu",
    "openeo-platform": "https://openeo.cloud",
}

#: Default endpoint URL when `endpoint=` is omitted (CDSE core).
DEFAULT_ENDPOINT: str = OPENEO_ENDPOINTS["cdse"]

#: openEO output formats the backend writes, mapped to their on-disk suffix.
OUTPUT_FORMATS: dict[str, str] = {"GTiff": "tif", "netCDF": "nc"}

#: openEO reducer process names accepted by `aggregate_temporal_period`'s
#: `reducer=` argument (verified on the CDSE backend, A1). `AggregationConfig.op`
#: must resolve to one of these.
OPENEO_REDUCERS: frozenset[str] = frozenset(
    {"mean", "median", "min", "max", "sum", "sd", "first", "last"}
)

# pandas offset alias (with any leading count stripped) → openEO calendar
# `period`. openEO `period` is a *calendar vocabulary*, not a pandas freq, so a
# request's `AggregationConfig.freq` ("1MS", "7D", "10D", "YS", …) is mapped
# here before it reaches `aggregate_temporal_period(period=…)`. The accepted
# enum on CDSE (A1) is hour|day|week|dekad|month|season|tropical-season|year|
# decade|decade-ad — `dekad` is a 10-day third of a month.
_FREQ_TO_PERIOD: dict[str, str] = {
    "h": "hour",
    "H": "hour",
    "D": "day",
    "W": "week",
    "10D": "dekad",
    "MS": "month",
    "M": "month",
    "ME": "month",
    "QS": "season",
    "Q": "season",
    "QE": "season",
    "YS": "year",
    "Y": "year",
    "YE": "year",
    "A": "year",
    "AS": "year",
}


def resolve_endpoint(endpoint: str | None) -> str:
    """Resolve an `endpoint=` value to a concrete openEO API root URL.

    Accepts a named alias (`"cdse"`, `"cdse-federation"`, `"openeo-platform"`),
    a full `http(s)://` URL (used verbatim), or `None` (the CDSE-core default).

    Args:
        endpoint: A named alias, a full URL, or `None`.

    Returns:
        The resolved API root URL.

    Examples:
        - A named alias maps to its URL:
            ```python
            >>> from earthlens.openeo._helpers import resolve_endpoint
            >>> resolve_endpoint("cdse")
            'https://openeo.dataspace.copernicus.eu'

            ```
        - `None` falls back to the CDSE-core default:
            ```python
            >>> from earthlens.openeo._helpers import resolve_endpoint
            >>> resolve_endpoint(None)
            'https://openeo.dataspace.copernicus.eu'

            ```
        - A full URL is returned unchanged:
            ```python
            >>> from earthlens.openeo._helpers import resolve_endpoint
            >>> resolve_endpoint("https://example.org/openeo")
            'https://example.org/openeo'

            ```
    """
    if endpoint is None:
        return DEFAULT_ENDPOINT
    if endpoint in OPENEO_ENDPOINTS:
        return OPENEO_ENDPOINTS[endpoint]
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    raise ValueError(
        f"unknown openEO endpoint {endpoint!r}: pass one of "
        f"{sorted(OPENEO_ENDPOINTS)} or a full http(s):// URL."
    )


def period_for(freq: str) -> str:
    """Map a pandas frequency alias to an openEO `aggregate_temporal_period` period.

    Strips any leading repeat count (`"1MS"` → `"MS"`) before the lookup, except
    for `"10D"` which maps to the calendar `dekad` directly.

    Args:
        freq: A pandas offset alias, optionally count-prefixed (`"1MS"`, `"7D"`,
            `"10D"`, `"YS"`, …).

    Returns:
        The matching openEO calendar period (`"day"`, `"week"`, `"dekad"`,
        `"month"`, `"season"`, `"year"`, …).

    Raises:
        NotImplementedError: If `freq` has no calendar-period equivalent.

    Examples:
        - Month-start maps to the calendar month period:
            ```python
            >>> from earthlens.openeo._helpers import period_for
            >>> period_for("1MS")
            'month'

            ```
        - A 10-day window is a dekad:
            ```python
            >>> from earthlens.openeo._helpers import period_for
            >>> period_for("10D")
            'dekad'

            ```
    """
    if freq in _FREQ_TO_PERIOD:
        return _FREQ_TO_PERIOD[freq]
    key = freq.lstrip("0123456789")
    if key in _FREQ_TO_PERIOD:
        return _FREQ_TO_PERIOD[key]
    raise NotImplementedError(
        f"openEO aggregate_temporal_period has no calendar period for "
        f"freq={freq!r}; supported pandas aliases map to "
        f"{sorted(set(_FREQ_TO_PERIOD.values()))}."
    )


def reducer_for(op: str) -> str:
    """Validate an aggregation op against the openEO reducer process names.

    Maps the aggregator's `"auto"` to `"mean"` and otherwise passes `op`
    straight through after checking it is a reducer the backend accepts.

    Args:
        op: The `AggregationConfig.op` value (`"auto"`, `"mean"`, `"median"`,
            `"min"`, `"max"`, `"sum"`, `"sd"`, …).

    Returns:
        The openEO reducer process name to pass to `aggregate_temporal_period`.

    Raises:
        NotImplementedError: If `op` is not a known openEO reducer.

    Examples:
        - `"auto"` resolves to `mean`:
            ```python
            >>> from earthlens.openeo._helpers import reducer_for
            >>> reducer_for("auto")
            'mean'

            ```
    """
    resolved = "mean" if op == "auto" else op
    if resolved not in OPENEO_REDUCERS:
        raise NotImplementedError(
            f"openEO reducer {op!r} is not supported; use one of "
            f"{sorted(OPENEO_REDUCERS)}."
        )
    return resolved


def import_openeo() -> Any:
    """Import and return the `openeo` SDK, or raise a friendly `ImportError`.

    The SDK is an optional dependency (`pip install earthlens[openeo]`); this
    helper centralises the lazy import so every call site surfaces the same
    actionable message when the extra is missing.

    Returns:
        The imported `openeo` module.

    Raises:
        ImportError: When the `openeo` extra is not installed.
    """
    try:
        import openeo
    except ImportError as exc:  # pragma: no cover - exercised via a stubbed import
        raise ImportError(
            "the openEO backend requires the 'openeo' client. Install it with "
            "`pip install earthlens[openeo]`."
        ) from exc
    return openeo
