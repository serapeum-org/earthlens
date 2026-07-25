"""Cross-backend authentication base class.

Hosts `AbstractAuth`, an ABC every backend's auth class inherits,
plus `AuthenticationError`, the shared exception type. Promotes a
pattern duplicated across the existing
`earthlens.gee.auth.EarthEngineAuth` and the planned per-store
ECMWF / NASA Earthdata / EUMETSAT / JAXA auth classes into a single
contract so every backend's auth surface is identifiable,
composable, and consistently testable.

The shape:

* A pydantic `BaseModel` (or any frozen value object the backend
  prefers) carries the credentials.
* `AbstractAuth` is generic in that credentials type — so
  `EarthEngineAuth` extends `AbstractAuth[EarthEngineCredentials]`,
  the planned `EarthdataAuth` extends
  `AbstractAuth[EarthdataCredentials]`, etc.
* `configure` performs whatever one-time setup the backend needs
  (`ee.Initialize` for GEE, `earthaccess.login()` for Earthdata,
  write `~/.cdsapirc` for ECMWF, mint an OAuth bearer for CDSE).
  It is idempotent — calling it twice is a no-op once
  `is_authenticated` returns `True`.
* `is_authenticated` is a cheap predicate that says whether the
  in-process state already has working credentials.

The class is a context manager so callers can write
`with FooAuth(creds) as auth: ...` for short-lived sessions; the
default `__exit__` is a no-op because most backends configure the
client globally (rasterio Env, `ee.Initialize`, `cdsapi.Client`)
and have nothing to tear down. Subclasses that genuinely need
teardown override `close`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

CredentialsT = TypeVar("CredentialsT", bound=BaseModel)
"""Type variable for the credentials value object an `AbstractAuth` subclass binds to.

