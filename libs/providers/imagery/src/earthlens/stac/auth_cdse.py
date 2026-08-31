"""CDSE S3 credential resolution for the STAC backend's `cdse-s3` signer.

Copernicus Data Space (CDSE) STAC *search* is anonymous, but the asset bytes
live on an S3-compatible store at `eodata.dataspace.copernicus.eu` that needs
S3 access-key/secret credentials. Two ways to obtain them:

* **(a) Dashboard keys** — long-lived S3 keys generated in the CDSE dashboard
  (the robust default). Supplied via the `CDSE_S3_ACCESS_KEY` /
  `CDSE_S3_SECRET_KEY` env vars or explicit kwargs.
* **(b) Keycloak token → temporary S3 creds** — exchanged from an OAuth
  password grant when only `CDSE_USERNAME` / `CDSE_PASSWORD` are available.

`s3_credentials` resolves (a) first (kwarg → env). Path (b) is a planned
fallback (see the STAC completion plan `G4`); until its token /
exchange endpoints are pinned, supplying only username/password raises a
clear `AuthenticationError` naming the dashboard URL.
"""

from __future__ import annotations

import os

from earthlens.base import AuthenticationError

#: Where a user generates long-lived S3 keys for the CDSE eodata store.
CDSE_DASHBOARD_URL = "https://eodata-s3keysmanager.dataspace.copernicus.eu"


def s3_credentials(
    access_key: str | None = None,
    secret_key: str | None = None,
    **_ignored: object,
) -> tuple[str, str]:
    """Resolve CDSE S3 `(access_key, secret_key)` for the `cdse-s3` signer.

    Resolution order: explicit kwargs, then the `CDSE_S3_ACCESS_KEY` /
    `CDSE_S3_SECRET_KEY` env vars. Extra keyword arguments are ignored so the
    `build_signer(**creds)` call site can forward a superset of kwargs.

    Args:
        access_key: CDSE S3 access key id, or `None` to read the env var.
        secret_key: CDSE S3 secret key, or `None` to read the env var.
        **_ignored: Ignored extra kwargs (forwarded by `build_signer`).

    Returns:
        The resolved `(access_key, secret_key)` pair.

    Raises:
        AuthenticationError: When neither kwargs nor env vars supply both
            halves of the credential pair. The message names the dashboard
            URL where keys are generated and the env vars to set.

    Examples:
        - Explicit keys are returned as a pair (kwargs win over the env):
            ```python
            >>> from earthlens.stac import auth_cdse
            >>> auth_cdse.s3_credentials(access_key="ak", secret_key="sk")
            ('ak', 'sk')

            ```
        - Extra kwargs that `build_signer` forwards are ignored:
            ```python
            >>> from earthlens.stac import auth_cdse
            >>> auth_cdse.s3_credentials(access_key="ak", secret_key="sk", region="eu")
            ('ak', 'sk')

            ```
    """
    resolved_access = access_key or os.environ.get("CDSE_S3_ACCESS_KEY")
    resolved_secret = secret_key or os.environ.get("CDSE_S3_SECRET_KEY")
    if resolved_access and resolved_secret:
        return resolved_access, resolved_secret

    has_login = os.environ.get("CDSE_USERNAME") and os.environ.get("CDSE_PASSWORD")
    extra = (
        " A CDSE_USERNAME/CDSE_PASSWORD token exchange is not yet wired; "
        "generate dashboard S3 keys for now."
        if has_login
        else ""
    )
    raise AuthenticationError(
        "CDSE S3 credentials are required to read eodata assets but none were "
        "found. Generate S3 keys at "
        f"{CDSE_DASHBOARD_URL} and set CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY "
        "(or pass access_key= / secret_key=)." + extra
    )
