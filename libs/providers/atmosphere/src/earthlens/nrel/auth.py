"""Credentials and key/email resolution for the NREL backend.

Hosts `NrelAuth`, an `earthlens.base.AbstractAuth` subclass that resolves the
**two** secrets every NREL (NLR) Developer Network request needs — an
**`api_key`** *and* an **`email`** — from, in priority order, explicit
`api_key=` / `email=` arguments or the `NREL_API_KEY` / `NREL_EMAIL`
environment variables. Both are required: NREL's keyed CSV download API attaches
them as `?api_key=…&email=…` query params, and neither defaults.

The shape mirrors `earthlens.openaq.auth`, but carries two secrets instead of
one:

* `NrelCredentials` is a frozen pydantic value object holding the optional API
  key as a `pydantic.SecretStr` and the optional email as a plain string.
* `NrelAuth` binds those credentials and resolves both in `NrelAuth.configure`
  — explicit values first, then the environment, then a clear
  `AuthenticationError` naming whichever variable is missing (never an
  interactive prompt).
* `configure()` is idempotent — a second call after `is_authenticated` returns
  `True` short-circuits.

The key is read back via the `NrelAuth.api_key` property (a `SecretStr`, so it
is never echoed) and the email via `NrelAuth.email`; the backend resolves the
secret with `.get_secret_value()` only at the moment it builds the request URL.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user registers for a free NREL/NLR Developer Network API key.
_REGISTER_URL = "https://developer.nlr.gov/signup/"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable NREL API key + email pair can be resolved.

    Carries a message that names the fix: pass `api_key=` / `email=` to
    `NREL(...)`, or set the `NREL_API_KEY` / `NREL_EMAIL` environment
    variables, and register a free key at `developer.nlr.gov/signup/`. A
    subclass of the cross-backend `earthlens.base.AuthenticationError` so
    callers can catch every backend's auth failure with one `except` clause.
    """


class NrelCredentials(BaseModel):
    """Frozen value object holding the NREL API key and email.

    Both fields are optional at construction time: `None` (or an empty email)
    means "resolve from the `NREL_API_KEY` / `NREL_EMAIL` environment variables
    at `NrelAuth.configure` time". The real "are both present?" gate is
    `NrelAuth.configure`, not this model.

    Attributes:
        api_key: The NREL `api_key`, stored as a `pydantic.SecretStr` so it is
            never echoed by `repr(creds)` or in logs. `None` defers resolution
            to the environment variable.
        email: The registered contact email sent as the `email` query param.
            `None` defers resolution to the environment variable.

    Examples:
        - Build from explicit values; the key is hidden in `repr`:
            ```python
            >>> from earthlens.nrel import NrelCredentials
            >>> creds = NrelCredentials(api_key="topsecret", email="me@example.com")
            >>> creds.api_key.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - Both are optional — rely on the environment instead:
            ```python
            >>> from earthlens.nrel import NrelCredentials
            >>> NrelCredentials().api_key is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None
    email: str | None = None


class NrelAuth(AbstractAuth[NrelCredentials]):
    """Resolve and hold the NREL API key + email.

    Implements the `earthlens.base.AbstractAuth` contract for a two-secret
    backend. Construction does not touch the environment; `configure` performs
    the resolution and is idempotent. After a successful `configure()`, the key
    is available via the `api_key` property (a `SecretStr`) and the email via
    `email` for the backend to attach as the `?api_key=…&email=…` query params.

    The class is a context manager (inherited from `AbstractAuth`):
    `with NrelAuth(creds) as auth: ...` calls `configure()` on enter and the
    default no-op `close()` on exit.

    Attributes:
        _creds: The `NrelCredentials` passed at construction.

    Examples:
        - Resolve explicit credentials:
            ```python
            >>> from earthlens.nrel import NrelAuth, NrelCredentials
            >>> auth = NrelAuth(NrelCredentials(api_key="k", email="me@example.com"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.api_key.get_secret_value()
            'k'
            >>> auth.email
            'me@example.com'

            ```
    """

    def __init__(self, credentials: NrelCredentials) -> None:
        """Store credentials; does not resolve them yet.

        Args:
            credentials: The `NrelCredentials` value object carrying the
                optional API key and email.
        """
        super().__init__(credentials)
        self._configured = False
        self._key: SecretStr | None = None
        self._email: str | None = None

    def configure(self) -> None:
        """Resolve the API key + email so subsequent requests can authenticate.

        Idempotent — short-circuits when `is_authenticated` already returns
        `True`. On the first call, resolves each secret in this order: the
        explicit value on the credentials, then the `NREL_API_KEY` /
        `NREL_EMAIL` environment variable.

        Raises:
            AuthenticationError: When the key is missing (names `NREL_API_KEY`),
                or the key is present but the email is missing (names
                `NREL_EMAIL`). The message points at the free-registration URL;
                it never blocks on an interactive prompt.
        """
        if self.is_authenticated():
            return
        key = self._creds.api_key
        # Treat an absent OR empty explicit key the same as a missing one and
        # fall back to the environment — symmetric with the `email` resolution
        # below, so `api_key=""` (e.g. an unset config value) does not error
        # while `NREL_API_KEY` is set.
        if (key is None or not key.get_secret_value()) and "NREL_API_KEY" in os.environ:
            key = SecretStr(os.environ["NREL_API_KEY"])
        email = self._creds.email or os.environ.get("NREL_EMAIL")
        if key is None or not key.get_secret_value():
            raise AuthenticationError(
                "no NREL API key available: pass api_key= to NREL(...) or set "
                "the NREL_API_KEY environment variable. Register a free key at "
                f"{_REGISTER_URL}."
            )
        if not email:
            raise AuthenticationError(
                "no NREL email available: pass email= to NREL(...) or set the "
                "NREL_EMAIL environment variable. NREL requires the email that "
                "registered the api_key on every request."
            )
        self._key = key
        self._email = email
        self.mark_configured()

    @property
    def api_key(self) -> SecretStr:
        """The resolved API key; valid only after `configure`.

        Returns:
            SecretStr: The NREL `api_key`, wrapped so it is never echoed. The
                caller resolves the plain string with `.get_secret_value()`
                only at the moment it builds the request URL.

        Raises:
            AuthenticationError: When read before `configure` has resolved it.
        """
        if self._key is None:
            raise AuthenticationError(
                "NrelAuth.configure() has not run yet; no API key resolved."
            )
        return self._key

    @property
    def email(self) -> str:
        """The resolved contact email; valid only after `configure`.

        Returns:
            str: The registered email sent as the `email` query param.

        Raises:
            AuthenticationError: When read before `configure` has resolved it.
        """
        if self._email is None:
            raise AuthenticationError(
                "NrelAuth.configure() has not run yet; no email resolved."
            )
        return self._email
