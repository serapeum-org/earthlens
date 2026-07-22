"""Credentials and API-key resolution for the GFW datasets of the risk backend.

Hosts :class:`GfwAuth`, an :class:`earthlens.base.AbstractAuth` subclass that
resolves a single Global Forest Watch **`x-api-key`** from, in priority order,
an explicit `api_key=` argument or the `GFW_API_KEY` environment variable. The
GFW Data API requires a key on every `query/json` and geostore request; there
is no username/password, so this is the simplest auth shape — a single secret
string, mirrored on the :class:`earthlens.openaq.OpenaqAuth` resolution chain.

The wrinkle this module exists for: auth in `earthlens.risk_indicators` is
**per source, not backend-global**. ThinkHazard! and INFORM are public and
construct no auth at all; only a dataset whose `provider` is `gfw` builds and
configures a :class:`GfwAuth`. So this surface is reached *conditionally*.

The shape:

* :class:`GfwCredentials` is a frozen pydantic value object carrying the
  optional API key as a :class:`pydantic.SecretStr`.
* :class:`GfwAuth` binds those credentials and resolves the key in
  :meth:`GfwAuth.configure` — explicit key first, then the `GFW_API_KEY`
  environment variable, then a clear :class:`AuthenticationError` naming how to
  create a key (never an interactive prompt).
* `configure()` is idempotent — a second call after
  :meth:`GfwAuth.is_authenticated` returns `True` short-circuits.

The resolved key is read back via the :attr:`GfwAuth.api_key` property and
attached as the `x-api-key` header by
:func:`earthlens.risk_indicators._helpers.gfw_query`.
"""

from __future__ import annotations

import os

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from pydantic import BaseModel, ConfigDict, SecretStr

#: Where a user creates a free GFW API key (MyGFW account -> token -> key).
_CREATE_KEY_URL = (
    "https://www.globalforestwatch.org/help/developers/guides/"
    "create-and-use-an-api-key/"
)


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable GFW API key can be resolved.

    Carries a message that names a fix: pass `api_key=` to
    `EarthLens("gfw", ...)`, set the `GFW_API_KEY` environment variable, or
    create a free key via a MyGFW account. A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch every
    backend's auth failure with one `except` clause.
    """


class GfwCredentials(BaseModel):
    """Frozen value object holding the GFW API key.

    The key is optional at construction time: `None` means "resolve from the
    `GFW_API_KEY` environment variable at :meth:`GfwAuth.configure` time". The
    real "is there a usable key?" gate is :meth:`GfwAuth.configure`, not this
    model.

    Attributes:
        api_key: The GFW `x-api-key`, stored as a :class:`pydantic.SecretStr`
            so it is never echoed by `repr(creds)` or in logs. `None` defers
            resolution to the environment variable.

    Examples:
        - Build from an explicit key; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.risk_indicators import GfwCredentials
            >>> creds = GfwCredentials(api_key="topsecret")
            >>> creds.api_key.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - The key is optional — rely on the environment instead:
            ```python
            >>> from earthlens.risk_indicators import GfwCredentials
            >>> GfwCredentials().api_key is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None


class GfwAuth(AbstractAuth[GfwCredentials]):
    """Resolve and hold the Global Forest Watch API key.

    Implements the :class:`earthlens.base.AbstractAuth` contract for a
    single-secret source. Construction does not touch the environment;
    :meth:`configure` performs the resolution and is idempotent. After a
    successful `configure()`, the key is available via the :attr:`api_key`
    property for the HTTP helpers to attach as the `x-api-key` header.

    The class is a context manager (inherited from :class:`AbstractAuth`):
    `with GfwAuth(creds) as auth: ...` calls `configure()` on enter and the
    default no-op `close()` on exit — there is no per-instance resource to
    release.

    Attributes:
        _creds: The :class:`GfwCredentials` passed at construction.

    Examples:
        - Resolve an explicit key:
            ```python
            >>> from earthlens.risk_indicators import GfwAuth, GfwCredentials
            >>> auth = GfwAuth(GfwCredentials(api_key="k"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.api_key
            'k'

            ```
    """

    def __init__(self, credentials: GfwCredentials) -> None:
        """Store credentials; does not resolve the key yet.

        Args:
            credentials: The :class:`GfwCredentials` value object carrying the
                optional API key.
        """
        super().__init__(credentials)
        self._configured = False
        self._key: str | None = None

    def configure(self) -> None:
        """Resolve the API key so subsequent requests can authenticate.

        Idempotent — short-circuits when :meth:`is_authenticated` already
        returns `True`. On the first call, resolves the key in this order: the
        explicit `api_key` on the credentials, then the `GFW_API_KEY`
        environment variable.

        Raises:
            AuthenticationError: When neither source supplies a key. The
                message names the `api_key=` argument, the `GFW_API_KEY` env
                var, and the key-creation URL — it never blocks on an
                interactive prompt.
        """
        if self.is_authenticated():
            return
        key = (
            self._creds.api_key.get_secret_value()
            if self._creds.api_key is not None
            else os.environ.get("GFW_API_KEY")
        )
        if not key:
            raise AuthenticationError(
                "no GFW API key available: pass api_key= to EarthLens(...) for "
                "a gfw dataset, or set the GFW_API_KEY environment variable. "
                f"Create a free key with a MyGFW account: {_CREATE_KEY_URL}."
            )
        self._key = key
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` once :meth:`configure` has resolved a key.

        Cheap predicate — does not call the network. A return of `True` means a
        usable key is held by this instance.

        Returns:
            bool: `True` after a successful :meth:`configure`, `False` before.
        """
        return self._configured

    @property
    def api_key(self) -> str:
        """The resolved API key; valid only after :meth:`configure`.

        Returns:
            str: The GFW `x-api-key` string.

        Raises:
            AuthenticationError: When read before :meth:`configure` has
                resolved a key.
        """
        if self._key is None:
            raise AuthenticationError(
                "GfwAuth.configure() has not run yet; no API key resolved."
            )
        return self._key
