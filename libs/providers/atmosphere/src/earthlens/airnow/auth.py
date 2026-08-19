"""Credentials and API-key resolution for the AirNow backend.

Hosts `AirnowAuth`, an `earthlens.base.SingleSecretAuth` subclass that
resolves a single AirNow API key from, in priority order, an explicit
`api_key=` argument or the `AIRNOW_API_KEY` environment variable. The
AirNow `/aq/data/` service requires a free key (registered at
`docs.airnowapi.org`) on every request as an `API_KEY` query argument;
there is no username/password and no saved config-file dance, so this
is the same single-secret shape as `earthlens.openaq.OpenaqAuth`.

The shape:

* `AirnowCredentials` is a frozen pydantic value object carrying the
  optional API key as a `pydantic.SecretStr`.
* `AirnowAuth` binds those credentials and lets the shared
  `earthlens.base.SingleSecretAuth.configure` resolve the key — explicit
  key first, then the `AIRNOW_API_KEY` env var, then a clear
  `AuthenticationError` naming the free-registration URL (never an
  interactive prompt).
* `configure()` is idempotent — a second call after
  `AirnowAuth.is_authenticated` returns `True` short-circuits.

The resolved key is read back via the `AirnowAuth.api_key` property
and attached as the `API_KEY` query argument by
`earthlens.airnow.client.AirnowClient`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.base.auth import SingleSecretAuth

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


class AirnowAuth(SingleSecretAuth[AirnowCredentials]):
    """Resolve and hold the AirNow API key.

    Implements the `earthlens.base.SingleSecretAuth` contract for a
    single-secret backend: it declares its env var and provider name and
    supplies `_explicit_credential` / `_connect`, while the inherited
    `configure` performs the explicit → env → raise → memoise resolution.
    Construction does not touch the environment; `configure` is idempotent.
    After a successful `configure()`, the key is available via the `api_key`
    property for the HTTP client to attach as the `API_KEY` query argument.

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

    ENV_VARS = ("AIRNOW_API_KEY",)
    PROVIDER = "AirNow"
    CREDENTIAL_ARG = "api_key"
    CREDENTIAL_HINT = f"Register a free key at {_REGISTER_URL}."
    AUTH_ERROR = AuthenticationError

    #: The resolved key, set by `_connect`; `None` until `configure` runs.
    _key: str | None = None

    def _explicit_credential(self) -> str | None:
        """Return the explicit `api_key` off the credentials, if any."""
        api_key = self._creds.api_key
        return api_key.get_secret_value() if api_key is not None else None

    def _connect(self, credential: str) -> None:
        """Store the resolved key for the `api_key` property to read back."""
        self._key = credential

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
