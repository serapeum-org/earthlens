"""Credentials and resolution for the JAXA backend's credentialed branches.

JAXA's archive is reached through three protocols, two of which need
credentials:

* `protocol: jaxa-earth` — open STAC + COG access through the official
  `jaxa.earth` API. Authless.
* `protocol: gportal` — G-Portal mission archive accessed via SFTP through
  the community `gportal` SDK. Needs a free G-Portal account.
* `protocol: ptree` — near-real-time Himawari-8/9 HSD granules on
  `ftp.ptree.jaxa.jp` (30-day rolling archive). Needs a free P-Tree
  account, distinct from G-Portal registration.

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
* `JaxaAuth(creds, protocol="ptree")` does the same for P-Tree, reading
  `ptree_username` / `ptree_password` off the credentials or falling back
  to `$JAXA_PTREE_USERNAME` / `$JAXA_PTREE_PASSWORD`. The two credential
  pairs never share values — P-Tree registration is separate from G-Portal.

The resolved username / password are exposed as :attr:`JaxaAuth.username`
and :attr:`JaxaAuth.password` (the latter is a :class:`pydantic.SecretStr`)
so each credentialed branch can pass them straight to its transport client
as kwargs — no module-level mutation of the SDK is required.
"""

from __future__ import annotations

import os
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user registers for a free G-Portal account.
_GPORTAL_REGISTER_URL = "https://gportal.jaxa.jp/gpr/user/regist1"

#: Where a user registers for a free P-Tree account (separate from G-Portal).
_PTREE_REGISTER_URL = "https://www.eorc.jaxa.jp/ptree/registration_top.html"

#: Kept as a back-compat alias for external callers that referenced the
#: original single-URL constant when only G-Portal was credentialed.
_REGISTER_URL = _GPORTAL_REGISTER_URL

#: The three protocols `JaxaAuth` accepts at construction.
JaxaProtocol = Literal["jaxa-earth", "gportal", "ptree"]


class AuthenticationError(_BaseAuthenticationError):
    """Raised when a credentialed JAXA branch has no usable credentials.

    Raised by the `gportal` and `ptree` branches; the `jaxa-earth` branch is
    authless. The message names the exact fix for the missing pair: for
    `gportal`, pass `gportal_username=` / `gportal_password=` to `JAXA(...)`
    or set `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`; for `ptree`, pass
    `ptree_username=` / `ptree_password=` or set `$JAXA_PTREE_USERNAME` /
    `$JAXA_PTREE_PASSWORD`.
    """


