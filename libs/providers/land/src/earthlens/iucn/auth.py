"""Credentials and token resolution for the IUCN Red List backend.

Hosts :class:`IucnAuth`, an :class:`earthlens.base.SingleSecretAuth` subclass
that resolves the IUCN Red List **v4 API token** from, in priority order, an
explicit `token=` argument or the `IUCN_TOKEN` environment variable. The v4
API (`api.iucnredlist.org/api/v4`) requires a token on every request, sent
as an `Authorization: Bearer <token>` header (the retired v3 `?token=` query
param is gone). The token is **mandatory** — `configure()` raises when none
can be resolved.

The shape mirrors `WdpaAuth` / `OpenaqAuth`: a frozen
:class:`IucnCredentials` value object holding the optional token as a
:class:`pydantic.SecretStr`, and :class:`IucnAuth` resolving it via the shared
:meth:`earthlens.base.SingleSecretAuth.configure`. The resolved token is read
back via the
:attr:`IucnAuth.token` property and attached as the Bearer header by
`earthlens.iucn._rest`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.base.auth import SingleSecretAuth

#: Where a user signs up for a free IUCN Red List v4 API token.
_TOKEN_URL = "https://api.iucnredlist.org/users/sign_up"  # nosec B105 - not a secret (public URL / identifier)


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable IUCN Red List token can be resolved.

    Carries a message that names a fix: pass `token=` to `IUCN(...)`, set the
    `IUCN_TOKEN` environment variable, or sign up at
    `api.iucnredlist.org/users/sign_up`. A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class IucnCredentials(BaseModel):
    """Frozen value object holding the IUCN Red List v4 API token.

    The token is optional at construction time: `None` means "resolve from
    the `IUCN_TOKEN` environment variable at :meth:`IucnAuth.configure`
    time". The real "is there a usable token?" gate is
    :meth:`IucnAuth.configure`, not this model.

    Attributes:
        token: The IUCN Red List v4 token, stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers resolution to the
            environment variable.

    Examples:
        - Build from an explicit token; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.iucn import IucnCredentials
            >>> creds = IucnCredentials(token="topsecret")
            >>> creds.token.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    token: SecretStr | None = None


class IucnAuth(SingleSecretAuth[IucnCredentials]):
    """Resolve and hold the IUCN Red List v4 API token (mandatory).

    Implements the :class:`earthlens.base.SingleSecretAuth` contract for a
    single-secret backend: it declares its env var and provider name and
    supplies `_explicit_credential` / `_connect`, while the inherited
    :meth:`configure` performs the resolution and is idempotent. After a
    successful `configure()`, the token is available via the :attr:`token`
    property for `_rest` to attach as the `Authorization: Bearer` header.

    Attributes:
        _creds: The :class:`IucnCredentials` passed at construction.

    Examples:
        - Resolve an explicit token:
            ```python
            >>> from earthlens.iucn import IucnAuth, IucnCredentials
            >>> auth = IucnAuth(IucnCredentials(token="k"))
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.token
            'k'

            ```
    """

    ENV_VARS = ("IUCN_TOKEN",)
    PROVIDER = "IUCN Red List"
    CREDENTIAL_ARG = "token"
    CREDENTIAL_HINT = f"Sign up for a free token at {_TOKEN_URL}."
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
            str: The IUCN Red List v4 token string.

        Raises:
            AuthenticationError: When read before :meth:`configure` has
                resolved a token.
        """
        if self._token is None:
            raise AuthenticationError(
                "IucnAuth.configure() has not run yet; no token resolved."
            )
        return self._token
