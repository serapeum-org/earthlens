"""Credentials and authentication for the Copernicus Marine backend.

Hosts :class:`CmemsAuth`, an :class:`earthlens.base.AbstractAuth`
subclass that wraps :func:`copernicusmarine.login`. The toolbox stores
credentials in a per-user configuration directory (default
`~/.copernicusmarine/`); after a successful `login()` every
subsequent `subset()` / `open_dataset()` call in the same process
(or any future process that reads the same config directory) is
authenticated automatically.

The auth wrapper exists so that:

* The facade can build a :class:`CmemsCredentials` value object up
  front, validate it with pydantic, and pass it through
  `super().__init__(creds)` — consistent with `EarthEngineAuth`
  (C2's reference shape).
* `configure()` is idempotent — a second call after
  `is_authenticated()` returns `True` short-circuits, so it is safe
  to call from long-lived workers without re-authing on every
  `download()`.
* Toolbox errors (`InvalidUsernameOrPassword`,
  `CouldNotConnectToAuthenticationSystem`,
  `CredentialsCannotBeNone`) are re-raised as the cross-backend
  :class:`AuthenticationError`, so a caller can write one
  `except AuthenticationError` clause across CMEMS / GEE / future
  backends.

The credentials source-of-truth priority used by `configure()` is:

1. Explicit `username` + `password` passed at construction time.
2. `COPERNICUSMARINE_SERVICE_USERNAME` /
   `COPERNICUSMARINE_SERVICE_PASSWORD` environment variables
   (toolbox-native).
3. A pre-existing `~/.copernicusmarine/.copernicusmarine-credentials`
   from a previous `copernicusmarine login` (CLI or library).
4. An explicit `credentials_file=` path.

If none of the above resolve, `configure()` raises
:class:`AuthenticationError` rather than blocking on the toolbox's
interactive prompt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

if TYPE_CHECKING:
    pass


_DOCS_URL = "https://help.marine.copernicus.eu/en/articles/8230433-copernicus-marine-toolbox-installation"
_REGISTER_URL = "https://marine.copernicus.eu/register"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when the Copernicus Marine toolbox cannot authenticate.

    Wraps the underlying `copernicusmarine` exceptions
    (`InvalidUsernameOrPassword`,
    `CouldNotConnectToAuthenticationSystem`,
    `CredentialsCannotBeNone`) with a message that names a fix:
    register at the Copernicus Marine portal, pass explicit
    `service_username` / `service_password`, or set the
    `COPERNICUSMARINE_SERVICE_USERNAME` /
    `COPERNICUSMARINE_SERVICE_PASSWORD` environment variables.

    A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch
    every backend's auth failure with one `except` clause.
    """


