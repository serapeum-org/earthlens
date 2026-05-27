"""Stateless helpers for the Sentinel Hub backend.

Hosts the small, pure functions and constant tables the backend, auth, and
dispatch modules share: the request-plane vocabulary (`VALID_APIS`), the
endpoint registry (CDSE-free vs commercial), the Process/Async pixel ceilings
(`SH_MAX_DIMENSION` / `ASYNC_MAX_DIMENSION`), the lazy `sentinelhub` SDK importer
(which turns a missing `[sentinel-hub]` extra into a friendly `ImportError`), the
CDSE data-collection binding (`cdse_collection`), and the pandas-frequency →
Statistical `aggregation_interval` (ISO-8601) mapping used by `aggregate=`.

None of these touch the network, so they are trivially unit testable and safe to
import without the `sentinelhub` SDK installed (the SDK is imported only inside
`import_sentinelhub` / `cdse_collection`, never at module load).
"""

from __future__ import annotations

from typing import Any

#: The request planes the backend dispatches on (the `api=` kwarg). `None`
#: selects a plane automatically from the request size, whether a `geometry=`
#: was supplied, and whether an S3 `batch_output` is configured (see
#: `earthlens.sentinel_hub.backend`). `"tiling"` is the local-mosaic strategy
#: (split into ≤2500 px Process tiles + merge) — the medium/large-AOI path that
#: needs no S3 bucket; `"async"` / `"batch"` deliver server-side to S3 (the SDK's
#: `AsyncProcessRequest` / `BatchProcessClient` both require an S3 delivery).
VALID_APIS: tuple[str, ...] = (
    "process",
    "async",
    "tiling",
    "batch",
    "statistical",
    "batch-statistical",
)

#: The raster planes (emit GeoTIFFs / S3 rasters); the tabular planes emit a table.
RASTER_APIS: frozenset[str] = frozenset({"process", "async", "tiling", "batch"})
TABULAR_APIS: frozenset[str] = frozenset({"statistical", "batch-statistical"})

#: The planes that deliver server-side to an S3 bucket (need `batch_output`).
S3_APIS: frozenset[str] = frozenset({"async", "batch", "batch-statistical"})

#: Named Sentinel Hub deployments. Keys are the `endpoint=` aliases; values are
#: `(sh_base_url, sh_token_url)` verified against the live services (A1). The
#: default is CDSE-free (a CDSE account, no commercial subscription).
SH_ENDPOINTS: dict[str, tuple[str, str]] = {
    "cdse": (
        "https://sh.dataspace.copernicus.eu",
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
    ),
    "commercial": (
        "https://services.sentinel-hub.com",
        "https://services.sentinel-hub.com/auth/realms/main/protocol/openid-connect/token",
    ),
}

#: Default endpoint alias when `endpoint=` is omitted (CDSE-free).
DEFAULT_ENDPOINT: str = "cdse"

#: The Process API caps a single synchronous request at 2500 px per side
#: (verified, A1 — a 5000 px request errors). Bounds one Process tile.
SH_MAX_DIMENSION: int = 2500

#: The Async Processing API raises the ceiling to 10000 px per side (A1).
ASYNC_MAX_DIMENSION: int = 10000

# pandas offset alias (any leading count stripped) → ISO-8601 duration used as
# the Statistical API `aggregation_interval`. The server returns one stats row
# per interval, so a request's `AggregationConfig.freq` ("1D", "7D", "1MS",
# "YS", …) maps here before it reaches `SentinelHubStatistical.aggregation`.
_FREQ_TO_INTERVAL: dict[str, str] = {
    "D": "P1D",
    "W": "P7D",
    "7D": "P7D",
    "10D": "P10D",
    "MS": "P1M",
    "M": "P1M",
    "ME": "P1M",
    "QS": "P3M",
    "Q": "P3M",
    "QE": "P3M",
    "YS": "P1Y",
    "Y": "P1Y",
    "YE": "P1Y",
    "A": "P1Y",
    "AS": "P1Y",
}


