"""Pure per-source HTTP query builders and JSON parsers for risk-indicators.

Every risk source here is a REST endpoint returning JSON, parsed into a
:class:`pandas.DataFrame` (ThinkHazard / INFORM / GFW table) or a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` (GFW geometry). These are
pre-computed country/admin-indexed indices and SQL queries, so this subpackage
uses **no gridded-array dependency** by design — the parse is pure pandas /
geopandas text-and-JSON.

The module groups three concerns, none of which holds state:

* **Query builders** — :func:`thinkhazard_query`, :func:`inform_query`,
  :func:`gfw_query`, :func:`gfw_geostore` issue one GET each and return parsed
  JSON. Only the GFW calls attach the `x-api-key` header.
* **Parsers** — :func:`thinkhazard_to_frame`, :func:`inform_to_frame`,
  :func:`to_frame`, :func:`to_feature_collection` turn a source's JSON into the
  canonical tabular / vector shape.
* **Resolution** — :func:`resolve_admin` turns a country ISO3 into the
  ThinkHazard ADM0 division code via the catalog's shipped lookup table.

Endpoints / response shapes were live-verified 2026-06-27 (see
the A1 gate captures).
"""

from __future__ import annotations

import time
from typing import cast

import geopandas as gpd
import pandas as pd
import requests
from pyramids.feature.collection import FeatureCollection

from earthlens.base.http import HttpClient

#: ThinkHazard! public REST base (no auth). Hazard reports live under
#: `/report/{division_code}.json` and `/report/{division_code}/{hazard}.json`.
THINKHAZARD_BASE = "https://thinkhazard.org/en"

#: INFORM Risk (JRC) public REST base (no auth). Country scores live under
#: `/countries/Scores/?WorkflowId={id}&IndicatorId={ind}`.
INFORM_BASE = "https://drmkc.jrc.ec.europa.eu/inform-index/API/InformAPI"

#: GFW Data API base. SQL queries live under
#: `/dataset/{dataset}/{version}/query/json`; admin geometry under
#: `/geostore/admin/{iso}`. Both require the `x-api-key` header.
GFW_BASE = "https://data-api.globalforestwatch.org"

#: The header GFW expects the API key on.
GFW_KEY_HEADER = "x-api-key"

#: A `User-Agent` is sent on every request — the GFW host resets connections
#: that carry no UA. Identifies earthlens without leaking anything sensitive.
_USER_AGENT = "earthlens-risk-indicators"

#: Default per-request HTTP timeout (seconds).
_HTTP_TIMEOUT: float = 120.0

#: Extra attempts after the first on a transient failure (connection reset /
#: timeout / 5xx). The GFW host in particular resets connections intermittently.
_HTTP_RETRIES: int = 2

#: Base back-off (seconds) between retries; the nth retry waits
#: `_HTTP_RETRY_BACKOFF * 2**(n-1)` (HttpClient's exponential back-off).
#: Coincides with the previous hand-rolled linear back-off at
#: `_HTTP_RETRIES=2` (both yield `[1s, 2s]`); bumping `_HTTP_RETRIES` diverges
#: the two (linear grows as `n`, exponential as `2**(n-1)`), whereas scaling
#: `_HTTP_RETRY_BACKOFF` rescales both by the same factor.
_HTTP_RETRY_BACKOFF: float = 1.0


