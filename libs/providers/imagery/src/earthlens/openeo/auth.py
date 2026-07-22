"""OIDC authentication for the openEO backend (CDSE / openEO Platform).

Hosts :class:`OpeneoAuth`, a thin wrapper over the `openeo` client's OpenID
Connect entry points. Unlike the static-key backends (a `~/.cdsapirc`, an AWS
key, a service-account JSON), openEO authenticates against an OIDC provider —
on CDSE that is the same account as the STAC `cdse` endpoint, but a different
auth plane (OIDC tokens here vs S3 keys there).

The resolution order in :meth:`OpeneoAuth.configure` mirrors the openEO client's
own ergonomics:

1. **client-credentials** — when a `client_id` (+ `client_secret`) is supplied
   (kwargs or `OPENEO_CLIENT_ID` / `OPENEO_CLIENT_SECRET` env vars), use the
   non-interactive service-account flow. This is the CI / headless path.
2. **refresh-token** — when a `refresh_token` is supplied, exchange it
   non-interactively.
3. **interactive device flow** — otherwise call `authenticate_oidc()`, which
   reuses a refresh token cached on disk (`~/.openeo/`) from an earlier login,
   or prints a URL + code for a human to complete the device flow.

Any failure is wrapped as :class:`AuthenticationError` with a pointer at the
CDSE account URL, never a raw `openeo` exception.
"""

from __future__ import annotations

import os
from typing import Any

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.openeo._helpers import (
    DEFAULT_ENDPOINT,
    import_openeo,
    resolve_endpoint,
)
from pydantic import BaseModel, ConfigDict, SecretStr

_CDSE_ACCOUNT_URL = "https://dataspace.copernicus.eu"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when the openEO OIDC connection cannot be established.

    Wraps the underlying `openeo` / OIDC errors with an actionable message —
    most commonly no cached refresh token in a headless run, missing client
    credentials, or a rejected token. A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class OpeneoCredentials(BaseModel):
    """Frozen value object holding the openEO OIDC credentials.

    All fields are optional: an empty credentials object selects the interactive
    device flow (which reuses a cached refresh token when present). Supplying
    `client_id` (+ `client_secret`) selects the non-interactive
    client-credentials flow; supplying `refresh_token` selects the refresh-token
    flow.

    Attributes:
        client_id: OIDC client id for the client-credentials / refresh-token
            flows, or `None` for the interactive flow.
        client_secret: OIDC client secret (kept as a `SecretStr`), or `None`.
        refresh_token: A previously minted OIDC refresh token, or `None`.
        provider_id: Optional OIDC provider id to disambiguate when the backend
            advertises several; `None` lets the client pick the default.

    Examples:
        - An empty object selects the interactive / cached-token flow:
            ```python
            >>> from earthlens.openeo.auth import OpeneoCredentials
            >>> OpeneoCredentials().client_id is None
            True

            ```
        - Client credentials are carried as a secret:
            ```python
            >>> from earthlens.openeo.auth import OpeneoCredentials
            >>> creds = OpeneoCredentials(client_id="svc", client_secret="shh")
            >>> creds.client_secret.get_secret_value()
            'shh'

            ```
    """

    model_config = ConfigDict(frozen=True)

    client_id: str | None = None
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None
    provider_id: str | None = None

    @classmethod
    def from_env(cls) -> OpeneoCredentials:
        """Build credentials from the `OPENEO_*` environment variables.

        Reads `OPENEO_CLIENT_ID`, `OPENEO_CLIENT_SECRET`,
        `OPENEO_REFRESH_TOKEN`, and `OPENEO_PROVIDER_ID`. Absent variables stay
        `None`, so a fully unset environment yields the interactive-flow
        credentials object.

        Returns:
            The credentials object built from the environment.
        """
        client_secret = os.environ.get("OPENEO_CLIENT_SECRET")
        refresh_token = os.environ.get("OPENEO_REFRESH_TOKEN")
        return cls(
            client_id=os.environ.get("OPENEO_CLIENT_ID"),
            client_secret=SecretStr(client_secret)
            if client_secret is not None
            else None,
            refresh_token=SecretStr(refresh_token)
            if refresh_token is not None
            else None,
            provider_id=os.environ.get("OPENEO_PROVIDER_ID"),
        )


