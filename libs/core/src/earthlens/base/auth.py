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

import os
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


class SingleSecretAuth(AbstractAuth[CredentialsT]):
    """`AbstractAuth` for a backend authenticated by a single secret.

    Many backends authenticate with one API key or token: resolve it from an
    explicit argument, fall back to one or more environment variables, raise
    `AuthenticationError` when none is found, then memoise so a second
    `configure()` is a no-op. That ceremony is identical across those backends —
    only *which* env var, *how* the explicit value is stored, and *what* to do
    with the resolved secret differ. This class carries the ceremony so each
    single-secret backend supplies only the parts that differ:

    * set :attr:`ENV_VARS` and :attr:`PROVIDER` (and optionally
      :attr:`CREDENTIAL_HINT` / :attr:`AUTH_ERROR`),
    * override :meth:`_explicit_credential` to read the explicit secret off its
      credentials value object,
    * implement :meth:`_connect` to apply the resolved secret.

    A multi-field / OAuth / username+password backend does not fit this shape and
    should extend :class:`AbstractAuth` directly with its own `configure`.

    Examples:
        - A minimal single-secret auth resolving from the environment:
            ```python
            >>> import os
            >>> from pydantic import BaseModel, SecretStr
            >>> from earthlens.base import SingleSecretAuth
            >>> class _Creds(BaseModel):
            ...     token: SecretStr | None = None
            >>> class _Auth(SingleSecretAuth[_Creds]):
            ...     ENV_VARS = ("DEMO_TOKEN",)
            ...     PROVIDER = "Demo"
            ...     def _explicit_credential(self):
            ...         tok = self._creds.token
            ...         return tok.get_secret_value() if tok is not None else None
            ...     def _connect(self, credential):
            ...         self._token = credential
            >>> os.environ["DEMO_TOKEN"] = "from-env"
            >>> auth = _Auth(_Creds())
            >>> auth.configure()
            >>> auth._token
            'from-env'
            >>> del os.environ["DEMO_TOKEN"]

            ```
        - An explicit credential wins over the environment, and a missing one
          raises a message naming the provider and the variable:
            ```python
            >>> from pydantic import BaseModel, SecretStr
            >>> from earthlens.base import AuthenticationError, SingleSecretAuth
            >>> class _Creds(BaseModel):
            ...     token: SecretStr | None = None
            >>> class _Auth(SingleSecretAuth[_Creds]):
            ...     ENV_VARS = ("DEMO_TOKEN",)
            ...     PROVIDER = "Demo"
            ...     def _explicit_credential(self):
            ...         tok = self._creds.token
            ...         return tok.get_secret_value() if tok is not None else None
            ...     def _connect(self, credential):
            ...         self._token = credential
            >>> _Auth(_Creds(token="explicit")).configure() or None
            >>> try:
            ...     _Auth(_Creds()).configure()
            ... except AuthenticationError as exc:
            ...     str(exc)
            'no Demo credential available: pass it explicitly or set DEMO_TOKEN.'

            ```
    """

    #: Environment variables consulted, in priority order, after the explicit
    #: argument. The first one that is set (non-empty) supplies the secret.
    ENV_VARS: tuple[str, ...] = ()

    #: Human-readable provider name used in the missing-credential message.
    PROVIDER: str = ""

    #: Name of the constructor argument that carries the explicit secret (e.g.
    #: `"api_key"` or `"token"`). Named in the missing-credential message so it
    #: tells the caller exactly what to pass. Empty falls back to the generic
    #: "pass it explicitly".
    CREDENTIAL_ARG: str = ""

    #: Optional extra sentence appended to that message (e.g. a sign-up URL).
    CREDENTIAL_HINT: str = ""

    #: Exception type raised when no credential resolves. A subclass may point
    #: this at its own `AuthenticationError` subclass so callers can catch it
    #: narrowly while the message stays consistent across providers.
    AUTH_ERROR: type[AuthenticationError] = AuthenticationError

    def configure(self) -> None:
        """Resolve and apply the secret once; idempotent.

        Short-circuits when :meth:`is_authenticated` is already `True`; otherwise
        resolves the credential (:meth:`_resolve_credential`), applies it
        (:meth:`_connect`), and records success (:meth:`mark_configured`).

        Raises:
            AuthenticationError: When no credential can be resolved.
        """
        if self.is_authenticated():
            return
        self._connect(self._resolve_credential())
        self.mark_configured()

    def _resolve_credential(self) -> str:
        """Return the secret: explicit argument, else :attr:`ENV_VARS`, else raise.

        The environment is consulted only when no explicit credential was
        supplied — i.e. :meth:`_explicit_credential` returns `None`. An explicit
        credential that is present but empty is treated as an error and does not
        fall back to the environment, matching each backend's original behaviour.

        Returns:
            The resolved secret string.

        Raises:
            AuthenticationError: When no explicit credential was supplied and
                none of :attr:`ENV_VARS` is set, or when an explicit credential
                was supplied but is empty. The message names :attr:`PROVIDER` and
                the variables, plus :attr:`CREDENTIAL_HINT`.
        """
        explicit = self._explicit_credential()
        if explicit:
            return explicit
        # Only an *absent* explicit credential (None) falls back to the
        # environment; a present-but-empty one is an error, so it skips the
        # env lookup and drops straight to the raise below.
        if explicit is None:
            for variable in self.ENV_VARS:
                value = os.environ.get(variable)
                if value:
                    return value
        names = " or ".join(self.ENV_VARS) or "a credential"
        how = (
            f"pass {self.CREDENTIAL_ARG}="
            if self.CREDENTIAL_ARG
            else "pass it explicitly"
        )
        message = (
            f"no {self.PROVIDER or type(self).__name__} credential available: "
            f"{how} or set {names}."
        )
        if self.CREDENTIAL_HINT:
            message = f"{message} {self.CREDENTIAL_HINT}"
        raise self.AUTH_ERROR(message)

    def _explicit_credential(self) -> str | None:
        """Return the explicit secret off the bound credentials, or `None`.

        Default: `None` (rely on :attr:`ENV_VARS`). A subclass whose credentials
        carry an explicit secret overrides this to extract it — typically
        unwrapping a `pydantic.SecretStr`.
        """
        return None

    @abstractmethod
    def _connect(self, credential: str) -> None:
        """Apply the resolved secret (store it, build a client, …).

        Args:
            credential: The secret resolved by :meth:`_resolve_credential`.
        """