def resolve_endpoint(endpoint: str | None) -> tuple[str, str]:
    """Resolve an `endpoint=` value to a `(base_url, token_url)` pair.

    Accepts a named alias (`"cdse"`, `"commercial"`), a full `http(s)://` base
    URL (paired with the matching token URL by host), or `None` (the CDSE-free
    default).

    Args:
        endpoint: A named alias, a full base URL, or `None`.

    Returns:
        The `(sh_base_url, sh_token_url)` pair to write onto an `SHConfig`.

    Raises:
        ValueError: When `endpoint` is an unknown non-URL string.

    Examples:
        - The default alias resolves to CDSE-free:
            ```python
            >>> from earthlens.sentinel_hub._helpers import resolve_endpoint
            >>> base, _ = resolve_endpoint(None)
            >>> base
            'https://sh.dataspace.copernicus.eu'

            ```
        - A full URL on a CDSE host keeps the CDSE Keycloak token URL:
            ```python
            >>> from earthlens.sentinel_hub._helpers import resolve_endpoint
            >>> _, token = resolve_endpoint("https://sh.dataspace.copernicus.eu")
            >>> "dataspace" in token
            True

            ```
    """
    if endpoint is None:
        return SH_ENDPOINTS[DEFAULT_ENDPOINT]
    if endpoint in SH_ENDPOINTS:
        return SH_ENDPOINTS[endpoint]
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        token = (
            SH_ENDPOINTS["cdse"][1]
            if "dataspace" in endpoint
            else SH_ENDPOINTS["commercial"][1]
        )
        return endpoint, token
    raise ValueError(
        f"unknown Sentinel Hub endpoint {endpoint!r}: pass one of "
        f"{sorted(SH_ENDPOINTS)} or a full http(s):// base URL."
    )


def interval_for(freq: str) -> str:
    """Map a pandas frequency alias to a Statistical `aggregation_interval`.

    Strips any leading repeat count (`"1MS"` → `"MS"`) before the lookup, except
    for the count-prefixed day windows (`"7D"`, `"10D"`) handled directly.

    Args:
        freq: A pandas offset alias, optionally count-prefixed (`"1D"`, `"7D"`,
            `"1MS"`, `"YS"`, …).

    Returns:
        The ISO-8601 duration string the Statistical API expects (`"P1D"`,
        `"P7D"`, `"P1M"`, `"P1Y"`, …).

    Raises:
        NotImplementedError: If `freq` has no ISO-8601 interval equivalent.

    Examples:
        - Month-start maps to a one-month interval:
            ```python
            >>> from earthlens.sentinel_hub._helpers import interval_for
            >>> interval_for("1MS")
            'P1M'

            ```
        - A daily window maps to `P1D`:
            ```python
            >>> from earthlens.sentinel_hub._helpers import interval_for
            >>> interval_for("D")
            'P1D'

            ```
    """
    if freq in _FREQ_TO_INTERVAL:
        return _FREQ_TO_INTERVAL[freq]
    key = freq.lstrip("0123456789")
    if key in _FREQ_TO_INTERVAL:
        return _FREQ_TO_INTERVAL[key]
    raise NotImplementedError(
        f"the Statistical API has no aggregation_interval for freq={freq!r}; "
        f"supported pandas aliases map to {sorted(set(_FREQ_TO_INTERVAL.values()))}."
    )


def import_sentinelhub() -> Any:
    """Import and return the `sentinelhub` SDK, or raise a friendly `ImportError`.

    The SDK is an optional dependency (`pip install earthlens[sentinel-hub]`);
    this helper centralises the lazy import so every call site surfaces the same
    actionable message when the extra is missing.

    Returns:
        The imported `sentinelhub` module.

    Raises:
        ImportError: When the `sentinel-hub` extra is not installed.
    """
    try:
        import sentinelhub
    except ImportError as exc:
        raise ImportError(
            "the Sentinel Hub backend requires the 'sentinelhub' client. Install "
            "it with `pip install earthlens[sentinel-hub]`."
        ) from exc
    return sentinelhub


def cdse_collection(name: str, base_url: str) -> Any:
    """Bind a `DataCollection` name to the CDSE deployment (or return it as-is).

    The stock `sentinelhub.DataCollection` enum members point at the commercial
    deployment, so on CDSE each must be rebound to the CDSE service URL via
    `DataCollection.<NAME>.define_from(..., service_url=base_url)` (verified, A1).
    On a commercial `base_url` the stock member is returned unchanged.

    Args:
        name: A `DataCollection` member name (e.g. `"SENTINEL2_L2A"`).
        base_url: The configured `sh_base_url` (CDSE vs commercial).

    Returns:
        The (possibly rebound) `DataCollection` to pass to `input_data`.

    Raises:
        ImportError: When the `sentinel-hub` extra is not installed.
        ValueError: When `name` is not a known `DataCollection` member.
    """
    sentinelhub = import_sentinelhub()
    try:
        base = getattr(sentinelhub.DataCollection, name)
    except AttributeError as exc:
        raise ValueError(
            f"{name!r} is not a known sentinelhub DataCollection."
        ) from exc
    if "dataspace" in base_url:
        return base.define_from(f"{name.lower()}_cdse", service_url=base_url)
    return base