#: Exception types treated as transient (worth a retry). Covers a reset during
#: the handshake (`ConnectionError`), a timeout, and — the common GFW case — a
#: reset *mid-body* while streaming the response, which `requests` raises as a
#: `ChunkedEncodingError` (a sibling of `ConnectionError`, not a subclass).
_TRANSIENT_ERRORS: tuple[type[requests.RequestException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


def _request_json(
    url: str,
    *,
    params: dict | None,
    headers: dict[str, str],
    timeout: float,
) -> dict | list:
    """GET `url` and return parsed JSON, retrying transient failures.

    Retries are delegated to :class:`~earthlens.base.http.HttpClient`: a 5xx
    response or a transient transport error (see :data:`_TRANSIENT_ERRORS`)
    is retried up to :data:`_HTTP_RETRIES` times with exponential back-off
    (`1s`, `2s`); a 4xx (including 429) fails fast.

    Args:
        url: The request URL.
        params: Query parameters, or `None`.
        headers: Extra request headers (e.g. the GFW `x-api-key` on the
            keyed sources; the `User-Agent` is a client-level default).
        timeout: Per-request timeout in seconds.

    Returns:
        The parsed JSON body.

    Raises:
        requests.RequestException: When the GET still fails after the retries
            (the last error is re-raised; a 4xx fails fast without retrying).
    """
    client = HttpClient(
        user_agent=_USER_AGENT,
        timeout=timeout,
        max_retries=_HTTP_RETRIES,
        backoff_factor=_HTTP_RETRY_BACKOFF,
        status_forcelist=tuple(range(500, 600)),
        retry_on_exceptions=_TRANSIENT_ERRORS,
        raise_for_status=True,
        sleep=lambda seconds: time.sleep(seconds),
    )
    return cast("dict | list", client.get_json(url, params=params, headers=headers))


#: Canonical column order for a ThinkHazard hazard-level table.
THINKHAZARD_COLUMNS: list[str] = [
    "country",
    "admin_code",
    "hazard",
    "hazard_type",
    "level",
    "level_title",
]

#: Canonical column order for an INFORM country-score table.
INFORM_COLUMNS: list[str] = ["iso3", "indicator_id", "indicator_score", "validity_year"]

#: ThinkHazard hazard-level title -> mnemonic. The all-hazards list returns the
#: mnemonic (`"HIG"`) while the single-hazard report returns the title word
#: (`"High"`); the parser normalises both reports to carry both forms.
_LEVEL_TITLE_TO_MNEMONIC: dict[str, str] = {
    "Very low": "VLO",
    "Low": "LOW",
    "Medium": "MED",
    "High": "HIG",
}


def _headers(api_key: str | None = None) -> dict[str, str]:
    """Build the per-call GFW `x-api-key` header, when given.

    The descriptive `User-Agent` rides on every request via `HttpClient`'s
    client-level default (set in :func:`_request_json`), so it does not need
    to be re-set here — a per-call `User-Agent` header would only shadow the
    identical default.

    Args:
        api_key: The GFW `x-api-key`, or `None` for the keyless public sources.

    Returns:
        dict[str, str]: `{GFW_KEY_HEADER: api_key}` when a key is given, else
            an empty dict.
    """
    return {GFW_KEY_HEADER: api_key} if api_key is not None else {}


def thinkhazard_query(
    admin_code: str,
    hazard: str | None = None,
    *,
    base: str = THINKHAZARD_BASE,
    timeout: float = _HTTP_TIMEOUT,
) -> list | dict:
    """Fetch ThinkHazard! hazard levels for a division (public, no key).

    Args:
        admin_code: The ThinkHazard ADM0 division code (`"133"` for Kenya).
        hazard: A hazard mnemonic (`"FL"`) for the single-hazard report, or
            `None` for the all-hazards list.
        base: The ThinkHazard base URL (overridable for tests).
        timeout: Per-request timeout in seconds.

    Returns:
        The parsed JSON: a `list` of `{hazardtype, hazardlevel}` for the
        all-hazards report, or a `dict` carrying `hazard_category` for a single
        hazard.

    Raises:
        requests.HTTPError: If the endpoint returns a non-2xx status.
    """
    suffix = f"/{hazard}" if hazard else ""
    url = f"{base}/report/{admin_code}{suffix}.json"
    return _request_json(url, params=None, headers=_headers(), timeout=timeout)


def inform_query(
    workflow_id: int,
    indicator_id: str,
    *,
    base: str = INFORM_BASE,
    timeout: float = _HTTP_TIMEOUT,
) -> list:
    """Fetch INFORM country scores for one indicator (public, no key).

    Args:
        workflow_id: The INFORM model WorkflowId (e.g. `503` for INFORM Risk
            Mid 2025).
        indicator_id: The indicator id (`"INFORM"`, `"HA"`, `"VU"`, `"CC"`).
        base: The INFORM API base URL (overridable for tests).
        timeout: Per-request timeout in seconds.

    Returns:
        list: The ISO3-keyed score rows
            (`[{"Iso3": ..., "IndicatorScore": ...}, ...]`).

    Raises:
        requests.HTTPError: If the endpoint returns a non-2xx status.
    """
    url = f"{base}/countries/Scores/"
    params = {"WorkflowId": workflow_id, "IndicatorId": indicator_id}
    return cast(
        "list", _request_json(url, params=params, headers=_headers(), timeout=timeout)
    )


def gfw_query(
    dataset: str,
    version: str,
    sql: str,
    *,
    api_key: str,
    base: str = GFW_BASE,
    timeout: float = _HTTP_TIMEOUT,
) -> dict:
    """Run a GFW Data API SQL query, returning the parsed JSON (keyed).

    Args:
        dataset: The GFW dataset id (`"gadm__tcl__iso_change"`).
        version: The dataset version (`"v20260424"`).
        sql: The SQL query string (already parameterised by the caller).
        api_key: The GFW `x-api-key`; attached as the `x-api-key` header.
        base: The GFW base URL (overridable for tests).
        timeout: Per-request timeout in seconds.

    Returns:
        dict: The parsed response (`{"status": "success", "data": [...]}`).

    Raises:
        requests.HTTPError: On a non-2xx status (a missing/invalid key is a
            403 from GFW).
    """
    url = f"{base}/dataset/{dataset}/{version}/query/json"
    return cast(
        "dict",
        _request_json(
            url, params={"sql": sql}, headers=_headers(api_key), timeout=timeout
        ),
    )


def gfw_geostore(
    iso: str,
    *,
    api_key: str,
    admin: tuple[str, ...] = (),
    base: str = GFW_BASE,
    timeout: float = _HTTP_TIMEOUT,
) -> dict:
    """Fetch the GADM admin-boundary geostore geometry for a country (keyed).

    Args:
        iso: An ISO3 country code (`"KEN"`).
        api_key: The GFW `x-api-key`; attached as the `x-api-key` header.
        admin: Optional extra path segments for a sub-national division
            (`("1",)` for an ADM1, `("1", "2")` for an ADM2).
        base: The GFW base URL (overridable for tests).
        timeout: Per-request timeout in seconds.

    Returns:
        dict: The geostore payload; the GeoJSON lives at
            `data.attributes.geojson`.

    Raises:
        requests.HTTPError: On a non-2xx status.
    """
    parts = "/".join((iso, *admin))
    url = f"{base}/geostore/admin/{parts}"
    return cast(
        "dict",
        _request_json(url, params=None, headers=_headers(api_key), timeout=timeout),
    )


def to_frame(payload: dict | list, columns: list[str] | None = None) -> pd.DataFrame:
    """Build a flat :class:`pandas.DataFrame` from a JSON rows payload.

    Accepts either a GFW-style `{"data": [...]}` envelope or a bare list of row
    dicts; a single dict (not under `data`) becomes a one-row frame.

    Args:
        payload: The parsed JSON — a `{"data": [...]}` dict, a list of rows, or
            a single row dict.
        columns: Optional explicit column order; `None` infers from the rows.

    Returns:
        pd.DataFrame: One row per record (empty-but-typed when there are no
            rows and `columns` is given).
    """
    if isinstance(payload, dict) and "data" in payload:
        rows = payload["data"]
    else:
        rows = payload
    if not isinstance(rows, list):
        rows = [rows]
    if not rows and columns is not None:
        return empty_canonical(columns)
    return pd.DataFrame(rows, columns=columns)


def thinkhazard_to_frame(
    payload: list | dict,
    *,
    admin_code: str,
    hazard: str | None = None,
    country: str | None = None,
) -> pd.DataFrame:
    """Flatten a ThinkHazard report into the canonical hazard-level table.

    Handles both report shapes: the all-hazards `list` of
    `{hazardtype, hazardlevel}` rows, and the single-hazard `dict` carrying
    `hazard_category`.

    Args:
        payload: The JSON returned by :func:`thinkhazard_query`.
        admin_code: The division code the report was fetched for (stamped onto
            every row).
        hazard: The hazard mnemonic for a single-hazard report; ignored for the
            all-hazards list (each row carries its own).
        country: Optional ISO3 to stamp onto every row.

    Returns:
        pd.DataFrame: Columns :data:`THINKHAZARD_COLUMNS` — one row per hazard.
    """
    records: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            hazardtype = item.get("hazardtype", {})
            hazardlevel = item.get("hazardlevel", {})
            records.append(
                {
                    "hazard": hazardtype.get("mnemonic"),
                    "hazard_type": hazardtype.get("hazardtype"),
                    "level": hazardlevel.get("mnemonic"),
                    "level_title": hazardlevel.get("title"),
                }
            )
    else:
        category = payload.get("hazard_category", {}) if payload else {}
        title = category.get("hazard_level")
        records.append(
            {
                "hazard": hazard,
                "hazard_type": category.get("hazard_type"),
                "level": _LEVEL_TITLE_TO_MNEMONIC.get(cast("str", title), title),
                "level_title": title,
            }
        )
    frame = pd.DataFrame(records)
    frame.insert(0, "admin_code", admin_code)
    frame.insert(0, "country", country)
    return frame.reindex(columns=THINKHAZARD_COLUMNS)


def inform_to_frame(payload: list, *, country: str | None = None) -> pd.DataFrame:
    """Reshape INFORM country scores into the canonical score table.

    Args:
        payload: The score rows from :func:`inform_query`.
        country: Optional ISO3 to filter to a single country (case-insensitive);
            `None` keeps every country.

    Returns:
        pd.DataFrame: Columns :data:`INFORM_COLUMNS` — one row per country (or
            one row for the filtered country).
    """
    records = [
        {
            "iso3": row.get("Iso3"),
            "indicator_id": row.get("IndicatorId"),
            "indicator_score": row.get("IndicatorScore"),
            "validity_year": row.get("ValidityYear"),
        }
        for row in (payload or [])
    ]
    frame = pd.DataFrame(records, columns=INFORM_COLUMNS)
    if country is not None:
        frame = frame[frame["iso3"].str.upper() == country.strip().upper()]
    return frame.reset_index(drop=True)


def gfw_geostore_to_feature_collection(payload: dict) -> FeatureCollection:
    """Extract the GeoJSON from a GFW geostore payload and wrap it.

    The geostore response nests the boundary at `data.attributes.geojson`; this
    digs it out with a descriptive error (rather than a bare `KeyError`) when an
    unexpected payload shape comes back, then delegates to
    :func:`to_feature_collection`.

    Args:
        payload: The parsed JSON from :func:`gfw_geostore`.

    Returns:
        FeatureCollection: The admin-boundary features tagged `EPSG:4326`.

    Raises:
        ValueError: If the payload carries no `data.attributes.geojson` mapping.
    """
    geojson = (((payload or {}).get("data") or {}).get("attributes") or {}).get(
        "geojson"
    )
    if not isinstance(geojson, dict):
        raise ValueError(
            "GFW geostore payload is missing a data.attributes.geojson mapping; "
            f"got top-level keys {sorted(payload or {})}."
        )
    return to_feature_collection(geojson)


def to_feature_collection(geojson: dict) -> FeatureCollection:
    """Wrap a GeoJSON `FeatureCollection` mapping into a pyramids collection.

    Args:
        geojson: A GeoJSON mapping carrying a `features` list (WGS84), e.g. the
            `data.attributes.geojson` of a GFW geostore payload.

    Returns:
        FeatureCollection: The features tagged `EPSG:4326`. An empty/missing
            `features` list yields a schema-light empty collection.

    Raises:
        ValueError: If `geojson` carries no `features` key at all.
    """
    if "features" not in geojson:
        raise ValueError(
            "to_feature_collection expects a GeoJSON mapping with a 'features' "
            f"key; got keys {sorted(geojson)}."
        )
    features = geojson["features"]
    if not features:
        empty = gpd.GeoDataFrame(
            {"id": gpd.GeoSeries([], dtype="object")},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
        return FeatureCollection(empty)
    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    return FeatureCollection(gdf)


def resolve_admin(country: str, level: int = 0) -> str:
    """Turn a country ISO3 into the ThinkHazard ADM0 division code.

    Convenience wrapper over :meth:`earthlens.risk_indicators.catalog.Catalog.resolve_admin`
    that loads the bundled catalog itself, so callers can resolve a code without
    holding a :class:`~earthlens.risk_indicators.catalog.Catalog`.

    Args:
        country: An ISO3 country code (`"KEN"`).
        level: The admin level; only `0` (country) is supported.

    Returns:
        str: The ThinkHazard ADM0 division code (`"133"`).

    Raises:
        ValueError: If `level` is not `0`, or `country` is not a known ISO3.

    Examples:
        - Kenya resolves to its ADM0 code:
            ```python
            >>> from earthlens.risk_indicators._helpers import resolve_admin
            >>> resolve_admin("KEN", 0)
            '133'

            ```
    """
    from earthlens.risk_indicators.catalog import Catalog

    return Catalog().resolve_admin(country, level)


def empty_canonical(columns: list[str]) -> pd.DataFrame:
    """Return a zero-row frame carrying exactly `columns`.

    Args:
        columns: The column names the empty frame should declare.

    Returns:
        pd.DataFrame: An empty frame with the given columns and object dtype —
            the all-empty fallback used when a query matched nothing.
    """
    return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
