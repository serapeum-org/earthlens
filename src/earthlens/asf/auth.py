"""Credentials and authentication for the ASF InSAR backend.

Hosts :class:`ASFAuth`, an :class:`earthlens.base.AbstractAuth`
subclass that wraps :class:`earthlens.earthdata.EarthdataAuth` to
produce an authenticated `asf_search.ASFSession`. ASF SAR data lives
behind NASA Earthdata Login (EDL), so the credential surface is the
same EDL bearer-token / username-password / `~/.netrc` ladder the
earthdata backend already implements — there is no second login
system.

The wrapper:

* Builds an :class:`EarthdataCredentials` from the four optional
  inputs (`token`, `username`, `password`, `netrc_path`) and runs
  :meth:`EarthdataAuth.configure` to validate the credentials.
* Pulls the resolved EDL bearer token from the underlying
  `earthaccess.Auth` handle (`edl._auth.token["access_token"]`,
  populated for every strategy after a successful login).
* Calls `asf_search.ASFSession().auth_with_token(<token>)` to hand
  the token to the SAR download path.

`configure()` is idempotent. `is_authenticated()` is a cheap
predicate (`self._session is not None`). Search calls do not need
auth (`asf_search.geo_search`, `granule_search` and
`ASFProduct.stack` are anonymous endpoints), so the backend builds
an :class:`ASFAuth` in `_initialize` and calls :meth:`configure`
only from `_fetch`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.earthdata.auth import EarthdataAuth, EarthdataCredentials

_REGISTER_URL = "https://urs.earthdata.nasa.gov"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when the ASF backend cannot establish an authenticated session.

    Subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch
    every backend's auth failure with one `except` clause. The
    message names a fix — register an EDL account, set the
    `EARTHDATA_TOKEN` (or `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`)
    environment variables, or add a `urs.earthdata.nasa.gov` entry
    to `~/.netrc`.
    """


