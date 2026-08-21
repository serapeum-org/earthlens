"""Credentials and API-key resolution for the OpenAQ backend.

Hosts :class:`OpenaqAuth`, an :class:`earthlens.base.SingleSecretAuth`
subclass that resolves a single OpenAQ **`X-API-Key`** from, in
priority order, an explicit `api_key=` argument or the
`OPENAQ_API_KEY` environment variable. OpenAQ v3 requires a (free)
key on every request; there is no username/password and no saved
config-file dance, so this is the simplest auth shape in the package
— a single secret string, mirrored on the `CmemsAuth` resolution
chain but without the toolbox login.

The shape:

* :class:`OpenaqCredentials` is a frozen pydantic value object
  carrying the optional API key as a :class:`pydantic.SecretStr`.
* :class:`OpenaqAuth` binds those credentials and lets the shared
  :meth:`earthlens.base.SingleSecretAuth.configure` resolve the key —
  explicit key first, then the `OPENAQ_API_KEY` env var, then a clear
  :class:`AuthenticationError` naming the free-registration URL (never
  an interactive prompt).
* `configure()` is idempotent — a second call after
  :meth:`OpenaqAuth.is_authenticated` returns `True` short-circuits,
  so it is safe to call from long-lived workers.

The resolved key is read back via the :attr:`OpenaqAuth.api_key`
property and attached as the `X-API-Key` header by
:class:`earthlens.openaq.client.OpenaqClient`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.base.auth import SingleSecretAuth

#: Where a user registers for a free OpenAQ API key.
_REGISTER_URL = "https://explore.openaq.org/register"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable OpenAQ API key can be resolved.

    Carries a message that names a fix: pass `api_key=` to
    `OpenAQ(...)`, set the `OPENAQ_API_KEY` environment variable, or
    register a free key at `explore.openaq.org/register`. A subclass
    of the cross-backend :class:`earthlens.base.AuthenticationError`
    so callers can catch every backend's auth failure with one
    `except` clause.
    """


class OpenaqCredentials(BaseModel):
    """Frozen value object holding the OpenAQ API key.

    The key is optional at construction time: `None` means "resolve
    from the `OPENAQ_API_KEY` environment variable at
    :meth:`OpenaqAuth.configure` time". The real "is there a usable
    key?" gate is :meth:`OpenaqAuth.configure`, not this model.

    Attributes:
        api_key: The OpenAQ `X-API-Key`, stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers resolution to the
            environment variable.

    Examples:
        - Build from an explicit key; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.openaq import OpenaqCredentials
            >>> creds = OpenaqCredentials(api_key="topsecret")
            >>> creds.api_key.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - The key is optional — rely on the environment instead:
            ```python
            >>> from earthlens.openaq import OpenaqCredentials
            >>> OpenaqCredentials().api_key is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None


class OpenaqAuth(SingleSecretAuth[OpenaqCredentials]):
    """Resolve and hold the OpenAQ API key.

    Implements the :class:`earthlens.base.SingleSecretAuth` contract
    for a single-secret backend: it declares its env var and provider
    name and supplies `_explicit_credential` / `_connect`, while the
    inherited :meth:`configure` performs the resolution and is
    idempotent. After a successful `configure()`, the key is available
    via the :attr:`api_key` property for the HTTP client to attach as
    the `X-API-Key` header.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with OpenaqAuth(creds) as auth: ...`
    calls `configure()` on enter and the default no-op `close()` on
    exit — there is no per-instance resource to release.

    Attributes:
        _creds: The :class:`OpenaqCredentials` passed at construction.

    Examples:
        - Resolve an explicit key:
            ```python
            >>> from earthlens.openaq import OpenaqAuth, OpenaqCredentials
            >>> auth = OpenaqAuth(OpenaqCredentials(api_key="k"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.api_key
            'k'

            ```
    """

    ENV_VARS = ("OPENAQ_API_KEY",)
    PROVIDER = "OpenAQ"
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
        """The resolved API key; valid only after :meth:`configure`.

        Returns:
            str: The OpenAQ `X-API-Key` string.

        Raises:
            AuthenticationError: When read before :meth:`configure`
                has resolved a key.
        """
        if self._key is None:
            raise AuthenticationError(
                "OpenaqAuth.configure() has not run yet; no API key resolved."
            )
        return self._key