class JaxaCredentials(BaseModel):
    """Frozen value object holding the credentialed-branch secrets.

    All fields are optional at construction time: `None` means "resolve
    from the corresponding environment variable at :meth:`JaxaAuth.configure`
    time". The real "are credentials available?" gate is
    :meth:`JaxaAuth.configure`, not this model.

    G-Portal and P-Tree credentials are stored side-by-side because a
    single :class:`JAXA` instance is bound to one protocol per call — the
    unused pair is simply ignored.

    Attributes:
        gportal_username: G-Portal username. `None` defers to
            `$GPORTAL_USERNAME` at configure time.
        gportal_password: G-Portal password, stored as a `SecretStr` so it
            is never echoed in `repr` or logs. `None` defers to
            `$GPORTAL_PASSWORD`.
        ptree_username: P-Tree username (the registered email). `None`
            defers to `$JAXA_PTREE_USERNAME`.
        ptree_password: P-Tree password, stored as a `SecretStr`. `None`
            defers to `$JAXA_PTREE_PASSWORD`.

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
        - The same protection applies to the P-Tree password:
            ```python
            >>> from earthlens.jaxa import JaxaCredentials
            >>> creds = JaxaCredentials(
            ...     ptree_username="alice@example.org",
            ...     ptree_password="hunter2",
            ... )
            >>> "hunter2" in repr(creds)
            False

            ```
        - Every field is optional — rely on the environment instead:
            ```python
            >>> from earthlens.jaxa import JaxaCredentials
            >>> JaxaCredentials().ptree_username is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    gportal_username: str | None = None
    gportal_password: SecretStr | None = None
    ptree_username: str | None = None
    ptree_password: SecretStr | None = None


class _ProtocolSpec(NamedTuple):
    """Per-protocol resolver spec consumed by :meth:`JaxaAuth.configure`.

    Named-tuple access (`spec.cred_user`) keeps `configure()` readable
    and gives static type-checkers a chance to catch a typo when a new
    credentialed protocol is added.
    """

    cred_user: str
    cred_pass: str
    env_user: str
    env_pass: str
    register_url: str
    human_name: str


#: Two credentialed branches share one resolver path via this map;
#: adding a fourth credentialed protocol only needs a new row.
_PROTOCOL_SPECS: dict[JaxaProtocol, _ProtocolSpec] = {
    "gportal": _ProtocolSpec(
        cred_user="gportal_username",
        cred_pass="gportal_password",  # nosec B106 - credential field-name, not a value
        env_user="GPORTAL_USERNAME",
        env_pass="GPORTAL_PASSWORD",
        register_url=_GPORTAL_REGISTER_URL,
        human_name="G-Portal",
    ),
    "ptree": _ProtocolSpec(
        cred_user="ptree_username",
        cred_pass="ptree_password",  # nosec B106 - credential field-name, not a value
        env_user="JAXA_PTREE_USERNAME",
        env_pass="JAXA_PTREE_PASSWORD",
        register_url=_PTREE_REGISTER_URL,
        human_name="P-Tree",
    ),
}


class JaxaAuth(AbstractAuth[JaxaCredentials]):
    """Resolve credentials for the bound JAXA protocol.

    The class is optional-credentials: the protocol is **bound at
    construction** so the parent contract's no-arg `configure()` /
    `is_authenticated()` (the methods `AbstractDataSource.authenticate()`
    calls) act on the right side. `JaxaAuth(creds, protocol="jaxa-earth")`
    makes `configure()` a no-op; `JaxaAuth(creds, protocol="gportal")` or
    `JaxaAuth(creds, protocol="ptree")` resolves the matching username +
    password from explicit credentials or the protocol's env-var pair,
    raising :class:`AuthenticationError` on miss.

    After a successful `configure()` on a credentialed protocol, the
    resolved values are available via :attr:`username` and
    :attr:`password` so the branch can pass them straight into its
    transport client (`gportal.download(username=, password=)`,
    `ftplib.FTP.login(...)`, `paramiko.Transport.connect(...)`) instead of
    mutating any SDK's module-level globals.

    Attributes:
        _creds: The :class:`JaxaCredentials` passed at construction.
        _protocol: The bound protocol — one of `"jaxa-earth"`, `"gportal"`
            or `"ptree"`.

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
                the optional G-Portal and P-Tree username / password pairs.
            protocol: The protocol this auth instance targets. The
                parent's no-arg :meth:`configure` dispatches on this so
                `AbstractDataSource.authenticate()` fails fast for missing
                credentials on either credentialed branch.

        Raises:
            ValueError: When `protocol` is not one of the three supported
                values.
        """
        if protocol not in ("jaxa-earth", "gportal", "ptree"):
            raise ValueError(
                "protocol must be one of 'jaxa-earth', 'gportal', 'ptree'; "
                f"got {protocol!r}."
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
        """The resolved username, or `None` until `configure()` runs."""
        return self._username

    @property
    def password(self) -> SecretStr | None:
        """The resolved password (still wrapped in `SecretStr`).

        Returns `None` until :meth:`configure` runs. Callers that need
        the raw string call `.get_secret_value()` themselves at the call
        site — the wrapper stays on `JaxaAuth` so it never lands in a
        `repr` or a log line.
        """
        return self._password

    def configure(self) -> None:
        """Resolve credentials for the bound protocol.

        For `"jaxa-earth"` the call is a no-op (the JAXA Earth API needs
        no auth). For `"gportal"` and `"ptree"` the call reads the
        explicit credentials (preferred) or the protocol's environment
        variables (`$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD` and
        `$JAXA_PTREE_USERNAME` / `$JAXA_PTREE_PASSWORD` respectively) as
        fallback, and caches them on the instance for the branch to read
        through :attr:`username` / :attr:`password`. Idempotent — a
        second call after `is_authenticated()` returns `True`
        short-circuits.

        Raises:
            AuthenticationError: When the protocol is credentialed and
                neither the explicit credentials nor the environment
                variables supply a usable username + password pair. The
                message names the env vars and the free-registration URL
                for the protocol that failed to resolve.

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
            - The `ptree` protocol reads its own credential pair:
                ```python
                >>> from pydantic import SecretStr
                >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
                >>> creds = JaxaCredentials(
                ...     ptree_username="alice@example.org",
                ...     ptree_password=SecretStr("hunter2"),
                ... )
                >>> auth = JaxaAuth(creds, protocol="ptree")
                >>> auth.configure()
                >>> auth.username
                'alice@example.org'

                ```
        """
        if self._configured:
            return
        if self._protocol == "jaxa-earth":
            self._configured = True
            return
        spec = _PROTOCOL_SPECS[self._protocol]
        cred_username = getattr(self._creds, spec.cred_user)
        cred_password: SecretStr | None = getattr(self._creds, spec.cred_pass)
        username = cred_username or os.environ.get(spec.env_user)
        password_raw = (
            cred_password.get_secret_value()
            if cred_password is not None
            else os.environ.get(spec.env_pass)
        )
        if not username or not password_raw:
            raise AuthenticationError(
                f"no {spec.human_name} credentials available: pass "
                f"{spec.cred_user}= and {spec.cred_pass}= to JAXA(...), "
                f"or set both {spec.env_user} and {spec.env_pass} "
                f"environment variables. Register a free account at "
                f"{spec.register_url}."
            )
        self._username = username
        self._password = SecretStr(password_raw)
        self._configured = True
