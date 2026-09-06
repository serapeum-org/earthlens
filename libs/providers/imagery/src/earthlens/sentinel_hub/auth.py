"""OAuth2 client-credentials authentication for the Sentinel Hub backend.

Hosts :class:`SentinelHubAuth`, a thin wrapper that builds and holds a
`sentinelhub.SHConfig`. Unlike the openEO backend's interactive OIDC device
flow, Sentinel Hub authenticates **non-interactively** with an OAuth2
*client-credentials* pair (a `client_id` / `client_secret` minted in the CDSE
Dashboard) — closest to GEE's service-account model. `sentinelhub-py` mints and
refreshes the bearer token internally from the config, so there is no explicit
login step here: :meth:`SentinelHubAuth.configure` only assembles the
`SHConfig` (base URL, token URL, credentials).

The credential resolution order in :meth:`SentinelHubAuth.configure` is:

1. **environment** — `SENTINELHUB_CLIENT_ID` / `SENTINELHUB_CLIENT_SECRET`
   (with `SH_CLIENT_ID` / `SH_CLIENT_SECRET` accepted as a `sentinelhub-py`-native
   fallback).
2. **kwargs** — an explicit `client_id` / `client_secret` on the credentials
   object (these win over the environment).
3. **saved profile** — a named `SHConfig` profile written earlier with
   `SHConfig.save(profile)` (used when neither of the above is set).

`endpoint=` switches the CDSE-free deployment (the default) and the commercial
one (`services.sentinel-hub.com`, a different token URL). Any failure is wrapped
as :class:`AuthenticationError` with a pointer at the CDSE Dashboard OAuth-client
page, never a raw `sentinelhub` exception.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.sentinel_hub._helpers import import_sentinelhub, resolve_endpoint

#: The CDSE Dashboard page where OAuth client-credentials are minted (Create →
#: name the client → Grant Type `Client Credentials`).
_DASHBOARD_URL = "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when the Sentinel Hub `SHConfig` cannot be assembled.

    Wraps the missing-credentials case with an actionable message pointing at
    the CDSE Dashboard. A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class SentinelHubCredentials(BaseModel):
    """Frozen value object holding the Sentinel Hub OAuth credentials.

    All fields are optional: an empty object falls back to the
    `SENTINELHUB_CLIENT_ID` / `SENTINELHUB_CLIENT_SECRET` environment (or the
    `SH_*` fallback), then to the named `profile`. Supplying `client_id`
    (+ `client_secret`) selects the explicit client-credentials pair (which wins
    over the environment).

    Attributes:
        client_id: OAuth client id, or `None` to read `SENTINELHUB_CLIENT_ID`.
        client_secret: OAuth client secret (kept as a `SecretStr`), or `None` to
            read `SENTINELHUB_CLIENT_SECRET`.
        profile: A saved `SHConfig` profile name to load credentials from when
            no id/secret is supplied, or `None` for the default profile.

    Examples:
        - An empty object carries no id (the env / profile path):
            ```python
            >>> from earthlens.sentinel_hub.auth import SentinelHubCredentials
            >>> SentinelHubCredentials().client_id is None
            True

            ```
        - The secret is carried opaquely:
            ```python
            >>> from earthlens.sentinel_hub.auth import SentinelHubCredentials
            >>> creds = SentinelHubCredentials(client_id="abc", client_secret="shh")
            >>> creds.client_secret.get_secret_value()
            'shh'

            ```
    """

    model_config = ConfigDict(frozen=True)

    client_id: str | None = None
    client_secret: SecretStr | None = None
    profile: str | None = None

    @classmethod
    def from_env(cls) -> SentinelHubCredentials:
        """Build credentials from the `SENTINELHUB_*` (or `SH_*`) environment.

        Reads the descriptive `SENTINELHUB_CLIENT_ID` / `SENTINELHUB_CLIENT_SECRET`
        / `SENTINELHUB_PROFILE` variables, falling back to the `sentinelhub-py`
        native `SH_CLIENT_ID` / `SH_CLIENT_SECRET` / `SH_PROFILE` for users coming
        from the SDK. Absent variables stay `None`.

        Returns:
            The credentials object built from the environment.
        """
        client_secret = os.environ.get("SENTINELHUB_CLIENT_SECRET") or os.environ.get(
            "SH_CLIENT_SECRET"
        )
        return cls(
            client_id=(
                os.environ.get("SENTINELHUB_CLIENT_ID")
                or os.environ.get("SH_CLIENT_ID")
            ),
            client_secret=SecretStr(client_secret) if client_secret else None,
            profile=(
                os.environ.get("SENTINELHUB_PROFILE") or os.environ.get("SH_PROFILE")
            ),
        )


class SentinelHubAuth(AbstractAuth[SentinelHubCredentials]):
    """Assemble and hold a Sentinel Hub `SHConfig` (non-interactive OAuth2).

    Conforms to the cross-backend :class:`earthlens.base.AbstractAuth` contract:
    :meth:`configure` builds the `SHConfig` lazily and idempotently;
    :meth:`is_authenticated` is the cheap predicate; and :meth:`config` returns
    the configured `SHConfig` (configuring on first use). The token itself is
    minted and refreshed by `sentinelhub-py` from the config on the first
    request — there is no interactive flow.

    Args:
        credentials: The OAuth credentials. Defaults to an empty
            :class:`SentinelHubCredentials` (env / profile path).
        endpoint: A named endpoint alias (`"cdse"`, `"commercial"`) or a full
            base URL. Defaults to CDSE-free.

    Examples:
        - Construct against CDSE-free (no config built until `configure()`):
            ```python
            >>> from earthlens.sentinel_hub.auth import SentinelHubAuth
            >>> auth = SentinelHubAuth()
            >>> auth.is_authenticated()
            False

            ```
    """

    def __init__(
        self,
        credentials: SentinelHubCredentials | None = None,
        endpoint: str | None = None,
    ) -> None:
        """Store the credentials + resolved endpoint; does not build the config yet.

        Args:
            credentials: OAuth credentials, or `None` for the env / profile path.
            endpoint: Endpoint alias or base URL; resolved eagerly so a bad alias
                fails at construction.
        """
        super().__init__(credentials or SentinelHubCredentials())
        self._base_url, self._token_url = resolve_endpoint(endpoint)
        self._config: Any = None

    @property
    def base_url(self) -> str:
        """The resolved Sentinel Hub base URL this auth targets."""
        return self._base_url

    def _resolve_pair(self) -> tuple[str | None, str | None]:
        """Return the `(client_id, client_secret)` to use (kwargs → env).

        Explicit credentials on the object win over the environment.

        Returns:
            The resolved id and secret (either may be `None`).
        """
        env = SentinelHubCredentials.from_env()
        client_id = self._creds.client_id or env.client_id
        secret_obj = self._creds.client_secret or env.client_secret
        secret = secret_obj.get_secret_value() if secret_obj else None
        return client_id, secret

    def configure(self) -> None:
        """Build the `SHConfig` (base/token urls + credentials); idempotent.

        Resolves credentials kwargs → env → profile. A second call after
        :meth:`is_authenticated` returns `True` is a no-op.

        Raises:
            ImportError: When the `sentinel-hub` extra is not installed.
            AuthenticationError: When no credentials are resolvable (no id/secret
                and no profile).
        """
        if self.is_authenticated():
            return
        sentinelhub = import_sentinelhub()
        client_id, client_secret = self._resolve_pair()
        if not (client_id and client_secret) and not self._creds.profile:
            raise AuthenticationError(
                "no Sentinel Hub credentials found: set SENTINELHUB_CLIENT_ID / "
                "SENTINELHUB_CLIENT_SECRET (mint an OAuth client_credentials pair "
                f"in the CDSE Dashboard at {_DASHBOARD_URL}, Grant Type "
                "'Client Credentials'), pass client_id= / client_secret=, or "
                "supply a saved SHConfig profile= name."
            )
        cfg = (
            sentinelhub.SHConfig(self._creds.profile)
            if self._creds.profile
            else sentinelhub.SHConfig()
        )
        cfg.sh_base_url = self._base_url
        cfg.sh_token_url = self._token_url
        if client_id:
            cfg.sh_client_id = client_id
        if client_secret:
            cfg.sh_client_secret = client_secret
        self._config = cfg

    def is_authenticated(self) -> bool:
        """`True` once :meth:`configure` has assembled an `SHConfig`.

        Cheap predicate — does not call the network. Returns `True` exactly when
        a config object has been built.

        Returns:
            Whether the `SHConfig` is ready to issue requests.
        """
        return self._config is not None

    def config(self) -> Any:
        """Return the assembled `SHConfig`, configuring on first use.

        Returns:
            The `sentinelhub.SHConfig` carrying the base/token urls + credentials.

        Raises:
            ImportError: When the `sentinel-hub` extra is not installed.
            AuthenticationError: When no credentials are resolvable.
        """
        self.configure()
        return self._config
