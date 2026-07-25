"""Credentials and token resolution for the WDPA / Protected Planet backend.

Hosts :class:`WdpaAuth`, an :class:`earthlens.base.AbstractAuth` subclass
that resolves the Protected Planet **personal API token** from, in priority
order, an explicit `token=` argument or the `WDPA_TOKEN` environment
variable. The Protected Planet v4 API requires a token on every request,
passed as a `?token=` **query parameter** (not a Bearer header); there is
no username/password, so this is a single-secret auth shape mirroring
`OpenaqAuth`, but the token is **mandatory** — `configure()` raises when
none can be resolved.

The shape:

* :class:`WdpaCredentials` is a frozen pydantic value object carrying the
  optional token as a :class:`pydantic.SecretStr`.
* :class:`WdpaAuth` binds those credentials and resolves the token in
  :meth:`WdpaAuth.configure` — explicit token first, then the `WDPA_TOKEN`
  env var, then a clear :class:`AuthenticationError` naming the
  token-request URL (never an interactive prompt).

The resolved token is read back via the :attr:`WdpaAuth.token` property and
attached as the `token=` query parameter by `earthlens.wdpa._rest`.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user requests a personal Protected Planet API token.
_TOKEN_URL = "https://api.protectedplanet.net/request"  # nosec B105 - not a secret (public URL / identifier)


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable Protected Planet token can be resolved.

    Carries a message that names a fix: pass `token=` to `WDPA(...)`, set
    the `WDPA_TOKEN` environment variable, or request a token at
    `api.protectedplanet.net/request`. A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class WdpaCredentials(BaseModel):
    """Frozen value object holding the Protected Planet API token.

    The token is optional at construction time: `None` means "resolve from
    the `WDPA_TOKEN` environment variable at :meth:`WdpaAuth.configure`
    time". The real "is there a usable token?" gate is
    :meth:`WdpaAuth.configure`, not this model.

    Attributes:
        token: The Protected Planet API token, stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers resolution to the
            environment variable.

    Examples:
        - Build from an explicit token; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.wdpa import WdpaCredentials
            >>> creds = WdpaCredentials(token="topsecret")
            >>> creds.token.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    token: SecretStr | None = None


class WdpaAuth(AbstractAuth[WdpaCredentials]):
    """Resolve and hold the Protected Planet API token (mandatory).

    Implements the :class:`earthlens.base.AbstractAuth` contract for a
    single-secret backend. Construction does not touch the environment;
    :meth:`configure` performs the resolution and is idempotent. After a
    successful `configure()`, the token is available via the
    :attr:`token` property for `_rest` to attach as the `token=` query
    parameter.

    Attributes:
        _creds: The :class:`WdpaCredentials` passed at construction.

    Examples:
        - Resolve an explicit token:
            ```python
            >>> from earthlens.wdpa import WdpaAuth, WdpaCredentials
            >>> auth = WdpaAuth(WdpaCredentials(token="k"))
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.token
            'k'

            ```
    """

    def __init__(self, credentials: WdpaCredentials) -> None:
        """Store credentials; does not resolve the token yet.

        Args:
            credentials: The :class:`WdpaCredentials` value object
                carrying the optional token.
        """
        super().__init__(credentials)
        self._configured = False
        self._token: str | None = None

    def configure(self) -> None:
        """Resolve the token so subsequent requests can authenticate.

        Idempotent — short-circuits when :meth:`is_authenticated` already
        returns `True`. On the first call, resolves the token in this
        order: the explicit `token` on the credentials, then the
        `WDPA_TOKEN` environment variable.

        Raises:
            AuthenticationError: When neither source supplies a token. The
                message names the `token=` argument, the `WDPA_TOKEN` env
                var, and the token-request URL — it never blocks on an
                interactive prompt.
        """
        if self.is_authenticated():
            return
        token = (
            self._creds.token.get_secret_value()
            if self._creds.token is not None
            else os.environ.get("WDPA_TOKEN")
        )
        if not token:
            raise AuthenticationError(
                "no Protected Planet token available: pass token= to WDPA(...) "
                "or set the WDPA_TOKEN environment variable. Request a token at "
                f"{_TOKEN_URL}."
            )
        self._token = token
        self.mark_configured()

    @property
    def token(self) -> str:
        """The resolved token; valid only after :meth:`configure`.

        Returns:
            str: The Protected Planet API token string.

        Raises:
            AuthenticationError: When read before :meth:`configure` has
                resolved a token.
        """
        if self._token is None:
            raise AuthenticationError(
                "WdpaAuth.configure() has not run yet; no token resolved."
            )
        return self._token
