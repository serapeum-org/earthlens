"""Earthdata Login authentication for the GDIS sources.

Only the `gdis:points` / `gdis:polygons` sources need credentials. GDIS used to
be served anonymously from `sedac.ciesin.columbia.edu`, but that host is gone
and the granules now live in NASA Earthdata Cloud behind an Earthdata Login
(EDL), so fetching them needs an EDL account. The `emdat:events` source — the
UCLouvain Dataverse archive — is anonymous and never touches this module.

:class:`EmdatAuth` wraps :func:`earthaccess.login` in the
:class:`earthlens.base.AbstractAuth` contract, resolving credentials from an
explicit token / username+password, then the `EARTHDATA_*` environment
variables, then a `~/.netrc` entry, then an interactive prompt.

Note:
    The `earthlens.earthdata` backend carries an equivalent `EarthdataAuth`.
    It is not reused here on purpose: it belongs to the `earthlens-imagery`
    distribution, and no provider distribution depends on another — each one
    depends only on `earthlens-core`. Importing it would make
    `earthlens-hazards` drag in the whole imagery tree for one login call.

A first download will also fail with a `401` and *"Be sure to agree to the
EULA"* until the account has accepted the SEDAC data licence once, in the
Earthdata profile — authentication alone is not enough. :meth:`EmdatAuth.configure`
surfaces that in its error text.

Two consequences of `earthaccess` keeping its authenticated handle in a
module-level global are worth knowing, and are not things this class can fix:

* Once any login has succeeded in the process, a later `configure()` with
  *different* credentials is a no-op — `earthaccess.login` hands back the
  existing handle. One set of Earthdata credentials per process.
* `configure()` briefly writes to `os.environ`, because `earthaccess.login`
  accepts no credential argument. It restores what it changed, but the window
  is process-global, so concurrent logins with different credentials in
  separate threads are not safe. Authenticate once, up front.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base import AbstractAuth, AuthenticationError

#: Attempts at `earthaccess.login` before a transport failure is fatal. EDL is
#: a remote identity provider reached over the network, so a dropped connection
#: says nothing about whether the credential is valid.
_LOGIN_ATTEMPTS = 3

#: Base seconds for the back-off between login attempts (wait = base * 2**n).
_LOGIN_BACKOFF = 1.0

#: Where a user without an Earthdata account registers (free).
_REGISTER_URL = "https://urs.earthdata.nasa.gov/users/new"

#: Where an authenticated user accepts the outstanding data-use agreements that
#: otherwise turn a download into a `401 Be sure to agree to the EULA`.
_EULA_URL = "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas"


def _restore_env(previous: dict[str, str | None]) -> None:
    """Put environment variables back to the values in `previous`.

    Args:
        previous: Variable name to its value before it was overwritten, or
            `None` when it was unset.
    """
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class EmdatCredentials(BaseModel):
    """Earthdata Login credentials for the GDIS sources.

    Every field is optional: leaving them all unset defers to the
    `EARTHDATA_TOKEN` / `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment
    variables, then `~/.netrc`, then an interactive prompt.

    Attributes:
        username: EDL username. Paired with :attr:`password`.
        password: EDL password, held as a `SecretStr` so it stays out of
            reprs and logs.
        token: An EDL bearer token, used in preference to a username and
            password when both are supplied.
        netrc_path: Path to a `.netrc` holding a
            `machine urs.earthdata.nasa.gov` entry. Defaults to `~/.netrc`.

    Examples:
        - Empty credentials defer to the environment:
            ```python
            >>> from earthlens.emdat import EmdatCredentials
            >>> creds = EmdatCredentials()
            >>> creds.username is None and creds.token is None
            True

            ```
        - `SecretStr` keeps the password out of the repr:
            ```python
            >>> from earthlens.emdat import EmdatCredentials
            >>> creds = EmdatCredentials(username="u", password="topsecret")
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    username: str | None = None
    password: SecretStr | None = None
    token: SecretStr | None = None
    netrc_path: Path | None = None


