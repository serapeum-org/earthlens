"""Per-endpoint CADS client routing for the ECMWF backend.

`earthlens.ecmwf` talks to more than one Copernicus Data Store instance through
the same `cdsapi` client: the Climate Data Store (CDS), the Atmosphere Data
Store (ADS), and the CEMS Early Warning Data Store (EWDS — GloFAS / EFAS / fire
danger). They share the request/retrieve protocol but differ by **URL**. Each
catalog `Dataset` carries an `endpoint:` slug (default `"cds"`); this module
maps that slug to a `(url, key)` pair and builds the matching
`cdsapi.Client`.

Credential model (verified live 2026-07-01): a single Personal Access Token
authenticates across CDS / ADS / EWDS, so a non-CDS endpoint falls back to the
CDS key (`CDSAPI_KEY`, else the `key:` line in `~/.cdsapirc`) when its own
`<ENDPOINT>_KEY` environment variable is unset. Only the URL has to differ. The
plain CDS path stays byte-identical to the historic bare `cdsapi.Client()` so
existing users are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import cdsapi

__all__ = ["DEFAULT_ENDPOINT", "ENDPOINTS", "open_client"]

DEFAULT_ENDPOINT: str = "cds"

# endpoint slug -> (default URL, URL-override env var, key-override env var).
# The `cds` row's env-var names are documentary only: `open_client("cds")`
# short-circuits to a bare `cdsapi.Client()`, which reads CDSAPI_URL / CDSAPI_KEY
# / ~/.cdsapirc itself. The URL/env names matter for the non-CDS endpoints.
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "cds": ("https://cds.climate.copernicus.eu/api", "CDSAPI_URL", "CDSAPI_KEY"),
    "ads": ("https://ads.atmosphere.copernicus.eu/api", "ADS_URL", "ADS_KEY"),
    "ewds": ("https://ewds.climate.copernicus.eu/api", "EWDS_URL", "EWDS_KEY"),
}


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
    return os.environ.get(key_env) or os.environ.get("CDSAPI_KEY") or _read_cdsapirc_key()


def open_client(endpoint: str = DEFAULT_ENDPOINT) -> cdsapi.Client:
    """Build a `cdsapi.Client` bound to the named CADS endpoint.

    Args:
        endpoint: One of the slugs in `ENDPOINTS` (`"cds"`, `"ads"`, `"ewds"`).
            Defaults to `"cds"`.

    Returns:
        cdsapi.Client: A client pointed at the endpoint's URL. For `"cds"` with
        no `CDSAPI_KEY` / `CDSAPI_URL` override set, this is the historic bare
        `cdsapi.Client()` (which reads `~/.cdsapirc`).

    Raises:
        ValueError: If `endpoint` is not a known slug.
        AuthenticationError: If a non-CDS endpoint has no resolvable token
            (neither `<ENDPOINT>_KEY` nor a shared CDS PAT).
    """
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