class CmemsCredentials(BaseModel):
    """Frozen value object holding the Copernicus Marine credentials.

    The auth wrapper accepts every CMEMS-credential source the
    `copernicusmarine` toolbox knows about: explicit
    username/password, a path to a pre-existing credentials file, or
    no fields at all (and rely on env vars / a saved configuration
    directory). Validation is intentionally permissive — the real
    "do these creds work?" gate is :meth:`CmemsAuth.configure`,
    which talks to the auth server.

    Attributes:
        username: Copernicus Marine portal account email or username.
            `None` means "look at the environment / saved config".
        password: Account password. Stored as
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` means same as
            `username`.
        credentials_file: Optional path to a
            `.copernicusmarine-credentials` file produced by a
            previous `copernicusmarine login` invocation. The
            toolbox reads this file directly when supplied; useful
            for CI runners that mount the credentials as a secret.

    Examples:
        - Build from explicit username + password:
            ```python
            >>> from earthlens.cmems import CmemsCredentials
            >>> creds = CmemsCredentials(username="alice", password="secret")
            >>> creds.username
            'alice'
            >>> creds.password.get_secret_value()
            'secret'

            ```
        - All fields optional — rely on env vars / saved config:
            ```python
            >>> from earthlens.cmems import CmemsCredentials
            >>> creds = CmemsCredentials()
            >>> creds.username is None and creds.password is None
            True

            ```
        - SecretStr hides the password in repr:
            ```python
            >>> from earthlens.cmems import CmemsCredentials
            >>> creds = CmemsCredentials(username="u", password="topsecret")
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    username: str | None = None
    password: SecretStr | None = None
    credentials_file: Path | None = None


class CmemsAuth(AbstractAuth[CmemsCredentials]):
    """Authenticate the Copernicus Marine toolbox.

    Wraps :func:`copernicusmarine.login` in the
    :class:`earthlens.base.AbstractAuth` contract (C2). Calling
    :meth:`configure` once on a process is enough: the toolbox
    writes a credentials file under
    `~/.copernicusmarine/` (overridable via
    `configuration_file_directory`) and every subsequent
    `copernicusmarine.subset()` / `.open_dataset()` call in the same
    process — or in a later process that reads the same directory —
    is authenticated automatically.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with CmemsAuth(creds) as auth: ...`
    calls `configure()` on enter and the default no-op `close()` on
    exit (the toolbox has no per-instance handle to release; the
    credentials are file-backed).

    Attributes:
        _creds: The :class:`CmemsCredentials` passed at
            construction. Read by :meth:`configure` to resolve
            username/password/credentials-file. Treated as
            write-once.

    Examples:
        - Build, configure, inspect — credentials come from env vars
          if present, otherwise from the toolbox's saved config dir.
          Marked `# doctest: +SKIP` because it makes a real
          auth-server call when no saved creds exist:

            ```python
            >>> from earthlens.cmems import CmemsAuth, CmemsCredentials
            >>> auth = CmemsAuth(CmemsCredentials())  # doctest: +SKIP
            >>> auth.configure()  # doctest: +SKIP
            >>> auth.is_authenticated()  # doctest: +SKIP
            True

            ```
    """

    def __init__(self, credentials: CmemsCredentials) -> None:
        """Store credentials; does not authenticate.

        Unlike :class:`earthlens.gee.EarthEngineAuth`, construction
        does not call the network — the user must call
        :meth:`configure` (or use the context-manager form). This
        keeps `CMEMS(...)` construction side-effect-free for
        callers who only want to inspect `space` / `time` without
        actually downloading.

        Args:
            credentials: The :class:`CmemsCredentials` value object
                carrying the username/password/credentials-file
                resolution rules.
        """
        super().__init__(credentials)
        self._configured = False

    def configure(self) -> None:
        """Authenticate against the Copernicus Marine portal.

        Idempotent — short-circuits when :meth:`is_authenticated`
        already returns `True`. On the first call, resolves
        credentials in the order documented at the module level:
        explicit credentials passed in, then env vars, then a saved
        credentials file, then the toolbox's config directory.

        Raises:
            AuthenticationError: When the toolbox rejects the
                credentials, when the auth server is unreachable, or
                when no credentials source resolves and the toolbox
                would otherwise drop into an interactive prompt.

        Examples:
            - First call authenticates; second is a no-op:

                ```python
                >>> from earthlens.cmems import CmemsAuth, CmemsCredentials
                >>> auth = CmemsAuth(CmemsCredentials())  # doctest: +SKIP
                >>> auth.configure()  # doctest: +SKIP
                >>> auth.is_authenticated()  # doctest: +SKIP
                True
                >>> auth.configure()  # no-op  # doctest: +SKIP

                ```
        """
        if self.is_authenticated():
            return

        import copernicusmarine as cm

        username = self._creds.username or os.environ.get(
            "COPERNICUSMARINE_SERVICE_USERNAME"
        )
        password_secret = self._creds.password
        password = (
            password_secret.get_secret_value()
            if password_secret is not None
            else os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")
        )
        credentials_file = self._creds.credentials_file

        if username is None and password is None and credentials_file is None:
            if not _saved_credentials_present():
                raise AuthenticationError(
                    "no Copernicus Marine credentials available. Pass "
                    "service_username=/service_password= to CMEMS(...), "
                    "set the COPERNICUSMARINE_SERVICE_USERNAME / "
                    "COPERNICUSMARINE_SERVICE_PASSWORD environment "
                    "variables, or run `copernicusmarine login` once "
                    f"to save credentials. Register a free account at "
                    f"{_REGISTER_URL}. See {_DOCS_URL} for details."
                )

        try:
            ok = cm.login(
                username=username,
                password=password,
                credentials_file=credentials_file,
                check_credentials_valid=True,
                force_overwrite=True,
            )
        except cm.InvalidUsernameOrPassword as exc:
            raise AuthenticationError(
                "Copernicus Marine rejected the supplied credentials. "
                f"Check the username and password (register at {_REGISTER_URL} "
                "if you do not yet have an account)."
            ) from exc
        except cm.CouldNotConnectToAuthenticationSystem as exc:
            raise AuthenticationError(
                "Could not reach the Copernicus Marine authentication "
                "service. Check network connectivity / proxy settings "
                "and try again."
            ) from exc
        except cm.CredentialsCannotBeNone as exc:
            raise AuthenticationError(
                "Copernicus Marine login received empty credentials. "
                "Pass service_username= / service_password= or set the "
                "COPERNICUSMARINE_SERVICE_USERNAME / "
                "COPERNICUSMARINE_SERVICE_PASSWORD env vars."
            ) from exc

        if not ok:
            raise AuthenticationError(
                "Copernicus Marine login returned False without raising. "
                "This usually means the supplied credentials are "
                f"malformed; see {_DOCS_URL}."
            )

        self.mark_configured()


def _saved_credentials_present() -> bool:
    """Return `True` when the toolbox's default config directory has saved creds.

    The CLI form of the toolbox (`copernicusmarine login`) writes a
    `.copernicusmarine-credentials` file under
    `~/.copernicusmarine/`. When that file is present, calling
    `cm.login()` with `username=None` / `password=None` succeeds
    because the toolbox reads it directly. Detect this so the
    library form (`CmemsAuth().configure()`) can also rely on it
    without forcing the user to re-pass credentials.

    Returns:
        bool: `True` if a saved credentials file is discoverable
            under the toolbox's default config directory.
    """
    default_dir = Path.home() / ".copernicusmarine"
    if not default_dir.is_dir():
        return False
    return any(default_dir.glob(".copernicusmarine-credentials*"))
