"""Credentials and authentication for the NASA Earthdata backend.

Hosts :class:`EarthdataAuth`, an :class:`earthlens.base.AbstractAuth`
subclass that wraps :func:`earthaccess.login`. A single Earthdata
Login (EDL) account authenticates once and unlocks every DAAC the
backend reaches through `earthaccess` + CMR; after a successful
`login()` the returned `earthaccess.Auth` handle also mints the
rotating, per-provider S3 credentials the in-region streaming path
needs.

The auth wrapper exists so that:

* The backend can build an :class:`EarthdataCredentials` value object
  up front, validate it with pydantic, and pass it through
  `super().__init__(creds)` — consistent with :class:`CmemsAuth`.
* `configure()` is idempotent — a second call after
  `is_authenticated()` returns `True` short-circuits, so it is safe
  to call from long-lived workers without re-authing on every
  `download()`.
* `earthaccess` login failures are re-raised as the cross-backend
  :class:`earthlens.base.AuthenticationError`, so a caller can write
  one `except AuthenticationError` clause across CMEMS / Earthdata /
  future backends.

The credentials-strategy priority used by `configure()` is:

1. `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment variables
   (`strategy="environment"`).
2. A `~/.netrc` (or explicit `netrc_path`) entry for
   `urs.earthdata.nasa.gov` (`strategy="netrc"`).
3. Interactive prompt (`strategy="interactive"`) — the last resort
   when neither of the above resolves.

`earthaccess.login` itself accepts `strategy="all"`, which cascades
through exactly that order; the explicit ladder here only exists to
fail fast (and report which source was tried) when nothing resolves.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

_REGISTER_URL = "https://urs.earthdata.nasa.gov"
_DOCS_URL = "https://earthaccess.readthedocs.io"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when `earthaccess` cannot authenticate against EDL.

    Wraps the underlying `earthaccess` / EDL failure with a message
    that names a fix: register an Earthdata Login account, set the
    `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment variables,
    or add a `urs.earthdata.nasa.gov` entry to `~/.netrc`.

    A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch
    every backend's auth failure with one `except` clause.
    """


