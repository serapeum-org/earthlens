"""Credentials and token resolution for the IUCN Red List backend.

Hosts :class:`IucnAuth`, an :class:`earthlens.base.AbstractAuth` subclass
that resolves the IUCN Red List **v4 API token** from, in priority order, an
explicit `token=` argument or the `IUCN_TOKEN` environment variable. The v4
API (`api.iucnredlist.org/api/v4`) requires a token on every request, sent
as an `Authorization: Bearer <token>` header (the retired v3 `?token=` query
param is gone). The token is **mandatory** — `configure()` raises when none
can be resolved.

The shape mirrors `WdpaAuth` / `OpenaqAuth`: a frozen
:class:`IucnCredentials` value object holding the optional token as a
:class:`pydantic.SecretStr`, and :class:`IucnAuth` resolving it in
:meth:`IucnAuth.configure`. The resolved token is read back via the
:attr:`IucnAuth.token` property and attached as the Bearer header by
`earthlens.iucn._rest`.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

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


class IucnAuth(AbstractAuth[IucnCredentials]):
    """Resolve and hold the IUCN Red List v4 API token (mandatory).

    Implements the :class:`earthlens.base.AbstractAuth` contract for a
    single-secret backend. Construction does not touch the environment;
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

    def __init__(self, credentials: IucnCredentials) -> None:
        """Store credentials; does not resolve the token yet.

        Args:
            credentials: The :class:`IucnCredentials` value object carrying
                the optional token.
        """
        super().__init__(credentials)
        self._configured = False
        self._token: str | None = None

    def configure(self) -> None:
        """Resolve the token so subsequent requests can authenticate.

        Idempotent — short-circuits when :meth:`is_authenticated` already
        returns `True`. On the first call, resolves the token in this order:
        the explicit `token` on the credentials, then the `IUCN_TOKEN`
        environment variable.

        Raises:
            AuthenticationError: When neither source supplies a token. The
                message names the `token=` argument, the `IUCN_TOKEN` env
                var, and the sign-up URL — it never blocks on an interactive
                prompt.
        """
        if self.is_authenticated():
            return
        token = (
            self._creds.token.get_secret_value()
            if self._creds.token is not None
            else os.environ.get("IUCN_TOKEN")
        )
        if not token:
            raise AuthenticationError(
                "no IUCN Red List token available: pass token= to IUCN(...) or "
                "set the IUCN_TOKEN environment variable. Sign up for a free "
                f"token at {_TOKEN_URL}."
            )
        self._token = token
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` once :meth:`configure` has resolved a token.

        Returns:
            bool: `True` after a successful :meth:`configure`, `False`
                before.
        """
        return self._configured

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
