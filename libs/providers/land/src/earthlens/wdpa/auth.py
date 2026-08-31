"""Credentials and token resolution for the WDPA / Protected Planet backend.

Hosts :class:`WdpaAuth`, an :class:`earthlens.base.SingleSecretAuth` subclass
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
* :class:`WdpaAuth` binds those credentials and lets the shared
  :meth:`earthlens.base.SingleSecretAuth.configure` resolve the token —
  explicit token first, then the `WDPA_TOKEN` env var, then a clear
  :class:`AuthenticationError` naming the token-request URL (never an
  interactive prompt).

The resolved token is read back via the :attr:`WdpaAuth.token` property and
attached as the `token=` query parameter by `earthlens.wdpa._rest`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.base.auth import SingleSecretAuth

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


class WdpaAuth(SingleSecretAuth[WdpaCredentials]):
    """Resolve and hold the Protected Planet API token (mandatory).

    Implements the :class:`earthlens.base.SingleSecretAuth` contract for a
    single-secret backend: it declares its env var and provider name and
    supplies `_explicit_credential` / `_connect`, while the inherited
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

    ENV_VARS = ("WDPA_TOKEN",)
    PROVIDER = "Protected Planet"
    CREDENTIAL_ARG = "token"
    CREDENTIAL_HINT = f"Request a token at {_TOKEN_URL}."
    AUTH_ERROR = AuthenticationError

    #: The resolved token, set by `_connect`; `None` until `configure` runs.
    _token: str | None = None

    def _explicit_credential(self) -> str | None:
        """Return the explicit `token` off the credentials, if any."""
        token = self._creds.token
        return token.get_secret_value() if token is not None else None

    def _connect(self, credential: str) -> None:
        """Store the resolved token for the `token` property to read back."""
        self._token = credential

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
