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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base import AbstractAuth, AuthenticationError

#: Where a user without an Earthdata account registers (free).
_REGISTER_URL = "https://urs.earthdata.nasa.gov/users/new"

#: Where an authenticated user accepts the outstanding data-use agreements that
#: otherwise turn a download into a `401 Be sure to agree to the EULA`.
_EULA_URL = "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas"


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
        """Return whether both an explicit username and password were given."""
        return self._creds.username is not None and self._creds.password is not None

    def _has_explicit_token(self) -> bool:
        """Return whether an explicit EDL bearer token was given."""
        return self._creds.token is not None

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

    def _export_explicit_credentials(self) -> None:
        """Put an explicitly-passed credential where `earthaccess` will read it.

        `earthaccess.login` takes no token or username/password argument — its
        environment strategy reads `EARTHDATA_TOKEN` (preferred) or
        `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` — so whichever explicit
        credential was supplied is exported before the login call.
        """
        if self._has_explicit_token() and self._creds.token is not None:
            os.environ["EARTHDATA_TOKEN"] = self._creds.token.get_secret_value()
        elif (
            self._has_explicit_credentials()
            and self._creds.username is not None
            and self._creds.password is not None
        ):
            os.environ["EARTHDATA_USERNAME"] = self._creds.username
            os.environ["EARTHDATA_PASSWORD"] = self._creds.password.get_secret_value()

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
        for var in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
            if os.environ.get(var) == "":
                os.environ.pop(var, None)

        try:
            import earthaccess  # lazy — only needed when actually logging in
        except ImportError as exc:
            raise ImportError(
                "the GDIS sources need `earthaccess`, which is not installed. "
                "Install the extra with `pip install earthlens[emdat]`. The "
                "`emdat:events` source needs no credentials and no extra."
            ) from exc

        strategy = self._resolve_strategy()
        if strategy == "environment":
            self._export_explicit_credentials()

        try:
            auth = earthaccess.login(strategy=strategy, persist=True)
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                "Earthdata Login failed while contacting EDL "
                f"(strategy={strategy!r}): {type(exc).__name__}: {exc}. Set "
                "EARTHDATA_USERNAME / EARTHDATA_PASSWORD, add a 'machine "
                "urs.earthdata.nasa.gov' entry to ~/.netrc, or register a free "
                f"account at {_REGISTER_URL}."
            ) from exc

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