class EmdatAuth(AbstractAuth[EmdatCredentials]):
    """Authenticate the GDIS sources against NASA Earthdata Login.

    Construction is side-effect-free; :meth:`configure` performs the login and
    is idempotent, so calling it repeatedly costs nothing after the first
    success. The class is a context manager via
    :class:`earthlens.base.AbstractAuth`.

    Attributes:
        _creds: The :class:`EmdatCredentials` passed at construction, read by
            :meth:`configure` to resolve the login strategy.
        _auth: The `earthaccess.Auth` handle returned by a successful login, or
            `None` before :meth:`configure` runs.

    Examples:
        - Build, configure, inspect — marked `# doctest: +SKIP` because it
          makes a real EDL call:

            ```python
            >>> from earthlens.emdat import EmdatAuth, EmdatCredentials
            >>> auth = EmdatAuth(EmdatCredentials())  # doctest: +SKIP
            >>> auth.configure()  # doctest: +SKIP
            >>> auth.is_authenticated()  # doctest: +SKIP
            True

            ```
    """

    def __init__(self, credentials: EmdatCredentials) -> None:
        """Store credentials; does not authenticate.

        Args:
            credentials: The :class:`EmdatCredentials` carrying the
                strategy-resolution rules.
        """
        super().__init__(credentials)
        self._auth: Any = None

    def _has_explicit_credentials(self) -> bool:
        """Report whether both an explicit username and password were given.

        Returns:
            bool: `True` when the constructor received both halves of a pair.
        """
        return self._creds.username is not None and self._creds.password is not None

    def _has_explicit_token(self) -> bool:
        """Report whether an explicit EDL bearer token was given.

        Returns:
            bool: `True` when the constructor received a token.
        """
        return self._creds.token is not None

    def _warn_half_credential(self) -> None:
        """Warn when only one half of a username/password pair was supplied.

        A lone `username=` leaves :meth:`_has_explicit_credentials` false, so
        resolution falls through to the `EARTHDATA_*` variables or `~/.netrc`
        and may authenticate as an entirely different account. Silently
        ignoring what the caller passed is the worst of the options.
        """
        username, password = self._creds.username, self._creds.password
        if (username is None) == (password is None):
            return
        supplied = "username" if username is not None else "password"
        missing = "password" if username is not None else "username"
        logger.warning(
            f"EmdatAuth: a {supplied} was given without a {missing}, so it "
            "cannot be used. Falling back to EARTHDATA_TOKEN / "
            "EARTHDATA_USERNAME+PASSWORD / ~/.netrc, which may authenticate "
            "as a different account. Pass both, or neither."
        )

    def _resolve_strategy(self) -> str:
        """Pick the `earthaccess.login` strategy.

        Returns the first viable source in this order: an explicit bearer token
        or username and password passed to the constructor (fed to
        `earthaccess` through its environment strategy), then the
        `EARTHDATA_TOKEN` or `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`
        environment variables, then a `.netrc` holding an EDL entry, then an
        interactive prompt as the last resort.

        Returns:
            str: One of `"environment"`, `"netrc"`, `"interactive"`.
        """
        self._warn_half_credential()
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

    def _export_explicit_credentials(self) -> dict[str, str | None]:
        """Put an explicitly-passed credential where `earthaccess` will read it.

        `earthaccess.login` takes no token or username/password argument — its
        environment strategy reads `EARTHDATA_TOKEN` (preferred) or
        `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` — so whichever explicit
        credential was supplied is exported before the login call.

        An explicit credential also has to *win*: `earthaccess` prefers
        `EARTHDATA_TOKEN` over a username/password pair, so an unrelated token
        already in the environment would otherwise silently override what the
        caller passed to the constructor.

        Returns:
            dict[str, str | None]: The previous value of every variable this
                touched, so the caller can put the environment back. Leaving a
                credential in `os.environ` would make it process-global and
                inherit into any subprocess the caller later spawns.
        """
        wanted: dict[str, str] = {}
        clear: list[str] = []
        if self._has_explicit_token() and self._creds.token is not None:
            wanted["EARTHDATA_TOKEN"] = self._creds.token.get_secret_value()
        elif (
            self._has_explicit_credentials()
            and self._creds.username is not None
            and self._creds.password is not None
        ):
            wanted["EARTHDATA_USERNAME"] = self._creds.username
            wanted["EARTHDATA_PASSWORD"] = self._creds.password.get_secret_value()
            # earthaccess prefers EARTHDATA_TOKEN over a username/password pair,
            # so an ambient token in the environment would silently beat the
            # credentials the caller passed explicitly. Clear it for the login.
            clear.append("EARTHDATA_TOKEN")

        touched = list(wanted) + clear
        previous = {name: os.environ.get(name) for name in touched}
        os.environ.update(wanted)
        for name in clear:
            os.environ.pop(name, None)
        return previous

    def _login(self, earthaccess: Any, strategy: str) -> Any:
        """Call `earthaccess.login`, retrying a transport failure.

        A refused credential is final and raises on the first attempt. A
        dropped connection to `urs.earthdata.nasa.gov` is not evidence about
        the credential at all, so it is retried with exponential back-off
        before the request is given up on.

        Args:
            earthaccess: The imported `earthaccess` module.
            strategy: The login strategy resolved for this call.

        Returns:
            Any: The `earthaccess.Auth` handle the login returned.

        Raises:
            AuthenticationError: When the login is refused, or when every
                attempt failed to reach EDL.
        """
        for attempt in range(_LOGIN_ATTEMPTS):
            try:
                return earthaccess.login(strategy=strategy, persist=True)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _LOGIN_ATTEMPTS - 1:
                    raise AuthenticationError(
                        f"Earthdata Login could not reach EDL in "
                        f"{_LOGIN_ATTEMPTS} attempts (strategy={strategy!r}): "
                        f"{type(exc).__name__}: {exc}. This is a network "
                        "failure rather than a rejected credential; check "
                        "connectivity to urs.earthdata.nasa.gov and retry."
                    ) from exc
                wait = _LOGIN_BACKOFF * 2**attempt
                logger.warning(
                    f"EMDAT: Earthdata Login could not reach EDL "
                    f"({type(exc).__name__}); retrying in {wait:.0f}s "
                    f"({attempt + 1}/{_LOGIN_ATTEMPTS})."
                )
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
                raise AuthenticationError(
                    "Earthdata Login failed while contacting EDL "
                    f"(strategy={strategy!r}): {type(exc).__name__}: {exc}. Set "
                    "EARTHDATA_USERNAME / EARTHDATA_PASSWORD, add a 'machine "
                    "urs.earthdata.nasa.gov' entry to ~/.netrc, or register a "
                    f"free account at {_REGISTER_URL}."
                ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def configure(self) -> None:
        """Authenticate against EDL via `earthaccess.login`.

        Idempotent — short-circuits when :meth:`is_authenticated` already
        returns `True`. On the first call, resolves the login strategy, exports
        any explicit credential to the environment variable `earthaccess`
        reads, logs in with `persist=True`, and keeps the returned handle.

        Raises:
            ImportError: When `earthaccess` is not installed.
            AuthenticationError: When `earthaccess.login` raises while
                contacting EDL, or returns an unauthenticated handle.
        """
        if self.is_authenticated():
            return

        # A CI workflow that maps an undefined secret onto an env var leaves it
        # as an empty string. `earthaccess` treats a present-but-empty
        # EARTHDATA_TOKEN as a real token and fails with "Token does not
        # exist", masking valid username/password env vars. Drop any empty EDL
        # env var so the strategy resolves to the credential actually set.
        previous: dict[str, str | None] = {}
        for var in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
            if os.environ.get(var) == "":
                previous[var] = os.environ.pop(var)

        # Everything from here restores the environment on the way out, however
        # it leaves — a failed import and a failed login included.
        try:
            try:
                import earthaccess  # lazy — only needed when actually logging in
            except ImportError as exc:
                raise ImportError(
                    "the GDIS sources need `earthaccess`, which is not "
                    "installed. Install the extra with `pip install "
                    "earthlens[emdat]`. The `emdat:events` source needs no "
                    "credentials and no extra."
                ) from exc

            strategy = self._resolve_strategy()
            if strategy == "environment":
                # `setdefault`, not `update`: the empty-var pop above already
                # recorded the true prior value, and the exporter re-samples
                # `os.environ` afterwards, so it would report `None` for a
                # variable that was really `""`.
                for name, value in self._export_explicit_credentials().items():
                    previous.setdefault(name, value)

            auth = self._login(earthaccess, strategy)
        finally:
            # An explicit credential is put in the environment only because
            # earthaccess has no argument for one. Leaving it there would make
            # it process-global and inherit into any later subprocess.
            _restore_env(previous)

        if not getattr(auth, "authenticated", False):
            raise AuthenticationError(
                "Earthdata Login failed — no valid credentials resolved "
                f"(strategy={strategy!r}). Set EARTHDATA_USERNAME / "
                "EARTHDATA_PASSWORD, add a 'machine urs.earthdata.nasa.gov' "
                f"entry to ~/.netrc, or register at {_REGISTER_URL}. Note that "
                "an authenticated account must also accept the SEDAC data-use "
                f"agreement once at {_EULA_URL}, or the download returns 401."
            )

        self._auth = auth
        self.mark_configured()