class OpeneoAuth(AbstractAuth[OpeneoCredentials]):
    """Authenticate and hold an openEO :class:`Connection`.

    Conforms to the cross-backend :class:`earthlens.base.AbstractAuth` contract:
    :meth:`configure` opens the connection and runs the OIDC flow lazily and
    idempotently; :meth:`is_authenticated` is the cheap predicate; and
    :meth:`connection` returns the configured connection (configuring on first
    use). The connection is built once and reused for every download in the
    session.

    Args:
        credentials: The OIDC credentials. Defaults to an empty
            :class:`OpeneoCredentials` (interactive / cached-token flow).
        endpoint: A named endpoint alias (`"cdse"`, `"cdse-federation"`,
            `"openeo-platform"`) or a full URL. Defaults to CDSE core.

    Examples:
        - Construct against the default CDSE endpoint (no network until
          `configure()`):
            ```python
            >>> from earthlens.openeo.auth import OpeneoAuth
            >>> auth = OpeneoAuth()
            >>> auth.is_authenticated()
            False

            ```
    """

    def __init__(
        self,
        credentials: OpeneoCredentials | None = None,
        endpoint: str | None = DEFAULT_ENDPOINT,
    ) -> None:
        """Store the credentials + resolved endpoint; does not connect yet.

        Args:
            credentials: OIDC credentials, or `None` for the interactive flow.
            endpoint: Endpoint alias or URL; resolved eagerly so a bad alias
                fails at construction.
        """
        super().__init__(credentials or OpeneoCredentials())
        self._endpoint = resolve_endpoint(endpoint)
        self._conn: Any = None

    @property
    def endpoint(self) -> str:
        """The resolved openEO API root URL this auth targets."""
        return self._endpoint

    def configure(self) -> None:
        """Open the connection and run the OIDC flow; idempotent.

        Selects the flow by which credentials are present: client-credentials
        (a `client_id`), then refresh-token, then the interactive device flow.
        A second call after :meth:`is_authenticated` returns `True` is a no-op.

        Raises:
            ImportError: When the `openeo` extra is not installed.
            AuthenticationError: When the OIDC flow fails (no cached token in a
                headless run, bad credentials, rejected token).
        """
        if self.is_authenticated():
            return
        openeo = import_openeo()
        try:
            conn = openeo.connect(self._endpoint)
            if self._creds.client_id and self._creds.client_secret:
                conn.authenticate_oidc_client_credentials(
                    client_id=self._creds.client_id,
                    client_secret=self._creds.client_secret.get_secret_value(),
                    provider_id=self._creds.provider_id,
                )
            elif self._creds.refresh_token:
                conn.authenticate_oidc_refresh_token(
                    client_id=self._creds.client_id,
                    refresh_token=self._creds.refresh_token.get_secret_value(),
                    client_secret=(
                        self._creds.client_secret.get_secret_value()
                        if self._creds.client_secret
                        else None
                    ),
                    provider_id=self._creds.provider_id,
                )
            else:
                conn.authenticate_oidc(provider_id=self._creds.provider_id)
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"openEO OIDC authentication failed against {self._endpoint!r}. "
                f"Sign in once interactively with a CDSE account "
                f"({_CDSE_ACCOUNT_URL}) to cache a refresh token, or set "
                "OPENEO_CLIENT_ID / OPENEO_CLIENT_SECRET for a headless run."
            ) from exc
        self._conn = conn

    def is_authenticated(self) -> bool:
        """`True` once :meth:`configure` has opened an authenticated connection.

        Cheap predicate — does not call the network. Returns `True` exactly when
        a connection object has been stored.

        Returns:
            Whether the connection is ready to build process graphs.
        """
        return self._conn is not None

    def connection(self) -> Any:
        """Return the authenticated openEO connection, configuring on first use.

        Returns:
            The `openeo.Connection` to build `load_collection` graphs from.

        Raises:
            ImportError: When the `openeo` extra is not installed.
            AuthenticationError: When the OIDC flow fails.
        """
        self.configure()
        return self._conn