class ASFCredentials(BaseModel):
    """Frozen value object holding the EDL credentials for ASF.

    Mirrors :class:`EarthdataCredentials` field-for-field, since ASF
    reuses NASA Earthdata Login. Validation is intentionally
    permissive — the real "do these creds work?" gate is
    :meth:`ASFAuth.configure`, which talks to EDL through
    `earthaccess`.

    Attributes:
        token: Optional EDL **bearer token** — the JSON Web Token
            generated from the EDL profile. Stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers to the
            `EARTHDATA_TOKEN` env var, then username/password, then
            netrc.
        username: EDL account username. `None` means "look at the
            environment / netrc".
        password: Account password, stored as
            :class:`pydantic.SecretStr`. `None` means same as
            `username`.
        netrc_path: Optional path to a `.netrc` file holding a
            `machine urs.earthdata.nasa.gov` entry. `None` falls back
            to `~/.netrc`.

    Examples:
        - All fields optional — rely on env vars / netrc:
            ```python
            >>> from earthlens.asf import ASFCredentials
            >>> creds = ASFCredentials()
            >>> creds.token is None and creds.username is None
            True

            ```
        - SecretStr hides the token in repr:
            ```python
            >>> from earthlens.asf import ASFCredentials
            >>> creds = ASFCredentials(token="EDL.ZZZZZZ")
            >>> "EDL.ZZZZZZ" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    netrc_path: Path | None = None


class ASFAuth(AbstractAuth[ASFCredentials]):
    """Authenticate `asf_search` against NASA Earthdata Login.

    Composes the shipped :class:`EarthdataAuth` to log into EDL and
    mint a bearer token, then hands the token to
    `asf_search.ASFSession().auth_with_token(token)`. The same
    credential ladder as :class:`EarthdataAuth` — explicit token,
    explicit username/password, the `EARTHDATA_TOKEN` /
    `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` env vars, then a
    `urs.earthdata.nasa.gov` entry in `~/.netrc` — applies unchanged.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with ASFAuth(creds) as auth: ...` calls
    `configure()` on enter and the default no-op `close()` on exit.

    Attributes:
        _creds: The :class:`ASFCredentials` value object passed at
            construction. Treated as write-once.
        _edl: The composed :class:`EarthdataAuth` instance, kept so
            `configure()` can be re-checked / re-used. `None` until
            :meth:`configure` runs.
        _session: The `asf_search.ASFSession` bound to the EDL bearer
            token. `None` until :meth:`configure` runs.

    Examples:
        - Build, configure, inspect — marked `# doctest: +SKIP`
          because it makes a real EDL call:

            ```python
            >>> from earthlens.asf import ASFAuth, ASFCredentials
            >>> auth = ASFAuth(ASFCredentials())  # doctest: +SKIP
            >>> auth.configure()  # doctest: +SKIP
            >>> auth.is_authenticated()  # doctest: +SKIP
            True

            ```
    """

    def __init__(self, credentials: ASFCredentials) -> None:
        """Store credentials; does not authenticate.

        Construction is side-effect-free — the user (or the
        backend's `_fetch`) must call :meth:`configure` (or use the
        context-manager form) to talk to EDL.

        Args:
            credentials: The :class:`ASFCredentials` value object.
        """
        super().__init__(credentials)
        self._edl: EarthdataAuth | None = None
        self._session = None

    def configure(self) -> None:
        """Authenticate against EDL and build an `ASFSession`.

        Idempotent — short-circuits when :meth:`is_authenticated`
        already returns `True`. On the first call:

        1. builds an :class:`EarthdataCredentials` from `self._creds`
           and runs :meth:`EarthdataAuth.configure` to log into EDL;
        2. reads the bearer token from the resolved
           `earthaccess.Auth` handle
           (`edl._auth.token["access_token"]`, populated for the
           token / env-creds / netrc strategies alike);
        3. constructs `asf_search.ASFSession().auth_with_token(token)`
           and caches it on `self._session`.

        Raises:
            AuthenticationError: When the EDL login succeeds but the
                resolved auth handle carries no usable token (an
                edge case the underlying `EarthdataAuth` already
                guards against, re-raised here for the ASF backend's
                error message). The base `EarthdataAuth.configure`
                raises its own :class:`AuthenticationError` for the
                no-credentials / bad-credentials cases.
            ImportError: When `asf_search` is not installed; the
                message names the `earthlens[asf]` extra.
        """
        if self.is_authenticated():
            return

        try:
            import asf_search
        except ImportError as exc:
            raise ImportError(
                "the ASF backend needs `asf_search`, which is not installed. "
                "Install the extra with `pip install earthlens[asf]`."
            ) from exc

        edl = EarthdataAuth(
            EarthdataCredentials(
                token=self._creds.token,
                username=self._creds.username,
                password=self._creds.password,
                netrc_path=self._creds.netrc_path,
            )
        )
        edl.configure()
        token = self._extract_token(edl)
        if not token:
            raise AuthenticationError(
                "Earthdata Login succeeded but no bearer token could be "
                "resolved from the auth handle. Set EARTHDATA_TOKEN, set "
                "EARTHDATA_USERNAME + EARTHDATA_PASSWORD, add a "
                f"'machine urs.earthdata.nasa.gov' entry to ~/.netrc, or "
                f"register a free account at {_REGISTER_URL}."
            )

        self._edl = edl
        self._session = asf_search.ASFSession().auth_with_token(token)

    @staticmethod
    def _extract_token(edl: EarthdataAuth) -> str | None:
        """Pull the EDL bearer token from a configured `EarthdataAuth`.

        The `earthaccess.Auth.token` attribute is a dict shaped
        `{"access_token": "<jwt>", ...}` after every login strategy
        (token / username-password / netrc). The dict is populated
        on the auth handle before `EarthdataAuth.configure` returns,
        so this is a synchronous read with no side effects.

        Args:
            edl: A configured :class:`EarthdataAuth` instance.

        Returns:
            The bearer token string, or `None` when the handle does
            not carry one (the configured callers re-raise this as a
            user-facing :class:`AuthenticationError`).
        """
        handle = getattr(edl, "_auth", None)
        token = getattr(handle, "token", None) if handle is not None else None
        if isinstance(token, dict):
            return token.get("access_token")
        return None

    def is_authenticated(self) -> bool:
        """Return `True` once `configure()` has built an ASFSession.

        Cheap predicate — does not call `earthaccess` or the
        network. A return of `True` means an EDL login succeeded
        during the current process and the `ASFSession` is ready.

        Returns:
            bool: `True` after a successful :meth:`configure`,
                `False` before.

        Examples:
            - A freshly constructed auth has not yet authenticated:
                ```python
                >>> from earthlens.asf import ASFAuth, ASFCredentials
                >>> auth = ASFAuth(ASFCredentials(username="u", password="p"))
                >>> auth.is_authenticated()
                False

                ```
        """
        return self._session is not None

    def session(self):
        """Return the bound `ASFSession`, configuring on first use.

        The download path (`asf_search.ASFSearchResults.download`)
        requires an authenticated session; search does not. The
        backend's `_fetch` calls this method, `_search` does not.

        Returns:
            asf_search.ASFSession: The session bound to the EDL
                bearer token.

        Raises:
            AuthenticationError: Propagated from :meth:`configure`
                on missing / bad credentials.
        """
        self.configure()
        return self._session
