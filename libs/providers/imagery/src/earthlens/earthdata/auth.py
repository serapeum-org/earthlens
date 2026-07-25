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
from typing import Any, cast

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
    knows about: an explicit EDL **bearer token** (like GEE's key /
    OpenAQ's API key — no password needed), an explicit
    username/password, a path to a `.netrc` file, or no fields at all
    (and rely on env vars / the default `~/.netrc` / an interactive
    prompt). Validation is intentionally permissive — the real "do these
    creds work?" gate is :meth:`EarthdataAuth.configure`, which talks to
    EDL.

    Attributes:
        username: EDL account username. `None` means "look at the
            environment / netrc / prompt".
        password: Account password. Stored as
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` means same as
            `username`.
        token: Optional EDL **bearer token** (a long-lived JSON Web
            Token generated from the EDL profile), stored as a
            :class:`pydantic.SecretStr`. Lets the backend authenticate
            without a password — the token-equivalent of GEE's service
            key. `configure` exports it to `EARTHDATA_TOKEN`, which
            `earthaccess`'s environment strategy consumes. `None` defers
            to the `EARTHDATA_TOKEN` env var / username-password / netrc.
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
        self._auth: Any = None
        self._configured = False

    def _has_explicit_credentials(self) -> bool:
        """Return whether both an explicit username and password were given."""
        return self._creds.username is not None and self._creds.password is not None

    def _has_explicit_token(self) -> bool:
        """Return whether an explicit EDL bearer token was given."""
        return self._creds.token is not None

    def _resolve_strategy(self) -> str:
        """Pick the `earthaccess.login` strategy.

        Returns the first viable source in this order: an explicit EDL
        bearer `token` or `username` + `password` passed to the
        constructor (fed to `earthaccess` via the environment strategy),
        then the `EARTHDATA_TOKEN` or `EARTHDATA_USERNAME` /
        `EARTHDATA_PASSWORD` environment variables, then a `.netrc`
        holding an EDL entry, then an interactive prompt as the last
        resort. `earthaccess`'s environment strategy accepts either a
        token or a username/password pair.

        Returns:
            str: One of `"environment"`, `"netrc"`, `"interactive"`.
        """
        if self._has_explicit_token() or self._has_explicit_credentials():
            return "environment"
        if os.getenv("EARTHDATA_TOKEN"):
            return "environment"
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
        strategy (explicit token / creds → env → netrc → interactive).
        `earthaccess` has no direct login argument for a token or a
        username/password, so an explicit credential is exported to the
        environment variable its `"environment"` strategy reads —
        `EARTHDATA_TOKEN` for a bearer token, or `EARTHDATA_USERNAME` /
        `EARTHDATA_PASSWORD` for a username/password — before login. Then
        logs in with `persist=True` and keeps the returned
        `earthaccess.Auth` handle on :attr:`_auth`.

        Raises:
            AuthenticationError: When `earthaccess.login` returns an
                unauthenticated handle (bad / missing credentials) or
                raises while contacting EDL.
        """
        if self.is_authenticated():
            return

        # A CI workflow that maps an undefined secret onto an env var leaves it
        # as an empty string (e.g. `EARTHDATA_TOKEN: ${{ secrets.EARTHDATA_TOKEN }}`
        # when no such secret exists). `earthaccess` treats a present-but-empty
        # EARTHDATA_TOKEN as a real token and fails with "Token does not exist",
        # masking valid username/password env vars. Drop any empty EDL env var so
        # the strategy resolves to the credential that is actually set.
        for _var in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
            if os.environ.get(_var) == "":
                os.environ.pop(_var, None)

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
        if strategy == "environment":
            # earthaccess has no direct token / username-password argument;
            # its environment strategy reads EARTHDATA_TOKEN (preferred) or
            # EARTHDATA_USERNAME / EARTHDATA_PASSWORD. Export whichever
            # explicit credential was supplied so it reaches the login.
            if self._has_explicit_token():
                assert (
                    self._creds.token is not None
                )  # _has_explicit_token guarantees it
                os.environ["EARTHDATA_TOKEN"] = self._creds.token.get_secret_value()
            elif self._has_explicit_credentials():
                # _has_explicit_credentials guarantees both are non-None
                assert self._creds.username is not None
                assert self._creds.password is not None
                os.environ["EARTHDATA_USERNAME"] = self._creds.username
                os.environ["EARTHDATA_PASSWORD"] = (
                    self._creds.password.get_secret_value()
                )
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

    def s3_credentials(self, provider: str) -> dict[str, str]:
        """Return rotating S3 credentials for one CMR provider.

        Delegates to `earthaccess.Auth.get_s3_credentials`. The
        credentials rotate roughly hourly; callers that hit an
        `AccessDenied` mid-stream should re-call this to refresh.

        Note:
            The backend's in-region fetch path uses `earthaccess.open`,
            which mints and refreshes S3 credentials internally, so it
            does not call this method. It is provided as forward-facing
            API for callers that want to read in-region S3 directly
            (e.g. with `s3fs`).

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
        return cast("dict[str, str]", self._auth.get_s3_credentials(provider=provider))