class EarthdataCredentials(BaseModel):
    """Frozen value object holding the Earthdata Login credentials.

    The auth wrapper accepts every EDL-credential source `earthaccess`
    knows about: explicit username/password, a pre-existing token, a
    path to a `.netrc` file, or no fields at all (and rely on env
    vars / the default `~/.netrc` / an interactive prompt). Validation
    is intentionally permissive — the real "do these creds work?" gate
    is :meth:`EarthdataAuth.configure`, which talks to EDL.

    Attributes:
        username: EDL account username. `None` means "look at the
            environment / netrc / prompt".
        password: Account password. Stored as
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` means same as
            `username`.
        token: Optional pre-minted EDL bearer token, stored as
            :class:`pydantic.SecretStr`. Currently informational —
            `earthaccess.login` resolves tokens itself from the
            chosen strategy; the field is carried so a future
            token-first strategy can use it.
        netrc_path: Optional path to a `.netrc` file holding a
            `machine urs.earthdata.nasa.gov` entry. `None` falls back
            to `~/.netrc`.

    Examples:
        - All fields optional — rely on env vars / netrc / prompt:
            ```python
            >>> from earthlens.earthdata import EarthdataCredentials
            >>> creds = EarthdataCredentials()
            >>> creds.username is None and creds.password is None
            True

            ```
        - SecretStr hides the password in repr:
            ```python
            >>> from earthlens.earthdata import EarthdataCredentials
            >>> creds = EarthdataCredentials(username="u", password="topsecret")
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    username: str | None = None
    password: SecretStr | None = None
    token: SecretStr | None = None
    netrc_path: Path | None = None


class EarthdataAuth(AbstractAuth[EarthdataCredentials]):
    """Authenticate against NASA Earthdata Login (EDL).

    Wraps :func:`earthaccess.login` in the
    :class:`earthlens.base.AbstractAuth` contract. Calling
    :meth:`configure` once per process is enough: `earthaccess`
    persists the resolved token (`persist=True`), and the returned
    `earthaccess.Auth` handle — kept on :attr:`_auth` — mints the
    rotating per-provider S3 credentials the in-region streaming path
    needs via :meth:`s3_credentials`.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with EarthdataAuth(creds) as auth: ...`
    calls `configure()` on enter and the default no-op `close()` on
    exit.

    Attributes:
        _creds: The :class:`EarthdataCredentials` passed at
            construction. Read by :meth:`configure` to resolve the
            login strategy. Treated as write-once.
        _auth: The `earthaccess.Auth` handle returned by a successful
            `login()`, or `None` before :meth:`configure` runs.

    Examples:
        - Build, configure, inspect — marked `# doctest: +SKIP`
          because it makes a real EDL call:

            ```python
            >>> from earthlens.earthdata import EarthdataAuth, EarthdataCredentials
            >>> auth = EarthdataAuth(EarthdataCredentials())  # doctest: +SKIP
            >>> auth.configure()  # doctest: +SKIP
            >>> auth.is_authenticated()  # doctest: +SKIP
            True

            ```
    """

    def __init__(self, credentials: EarthdataCredentials) -> None:
        """Store credentials; does not authenticate.

        Construction is side-effect-free — the user (or the backend's
        :meth:`_initialize`) must call :meth:`configure` (or use the
        context-manager form) to talk to EDL.

        Args:
            credentials: The :class:`EarthdataCredentials` value
                object carrying the strategy-resolution rules.
        """
        super().__init__(credentials)
        self._auth = None
        self._configured = False

    def _resolve_strategy(self) -> str:
        """Pick the `earthaccess.login` strategy from the environment.

        Returns the first viable source in the documented order:
        environment variables, then a `.netrc` holding an EDL entry,
        then an interactive prompt as the last resort.

        Returns:
            str: One of `"environment"`, `"netrc"`, `"interactive"`.
        """
        if os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"):
            return "environment"
        netrc_path = self._creds.netrc_path or (Path.home() / ".netrc")
        if netrc_path.exists():
            return "netrc"
        return "interactive"

    def configure(self) -> None:
        """Authenticate against EDL via `earthaccess.login`.

        Idempotent — short-circuits when :meth:`is_authenticated`
        already returns `True`. On the first call, resolves the login
        strategy (env → netrc → interactive), logs in with
        `persist=True`, and keeps the returned `earthaccess.Auth`
        handle on :attr:`_auth`.

        Raises:
            AuthenticationError: When `earthaccess.login` returns an
                unauthenticated handle (bad / missing credentials) or
                raises while contacting EDL.
        """
        if self.is_authenticated():
            return

        try:
            import earthaccess  # lazy — only needed when actually logging in
        except ImportError as exc:
            raise ImportError(
                "the NASA Earthdata backend needs `earthaccess`, which is "
                "not installed. Install the extra with "
                "`pip install earthlens[earthdata]` (earthaccess >=0.18 "
                "requires Python >=3.12)."
            ) from exc

        strategy = self._resolve_strategy()
        try:
            auth = earthaccess.login(strategy=strategy, persist=True)
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                "Earthdata Login failed while contacting EDL "
                f"(strategy={strategy!r}): {type(exc).__name__}: {exc}. "
                "Set EARTHDATA_USERNAME / EARTHDATA_PASSWORD, add a "
                f"'machine urs.earthdata.nasa.gov' entry to ~/.netrc, or "
                f"register a free account at {_REGISTER_URL}."
            ) from exc

        if not getattr(auth, "authenticated", False):
            raise AuthenticationError(
                "Earthdata Login failed — no valid credentials resolved "
                f"(strategy={strategy!r}). Set EARTHDATA_USERNAME / "
                "EARTHDATA_PASSWORD, add a 'machine urs.earthdata.nasa.gov' "
                f"entry to ~/.netrc, or register at {_REGISTER_URL}. "
                f"See {_DOCS_URL} for details."
            )

        self._auth = auth
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` once `configure()` has succeeded for this instance.

        Cheap predicate — does not call `earthaccess` or the network.
        A return of `True` means an EDL login succeeded during the
        current process.

        Returns:
            bool: `True` after a successful :meth:`configure`,
                `False` before.

        Examples:
            - A freshly constructed auth has not yet authenticated:
                ```python
                >>> from earthlens.earthdata import EarthdataAuth, EarthdataCredentials
                >>> auth = EarthdataAuth(EarthdataCredentials(username="u", password="p"))
                >>> auth.is_authenticated()
                False

                ```
        """
        return self._configured

    def s3_credentials(self, provider: str) -> dict[str, str]:
        """Return rotating S3 credentials for one CMR provider.

        Delegates to `earthaccess.Auth.get_s3_credentials`. The
        credentials rotate roughly hourly; callers that hit an
        `AccessDenied` mid-stream should re-call this to refresh.

        Args:
            provider: The CMR provider code whose in-region bucket the
                caller wants to read (e.g. `"GES_DISC"`, `"POCLOUD"`).

        Returns:
            dict[str, str]: The temporary AWS credentials dict
                (`accessKeyId`, `secretAccessKey`, `sessionToken`, …).

        Raises:
            AuthenticationError: When :meth:`configure` has not run, so
                no `earthaccess.Auth` handle is available.
        """
        if self._auth is None:
            raise AuthenticationError(
                "s3_credentials() called before configure(); authenticate "
                "first via configure() or the context-manager form."
            )
        # A1: the first positional param of get_s3_credentials is `daac`,
        # so the provider code must be passed by keyword.
        return self._auth.get_s3_credentials(provider=provider)
