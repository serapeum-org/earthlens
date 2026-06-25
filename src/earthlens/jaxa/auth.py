"""Credentials and resolution for the JAXA backend's G-Portal SFTP branch.

JAXA's archive is reached through two protocols, only one of which needs
credentials:

* `protocol: jaxa-earth` — open STAC + COG access through the official
  `jaxa.earth` API. Authless.
* `protocol: gportal` — G-Portal mission archive accessed via SFTP through
  the community `gportal` SDK. Needs a free G-Portal account.

`JaxaAuth` mirrors `OpenaqAuth` (optional secret resolved from explicit kwargs
or environment variables) but binds the target protocol at construction time
so the parent's no-arg `AbstractAuth.configure()` / `is_authenticated()` —
the contract `AbstractDataSource.authenticate()` calls — does the right
thing per protocol:

* `JaxaAuth(creds, protocol="jaxa-earth")` makes `configure()` a no-op (no
  credentials needed).
* `JaxaAuth(creds, protocol="gportal")` makes `configure()` resolve and
  store the username + password from explicit credentials or from
  `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`, raising
  :class:`AuthenticationError` on miss.

The resolved username / password are exposed as :attr:`JaxaAuth.username`
and :attr:`JaxaAuth.password` (the latter is a :class:`pydantic.SecretStr`)
so the gportal branch can pass them straight to ``gportal.download(...)``
as kwargs — no module-level mutation of the SDK is required.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user registers for a free G-Portal account.
_REGISTER_URL = "https://gportal.jaxa.jp/gpr/user/regist1"

#: The two protocols `JaxaAuth` accepts at construction.
JaxaProtocol = Literal["jaxa-earth", "gportal"]


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable G-Portal credentials can be resolved.

    Only the `gportal` protocol can raise this — the `jaxa-earth` branch is
    authless. The message names the fix: pass `gportal_username=` /
    `gportal_password=` to `JAXA(...)`, set `$GPORTAL_USERNAME` /
    `$GPORTAL_PASSWORD`, or register a free account.
    """


