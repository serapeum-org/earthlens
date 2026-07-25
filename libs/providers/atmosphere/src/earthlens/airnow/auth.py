"""Credentials and API-key resolution for the AirNow backend.

Hosts `AirnowAuth`, an `earthlens.base.AbstractAuth` subclass that
resolves a single AirNow API key from, in priority order, an explicit
`api_key=` argument or the `AIRNOW_API_KEY` environment variable. The
AirNow `/aq/data/` service requires a free key (registered at
`docs.airnowapi.org`) on every request as an `API_KEY` query argument;
there is no username/password and no saved config-file dance, so this
is the same single-secret shape as `earthlens.openaq.OpenaqAuth`.

The shape:

* `AirnowCredentials` is a frozen pydantic value object carrying the
  optional API key as a `pydantic.SecretStr`.
* `AirnowAuth` binds those credentials and resolves the key in
  `AirnowAuth.configure` — explicit key first, then the
  `AIRNOW_API_KEY` env var, then a clear `AuthenticationError` naming
  the free-registration URL (never an interactive prompt).
* `configure()` is idempotent — a second call after
  `AirnowAuth.is_authenticated` returns `True` short-circuits.

The resolved key is read back via the `AirnowAuth.api_key` property
and attached as the `API_KEY` query argument by
`earthlens.airnow.client.AirnowClient`.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user registers for a free AirNow API key.
_REGISTER_URL = "https://docs.airnowapi.org/account/request/"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable AirNow API key can be resolved.

    Carries a message that names a fix: pass `api_key=` to `AirNow(...)`,
    set the `AIRNOW_API_KEY` environment variable, or register a free key
    at `docs.airnowapi.org`. A subclass of the cross-backend
    `earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class AirnowCredentials(BaseModel):
    """Frozen value object holding the AirNow API key.

    The key is optional at construction time: `None` means "resolve from
    the `AIRNOW_API_KEY` environment variable at `AirnowAuth.configure`
    time". The real "is there a usable key?" gate is
    `AirnowAuth.configure`, not this model.

    Attributes:
        api_key: The AirNow `API_KEY`, stored as a `pydantic.SecretStr`
            so it is never echoed by `repr(creds)` or in logs. `None`
            defers resolution to the environment variable.

    Examples:
        - Build from an explicit key; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.airnow import AirnowCredentials
            >>> creds = AirnowCredentials(api_key="topsecret")
            >>> creds.api_key.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - The key is optional — rely on the environment instead:
            ```python
            >>> from earthlens.airnow import AirnowCredentials
            >>> AirnowCredentials().api_key is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None


class AirnowAuth(AbstractAuth[AirnowCredentials]):
    """Resolve and hold the AirNow API key.

    Implements the `earthlens.base.AbstractAuth` contract for a
    single-secret backend. Construction does not touch the environment;
    `configure` performs the resolution and is idempotent. After a
    successful `configure()`, the key is available via the `api_key`
    property for the HTTP client to attach as the `API_KEY` query
    argument.

    The class is a context manager (inherited from `AbstractAuth`):
    `with AirnowAuth(creds) as auth: ...` calls `configure()` on enter
    and the default no-op `close()` on exit — there is no per-instance
    resource to release.

    Attributes:
        _creds: The `AirnowCredentials` passed at construction.

    Examples:
        - Resolve an explicit key:
            ```python
            >>> from earthlens.airnow import AirnowAuth, AirnowCredentials
            >>> auth = AirnowAuth(AirnowCredentials(api_key="k"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.api_key
            'k'

            ```
    """

    def __init__(self, credentials: AirnowCredentials) -> None:
        """Store credentials; does not resolve the key yet.

        Args:
            credentials: The `AirnowCredentials` value object carrying
                the optional API key.
        """
        super().__init__(credentials)
        self._configured = False
        self._key: str | None = None

    def configure(self) -> None:
        """Resolve the API key so subsequent requests can authenticate.

        Idempotent — short-circuits when `is_authenticated` already
        returns `True`. On the first call, resolves the key in this
        order: the explicit `api_key` on the credentials, then the
        `AIRNOW_API_KEY` environment variable.

        Raises:
            AuthenticationError: When neither source supplies a key. The
                message names the `api_key=` argument, the
                `AIRNOW_API_KEY` env var, and the free-registration URL —
                it never blocks on an interactive prompt.
        """
        if self.is_authenticated():
            return
        key = (
            self._creds.api_key.get_secret_value()
            if self._creds.api_key is not None
            else os.environ.get("AIRNOW_API_KEY")
        )
        if not key:
            raise AuthenticationError(
                "no AirNow API key available: pass api_key= to AirNow(...) "
                "or set the AIRNOW_API_KEY environment variable. Register a "
                f"free key at {_REGISTER_URL}."
            )
        self._key = key
        self._configured = True

    @property
    def api_key(self) -> str:
        """The resolved API key; valid only after `configure`.

        Returns:
            str: The AirNow `API_KEY` string.

        Raises:
            AuthenticationError: When read before `configure` has
                resolved a key.
        """
        if self._key is None:
            raise AuthenticationError(
                "AirnowAuth.configure() has not run yet; no API key resolved."
            )
        return self._key