Constrained to `pydantic.BaseModel` so every credentials class is a
validated, frozen-by-default container with consistent repr and
serialisation semantics. A subclass of `AbstractAuth` binds to a
specific concrete credentials type via its generic parameter, e.g.
`class EarthEngineAuth(AbstractAuth[EarthEngineCredentials])`.
"""


class AuthenticationError(Exception):
    """Raised when a backend cannot establish an authenticated session.

    Subclasses re-raise this with backend-specific context (missing
    `~/.cdsapirc`, unregistered Earth Engine project, expired CDSE
    token, missing Earthdata Login). Every backend's auth class
    catches the underlying SDK / HTTP exception and wraps it with
    an actionable message — never propagates a raw
    `cdsapi.api.Exception` or `ee.EEException` to the user.

    The class is intentionally a flat `Exception` subclass and not
    `ConnectionError` because half the failure modes are not
    network errors (no credentials at all, malformed key file,
    misconfigured project IAM). Callers should catch
    `AuthenticationError` directly rather than its causes.

    Examples:
        - The error preserves its constructor message:
            ```python
            >>> from earthlens.base import AuthenticationError
            >>> exc = AuthenticationError("missing ~/.cdsapirc")
            >>> str(exc)
            'missing ~/.cdsapirc'

            ```
        - Catch every backend's auth failure with one clause:
            ```python
            >>> from earthlens.base import AuthenticationError
            >>> try:
            ...     raise AuthenticationError("token expired")
            ... except AuthenticationError as exc:
            ...     handled = str(exc)
            >>> handled
            'token expired'

            ```
    """


class AbstractAuth(ABC, Generic[CredentialsT]):
    """Blueprint for every backend's auth class.

    Concrete subclasses bind a credentials type (the `CredentialsT`
    type parameter) and implement two methods:

    * `configure` — perform whatever one-time setup the backend
      needs (call `ee.Initialize`, write `~/.cdsapirc`, fetch an
      OAuth bearer, etc.). Must be idempotent: calling it after
      `is_authenticated` returns `True` is a no-op.
    * `is_authenticated` — return `True` when the in-process state
      has working credentials and the next download call will
      succeed without re-authenticating.

    The class is a context manager: `with FooAuth(creds) as auth:`
    enters by calling `configure` and exits by calling `close`. The
    default `close` does nothing because most backends configure
    their SDK globally; subclasses that genuinely hold a closeable
    resource (e.g. an HTTP session, a boto3 client) override
    `close` to release it.

    Attributes:
        _creds: The credentials value object passed at
            construction. Stored verbatim so subclasses can read
            individual fields (`self._creds.username`,
            `self._creds.api_key`). Not re-exported as a public
            attribute — concrete classes decide which fields are
            safe to surface (a service-account email is fine; a
            secret is not).

    Examples:
        - A minimal concrete subclass that flips an internal flag:
            ```python
            >>> from pydantic import BaseModel, SecretStr
            >>> from earthlens.base import AbstractAuth
            >>> class _Creds(BaseModel):
            ...     token: SecretStr
            >>> class _Auth(AbstractAuth[_Creds]):
            ...     def __init__(self, creds):
            ...         super().__init__(creds)
            ...         self._authed = False
            ...     def configure(self):
            ...         if self.is_authenticated():
            ...             return
            ...         self._authed = True
            ...     def is_authenticated(self):
            ...         return self._authed
            >>> auth = _Auth(_Creds(token="abc"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True

            ```
        - The context-manager form configures on enter:
            ```python
            >>> from pydantic import BaseModel, SecretStr
            >>> from earthlens.base import AbstractAuth
            >>> class _Creds(BaseModel):
            ...     token: SecretStr
            >>> class _Auth(AbstractAuth[_Creds]):
            ...     def __init__(self, creds):
            ...         super().__init__(creds)
            ...         self._authed = False
            ...     def configure(self):
            ...         self._authed = True
            ...     def is_authenticated(self):
            ...         return self._authed
            >>> with _Auth(_Creds(token="x")) as auth:
            ...     auth.is_authenticated()
            True

            ```
    """

    #: Set to `True` by :meth:`mark_configured`; read by the default
    #: :meth:`is_authenticated`. Class-level so an instance that never
    #: configured still answers `False` without an `__init__` of its own.
    _configured: bool = False

    def __init__(self, credentials: CredentialsT) -> None:
        """Store the credentials value object.

        Args:
            credentials: A frozen / validated value object holding the
                secrets the backend needs (service-account email + key
                path, CDS URL + API key, EDL username + password, …).
                Type-parameterised on the concrete subclass so
                `MyAuth(MyCreds(...))` is type-checked.
        """
        self._creds = credentials

    @abstractmethod
    def configure(self) -> None:
        """Perform the one-time setup so subsequent calls work without re-auth.

        Subclasses implement this to (e.g.) call `ee.Initialize`,
        write a credentials file, or mint an OAuth bearer. Must be
        idempotent: a second call after :meth:`is_authenticated`
        returns `True` is a no-op (typical implementation is an
        early-return guarded by `if self.is_authenticated(): return`).

        Raises:
            AuthenticationError: When the credentials are
                missing/invalid or the backend rejects them.
        """

    def mark_configured(self) -> None:
        """Record that :meth:`configure` completed, for the default predicate.

        Call this at the end of a successful `configure()` so the inherited
        :meth:`is_authenticated` starts returning `True` and the next
        `configure()` short-circuits.
        """
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` when the in-process state has working credentials.

        Cheap predicate — must not call the network. Used by
        :meth:`configure` for idempotency and by callers that want to
        skip a redundant setup pass.

        The default reports whether :meth:`mark_configured` has run, which is
        the "did `configure()` succeed?" flag the majority of the auth classes
        each declared by hand. Override it when the real answer lives elsewhere
        — an SDK's own session object (asf), a token expiry, or a backend whose
        credentials are always present (ghsl, worldpop return `True`).
        """
        return self._configured

    def close(self) -> None:
        """Release any resource held by :meth:`configure`.

        Default: no-op. Backends whose `configure()` opens a
        long-lived HTTP session, boto3 client, or background thread
        override this to close it. Most backends configure their SDK
        globally and have nothing to release here.
        """

    def __enter__(self) -> AbstractAuth[CredentialsT]:
        self.configure()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        self.close()
