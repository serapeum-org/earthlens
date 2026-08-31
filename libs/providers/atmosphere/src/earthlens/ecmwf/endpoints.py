"""Per-endpoint CADS client routing for the ECMWF backend.

`earthlens.ecmwf` talks to more than one data-store instance through the same
`cdsapi` client:

* the Climate Data Store (CDS),
* the Atmosphere Data Store (ADS),
* the CEMS Early Warning Data Store (EWDS — GloFAS / EFAS / fire danger),
* the ECMWF Data Store (ECDS — TIGGE and S2S ensemble forecasts), and
* the ECMWF Cross Data Store (XDS — fire fuel and burned area).

They share the request/retrieve protocol but differ by **URL**. Each catalog
`Dataset` carries an `endpoint:` slug (default `"cds"`); this module maps that
slug to a `(url, key)` pair and builds the matching `cdsapi.Client`.

Credential model (verified live 2026-07-01 for CDS / ADS / EWDS, 2026-08-16 for
ECDS / XDS): a single Personal Access Token authenticates across **all five**
stores — probing `profiles/v1/account` on each returns the same account — so a
non-CDS endpoint falls back to the CDS key (`CDSAPI_KEY`, else the `key:` line
in `~/.cdsapirc`) when its own `<ENDPOINT>_KEY` environment variable is unset.
Only the URL has to differ. The plain CDS path stays byte-identical to the
historic bare `cdsapi.Client()` so existing users are unaffected.

The two ECMWF-hosted stores are not Copernicus-branded, but they run the same
CADS software and the same catalogue API (`catalogue/v1/collections/<id>/
form.json` and `constraints.json`), so pre-flight constraint validation works
against them unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import cdsapi

if TYPE_CHECKING:
    from ecmwf.datastores import Client as ModernClient

__all__ = [
    "DEFAULT_ENDPOINT",
    "ENDPOINTS",
    "constraints_base_url",
    "endpoint_url",
    "open_client",
    "open_modern_client",
]

DEFAULT_ENDPOINT: str = "cds"

# endpoint slug -> (default URL, URL-override env var, key-override env var).
# The `cds` row's env-var names are documentary only: `open_client("cds")`
# short-circuits to a bare `cdsapi.Client()`, which reads CDSAPI_URL / CDSAPI_KEY
# / ~/.cdsapirc itself. The URL/env names matter for the non-CDS endpoints.
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "cds": ("https://cds.climate.copernicus.eu/api", "CDSAPI_URL", "CDSAPI_KEY"),
    "ads": ("https://ads.atmosphere.copernicus.eu/api", "ADS_URL", "ADS_KEY"),
    "ewds": ("https://ewds.climate.copernicus.eu/api", "EWDS_URL", "EWDS_KEY"),
    "ecds": ("https://ecds.ecmwf.int/api", "ECDS_URL", "ECDS_KEY"),
    "xds": ("https://xds.ecmwf.int/api", "XDS_URL", "XDS_KEY"),
}


def endpoint_url(endpoint: str) -> str:
    """Resolve a CADS endpoint's API root, honouring its URL-override env var.

    The same resolution `open_client` uses, so the constraints host and the
    licence/dataset-page links line up with the client's actual URL even when a
    user points `<ENDPOINT>_URL` at a staging host.

    Args:
        endpoint: One of the slugs in `ENDPOINTS` (`"cds"` / `"ads"` / `"ewds"` /
            `"ecds"` / `"xds"`).

    Returns:
        str: The resolved API root URL.

    Raises:
        ValueError: If `endpoint` is not a known slug.

    Examples:
        - Resolve the two ECMWF-hosted stores:
            ```python
            >>> from earthlens.ecmwf.endpoints import endpoint_url
            >>> endpoint_url("ecds")
            'https://ecds.ecmwf.int/api'
            >>> endpoint_url("xds")
            'https://xds.ecmwf.int/api'

            ```
        - Derive a store's profile page from its API root:
            ```python
            >>> from earthlens.ecmwf.endpoints import endpoint_url
            >>> endpoint_url("ewds").rsplit("/api", 1)[0] + "/profile"
            'https://ewds.climate.copernicus.eu/profile'

            ```
        - An unknown slug is rejected and the message lists the valid ones:
            ```python
            >>> from earthlens.ecmwf.endpoints import endpoint_url
            >>> endpoint_url("mars")
            Traceback (most recent call last):
                ...
            ValueError: unknown ECMWF endpoint 'mars'; expected one of ['ads', 'cds', 'ecds', 'ewds', 'xds']

            ```
    """
    if endpoint not in ENDPOINTS:
        raise ValueError(
            f"unknown ECMWF endpoint {endpoint!r}; expected one of {sorted(ENDPOINTS)}"
        )
    url_default, url_env, _key_env = ENDPOINTS[endpoint]
    return os.environ.get(url_env, url_default)


def constraints_base_url(endpoint: str) -> str | None:
    """The base URL to fetch `constraints.json` from for `endpoint`.

    Returns `None` for CDS so `fetch_constraints` uses its historic default
    (`CONSTRAINTS_URL_TEMPLATE`); for a non-CDS endpoint it returns the
    (env-aware) endpoint URL so EWDS/ADS datasets are validated against the
    host that publishes their constraints.

    Args:
        endpoint: One of the slugs in `ENDPOINTS`.

    Returns:
        str | None: The base URL, or `None` for CDS.

    Examples:
        - CDS keeps its historic `None`, so the default template is used:
            ```python
            >>> from earthlens.ecmwf.endpoints import constraints_base_url
            >>> constraints_base_url("cds") is None
            True

            ```
        - Every other store validates against its own host:
            ```python
            >>> from earthlens.ecmwf.endpoints import constraints_base_url
            >>> constraints_base_url("ecds")
            'https://ecds.ecmwf.int/api'

            ```
        - Build the URL a dataset's constraints are actually fetched from:
            ```python
            >>> from earthlens.ecmwf.endpoints import constraints_base_url
            >>> base = constraints_base_url("xds")
            >>> f"{base}/catalogue/v1/collections/derived-fire-fuel-biomass/constraints.json"
            'https://xds.ecmwf.int/api/catalogue/v1/collections/derived-fire-fuel-biomass/constraints.json'

            ```
    """
    return None if endpoint == "cds" else endpoint_url(endpoint)


def _read_cdsapirc_key() -> str | None:
    """Return the `key:` value from `~/.cdsapirc`, or `None` if unavailable.

    Reads the shared CDS Personal Access Token so a non-CDS endpoint can reuse
    it. Returns `None` when the dotfile is absent or has no `key:` line.

    Returns:
        str | None: The token string, or `None` when it cannot be read.
    """
    path = Path.home() / ".cdsapirc"
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() == "key":
            return value.strip() or None
    return None


def _resolve_key(key_env: str) -> str | None:
    """Resolve an endpoint's token: its own env var, else the shared CDS PAT.

    Args:
        key_env: The endpoint's key-override env var name (e.g. `"EWDS_KEY"`).

    Returns:
        str | None: The token to authenticate with, or `None` when neither the
        endpoint-specific env var nor the shared CDS token is available.
    """
    return (
        os.environ.get(key_env) or os.environ.get("CDSAPI_KEY") or _read_cdsapirc_key()
    )


def open_client(endpoint: str = DEFAULT_ENDPOINT) -> cdsapi.Client | ModernClient:
    """Build a `cdsapi.Client` bound to the named CADS endpoint.

    Args:
        endpoint: One of the slugs in `ENDPOINTS` (`"cds"`, `"ads"`, `"ewds"`,
            `"ecds"`, `"xds"`).
            Defaults to `"cds"`.

    Returns:
        cdsapi.Client | ecmwf.datastores.Client: A client pointed at the
        endpoint's URL. By default a `cdsapi.Client` — for `"cds"` with no
        `CDSAPI_KEY` / `CDSAPI_URL` override set, the historic bare
        `cdsapi.Client()` (which reads `~/.cdsapirc`). When
        `EARTHLENS_ECMWF_MODERN` is set, an `ecmwf.datastores.Client` instead
        (see the note below).

    Raises:
        ValueError: If `endpoint` is not a known slug.
        AuthenticationError: If a non-CDS endpoint has no resolvable token
            (neither `<ENDPOINT>_KEY` nor a shared CDS PAT).

    Note:
        Opt-in modern client: when `EARTHLENS_ECMWF_MODERN` is truthy
        (`1`/`true`/`yes`/`on`), this delegates to `open_modern_client`, which
        returns an `ecmwf.datastores.Client` (the `cdsapi` successor) instead —
        requires the `earthlens[ecmwf-modern]` extra. The flag is **process-wide**
        (an environment variable, not a per-call argument). Unset (the default),
        the behaviour below is byte-identical to before. When it *is* set, even
        the `"cds"` path changes: `open_modern_client("cds")` resolves the URL +
        token up front and raises `AuthenticationError` if none is found, rather
        than returning a bare `cdsapi.Client()` that does cdsapi's own lazy
        config discovery.
    """
    if _use_modern_client():
        return open_modern_client(endpoint)
    if endpoint not in ENDPOINTS:
        raise ValueError(
            f"unknown ECMWF endpoint {endpoint!r}; expected one of {sorted(ENDPOINTS)}"
        )
    url_default, url_env, key_env = ENDPOINTS[endpoint]

    # CDS stays byte-identical to the historic bare `cdsapi.Client()`: cdsapi
    # natively reads `CDSAPI_URL` / `CDSAPI_KEY` / `~/.cdsapirc`, so there is
    # nothing to thread. Only the non-CDS endpoints need an explicit url+key.
    if endpoint == "cds":
        return cdsapi.Client()

    url = os.environ.get(url_env, url_default)
    key = _resolve_key(key_env)
    if not key:
        # Imported lazily so this module has no import cycle with backend.py
        # (which imports open_client at load time).
        from earthlens.ecmwf.backend import AuthenticationError

        profile = url.rsplit("/api", 1)[0] + "/profile"
        raise AuthenticationError(
            f"ECMWF {endpoint.upper()} needs a Personal Access Token. Set the "
            f"{key_env} environment variable, or configure ~/.cdsapirc with your "
            f"CDS token (the same token authenticates against {endpoint.upper()}). "
            f"Generate one at {profile} and accept the dataset licence."
        )
    return cdsapi.Client(url=url, key=key)


def _use_modern_client() -> bool:
    """Whether the opt-in `ecmwf-datastores-client` path is enabled.

    Controlled by the `EARTHLENS_ECMWF_MODERN` environment variable (truthy =
    `1` / `true` / `yes` / `on`, case-insensitive). Off by default so the
    historic `cdsapi` path is untouched.

    Returns:
        bool: `True` when the modern client should be built.
    """
    return os.environ.get("EARTHLENS_ECMWF_MODERN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def open_modern_client(endpoint: str = DEFAULT_ENDPOINT) -> ModernClient:
    """Build an `ecmwf.datastores.Client` for the named CADS endpoint.

    The `ecmwf-datastores-client` package (installed via the
    `earthlens[ecmwf-modern]` extra) is the strategic successor to `cdsapi`. Its
    blocking `retrieve(collection_id, request, target=...)` matches the `cdsapi`
    call the backend makes, so it is a drop-in for the download path; the
    async `submit()` / `download()` API is available to callers who want it.

    Token resolution mirrors `open_client` (`<ENDPOINT>_KEY`, else `CDSAPI_KEY`,
    else `~/.cdsapirc`), so the same Personal Access Token authenticates. The
    URL, however, is resolved from `<ENDPOINT>_URL` (env) or the built-in
    default **only** — unlike `key`, it does not fall back to the `url:` line in
    `~/.cdsapirc` (a no-op for the shipped default CDS URL). This is opt-in —
    reached only when `EARTHLENS_ECMWF_MODERN` is truthy.

    Known limitation: the backend's friendly error classification (e.g. the
    "licence not accepted" → `PermissionError`-with-hint mapping) is tuned to
    `cdsapi`'s exception shapes, so a failure raised by `ecmwf.datastores.Client`
    may fall through to a generic error instead. This opt-in path is not yet
    exercised end-to-end; harden the classification before promoting it to the
    default.

    Args:
        endpoint: One of the slugs in `ENDPOINTS`. Defaults to `"cds"`.

    Returns:
        ecmwf.datastores.Client: A client pointed at the endpoint's URL.

    Raises:
        ValueError: If `endpoint` is not a known slug.
        ImportError: If the `ecmwf-modern` extra is not installed.
        AuthenticationError: If no token can be resolved for `endpoint`.
    """
    if endpoint not in ENDPOINTS:
        raise ValueError(
            f"unknown ECMWF endpoint {endpoint!r}; expected one of {sorted(ENDPOINTS)}"
        )
    try:
        from ecmwf.datastores import Client as ModernClient
    except ImportError as exc:
        raise ImportError(
            "The modern ECMWF client (ecmwf-datastores-client) is not "
            "installed. Install it with: pip install earthlens[ecmwf-modern] "
            "(and enable it with EARTHLENS_ECMWF_MODERN=1)."
        ) from exc

    url_default, url_env, key_env = ENDPOINTS[endpoint]
    url = os.environ.get(url_env, url_default)
    key = _resolve_key(key_env)
    if not key:
        from earthlens.ecmwf.backend import AuthenticationError

        profile = url.rsplit("/api", 1)[0] + "/profile"
        raise AuthenticationError(
            f"ECMWF {endpoint.upper()} needs a Personal Access Token. Set the "
            f"{key_env} (or CDSAPI_KEY) environment variable, or configure "
            f"~/.cdsapirc with your CDS token. Generate one at {profile} and "
            f"accept the dataset licence."
        )
    return ModernClient(url=url, key=key)