class JaxaCredentials(BaseModel):
    """Frozen value object holding the G-Portal credentials.

    Both fields are optional at construction time: `None` means "resolve
    from the corresponding environment variable at :meth:`JaxaAuth.configure`
    time". The real "are credentials available?" gate is
    :meth:`JaxaAuth.configure`, not this model.

    Attributes:
        gportal_username: G-Portal username. `None` defers to
            `$GPORTAL_USERNAME` at configure time.
        gportal_password: G-Portal password, stored as a `SecretStr` so it
            is never echoed in `repr` or logs. `None` defers to
            `$GPORTAL_PASSWORD`.

    Examples:
        - The password is hidden in `repr`:
            ```python
            >>> from earthlens.jaxa import JaxaCredentials
            >>> creds = JaxaCredentials(gportal_username="alice", gportal_password="topsecret")
            >>> "topsecret" in repr(creds)
            False
            >>> creds.gportal_password.get_secret_value()
            'topsecret'

            ```
        - Both fields are optional — rely on the environment instead:
            ```python
            >>> from earthlens.jaxa import JaxaCredentials
            >>> JaxaCredentials().gportal_username is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    gportal_username: str | None = None
    gportal_password: SecretStr | None = None


class JaxaAuth(AbstractAuth[JaxaCredentials]):
    """Resolve and apply G-Portal credentials for the bound protocol.

    The class is optional-credentials: the protocol is **bound at
    construction** so the parent contract's no-arg `configure()` /
    `is_authenticated()` (the methods `AbstractDataSource.authenticate()`
    calls) act on the right side. `JaxaAuth(creds, protocol="jaxa-earth")`
    makes `configure()` a no-op; `JaxaAuth(creds, protocol="gportal")`
    makes it resolve and store the G-Portal username + password from
    explicit credentials or `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`,
    raising :class:`AuthenticationError` on miss.

    After a successful `configure()` on the `gportal` protocol, the
    resolved values are available via :attr:`username` and
    :attr:`password` so the `gportal` branch can pass them straight into
    `gportal.download(username=, password=)` instead of mutating the
    SDK's module-level globals.

    Attributes:
        _creds: The :class:`JaxaCredentials` passed at construction.
        _protocol: The bound protocol — one of `"jaxa-earth"` or
            `"gportal"`.

    Examples:
        - The `jaxa-earth` protocol never needs credentials:
            ```python
            >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
            >>> auth = JaxaAuth(JaxaCredentials(), protocol="jaxa-earth")
            >>> auth.configure()
            >>> auth.is_authenticated()
            True

            ```
    """

    def __init__(
        self,
        credentials: JaxaCredentials,
        protocol: JaxaProtocol = "jaxa-earth",
    ) -> None:
        """Bind the protocol; does not resolve credentials yet.

        Args:
            credentials: The :class:`JaxaCredentials` value object carrying
                the optional G-Portal username and password.
            protocol: The protocol this auth instance targets. The
                parent's no-arg :meth:`configure` dispatches on this so
                `AbstractDataSource.authenticate()` fails fast for missing
                `gportal` credentials.

        Raises:
            ValueError: When `protocol` is not one of the two supported
                values.
        """
        if protocol not in ("jaxa-earth", "gportal"):
            raise ValueError(
                f"protocol must be 'jaxa-earth' or 'gportal'; got {protocol!r}."
            )
        super().__init__(credentials)
        self._protocol: JaxaProtocol = protocol
        self._configured = False
        self._username: str | None = None
        self._password: SecretStr | None = None

    @property
    def protocol(self) -> JaxaProtocol:
        """The protocol bound at construction."""
        return self._protocol

    @property
    def username(self) -> str | None:
        """The resolved G-Portal username, or `None` until `configure()` runs."""
        return self._username

    @property
    def password(self) -> SecretStr | None:
        """The resolved G-Portal password (still wrapped in `SecretStr`).

        Returns `None` until :meth:`configure` runs. Callers that need
        the raw string call `.get_secret_value()` themselves at the call
        site — the wrapper stays on `JaxaAuth` so it never lands in a
        `repr` or a log line.
        """
        return self._password

    def configure(self) -> None:
        """Resolve credentials for the bound protocol.

        For `"jaxa-earth"` the call is a no-op (the JAXA Earth API needs
        no auth). For `"gportal"` the call reads the explicit credentials
        (preferred) or `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`
        (fallback) and caches them on the instance for the branch to read
        through :attr:`username` / :attr:`password`. Idempotent — a
        second call after `is_authenticated()` returns `True`
        short-circuits.

        Raises:
            AuthenticationError: When the protocol is `"gportal"` and
                neither the explicit credentials nor the environment
                variables supply a usable username + password pair. The
                message names the env vars and the free-registration URL.

        Examples:
            - The jaxa-earth protocol's configure is a no-op:
                ```python
                >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
                >>> auth = JaxaAuth(JaxaCredentials(), protocol="jaxa-earth")
                >>> auth.configure()
                >>> auth.is_authenticated()
                True

                ```
            - With gportal credentials supplied explicitly, configure caches them
              for the branch to read via `.username` / `.password`:
                ```python
                >>> from pydantic import SecretStr
                >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
                >>> creds = JaxaCredentials(
                ...     gportal_username="alice",
                ...     gportal_password=SecretStr("topsecret"),
                ... )
                >>> auth = JaxaAuth(creds, protocol="gportal")
                >>> auth.configure()
                >>> auth.username
                'alice'

                ```
        """
        if self._configured:
            return
        if self._protocol == "jaxa-earth":
            self._configured = True
            return
        username = self._creds.gportal_username or os.environ.get("GPORTAL_USERNAME")
        password_raw = (
            self._creds.gportal_password.get_secret_value()
            if self._creds.gportal_password is not None
            else os.environ.get("GPORTAL_PASSWORD")
        )
        if not username or not password_raw:
            raise AuthenticationError(
                "no G-Portal credentials available: pass gportal_username= and "
                "gportal_password= to JAXA(...), or set both GPORTAL_USERNAME and "
                f"GPORTAL_PASSWORD environment variables. Register a free account "
                f"at {_REGISTER_URL}."
            )
        self._username = username
        self._password = SecretStr(password_raw)
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` once :meth:`configure` has run successfully.

        Returns:
            bool: `True` after :meth:`configure` has completed without
                raising; `False` before then.

        Examples:
            - Construction does not authenticate; configure flips the flag:
                ```python
                >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
                >>> auth = JaxaAuth(JaxaCredentials(), protocol="jaxa-earth")
                >>> auth.is_authenticated()
                False
                >>> auth.configure()
                >>> auth.is_authenticated()
                True

                ```
        """
        return self._configured
