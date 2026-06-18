"""Credentials and resolution for the JAXA backend's G-Portal SFTP branch.

JAXA's archive is reached through two protocols, only one of which needs
credentials:

* `protocol: jaxa-earth` — open STAC + COG access through the official
  `jaxa.earth` API. Authless.
* `protocol: gportal` — G-Portal mission archive accessed via SFTP through
  the community `gportal` SDK. Needs a free G-Portal account.

`JaxaAuth` mirrors `OpenaqAuth` (optional secret resolved from explicit kwargs
or environment variables) but routes the resolution per-protocol: a
`configure("jaxa-earth")` call is a no-op, and a `configure("gportal")` call
sets the SDK's module-level `gportal.username` / `gportal.password` from the
explicit credentials or from `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`. Both
env vars must be set by earthlens (verified against the installed `gportal`
0.4.0: the SDK does **not** auto-read either env var, despite the casual
claim in older docs).

The resolved values are not re-exposed publicly — they live on the SDK module
itself once `configure("gportal")` runs.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Where a user registers for a free G-Portal account.
_REGISTER_URL = "https://gportal.jaxa.jp/gpr/user/regist1"

#: The two protocols `JaxaAuth.configure` accepts.
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
    from the corresponding environment variable at `configure("gportal")`
    time". The real "are credentials available?" gate is
    `JaxaAuth.configure("gportal")`, not this model.

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
    """Resolve and apply G-Portal credentials for the `gportal` protocol.

    The class is optional-credentials: a `configure("jaxa-earth")` call
    returns immediately (the JAXA Earth API needs no auth); a
    `configure("gportal")` call resolves the username and password and
    assigns them to the `gportal` SDK's module-level `gportal.username`
    and `gportal.password` attributes. The verified `gportal` 0.4.0 source
    shows these are plain attributes (default `None`) — assigning to them
    is the supported auth model.

    `configure()` is idempotent per protocol: a second call after a
    successful first call short-circuits via `is_authenticated()`.

    Attributes:
        _creds: The :class:`JaxaCredentials` passed at construction.

    Examples:
        - The `jaxa-earth` protocol never needs credentials:
            ```python
            >>> from earthlens.jaxa import JaxaAuth, JaxaCredentials
            >>> auth = JaxaAuth(JaxaCredentials())
            >>> auth.configure("jaxa-earth")
            >>> auth.is_authenticated()
            True

            ```
    """

    def __init__(self, credentials: JaxaCredentials) -> None:
        """Store credentials; does not resolve them yet.

        Args:
            credentials: The :class:`JaxaCredentials` value object carrying
                the optional G-Portal username and password.
        """
        super().__init__(credentials)
        self._configured_protocols: set[str] = set()

    def configure(self, protocol: JaxaProtocol = "jaxa-earth") -> None:
        """Resolve credentials for `protocol` so downstream calls authenticate.

        For `"jaxa-earth"` the call is a no-op. For `"gportal"` the call
        reads the explicit credentials (preferred) or `$GPORTAL_USERNAME`
        / `$GPORTAL_PASSWORD` (fallback) and assigns them to
        `gportal.username` / `gportal.password`. Subsequent
        `gportal.download(...)` calls will use those values.

        Idempotent per protocol — a second call for the same protocol
        after a successful first call short-circuits.

        Args:
            protocol: Either `"jaxa-earth"` (no-op) or `"gportal"`
                (resolve and apply G-Portal credentials).

        Raises:
            AuthenticationError: When `protocol="gportal"` and neither the
                explicit credentials nor the environment variables supply
                a usable username + password pair. The message names the
                env vars and the free-registration URL.
            ValueError: When `protocol` is not one of the two supported
                values.
            ImportError: When `protocol="gportal"` and the optional
                `gportal` SDK is not installed — surfaced as a friendly
                hint pointing at the `earthlens[jaxa]` extra.
        """
        if protocol not in ("jaxa-earth", "gportal"):
            raise ValueError(
                f"protocol must be 'jaxa-earth' or 'gportal'; got {protocol!r}."
            )
        if self.is_authenticated(protocol):
            return
        if protocol == "jaxa-earth":
            self._configured_protocols.add(protocol)
            return

        username = self._creds.gportal_username or os.environ.get("GPORTAL_USERNAME")
        password = (
            self._creds.gportal_password.get_secret_value()
            if self._creds.gportal_password is not None
            else os.environ.get("GPORTAL_PASSWORD")
        )
        if not username or not password:
            raise AuthenticationError(
                "no G-Portal credentials available: pass gportal_username= and "
                "gportal_password= to JAXA(...), or set both GPORTAL_USERNAME and "
                f"GPORTAL_PASSWORD environment variables. Register a free account "
                f"at {_REGISTER_URL}."
            )
        try:
            import gportal
        except ImportError as exc:
            raise ImportError(
                "the 'gportal' SDK is required for the gportal protocol. "
                "Install it via the [jaxa] extra: pip install 'earthlens[jaxa]'."
            ) from exc
        gportal.username = username
        gportal.password = password
        self._configured_protocols.add(protocol)

    def is_authenticated(self, protocol: JaxaProtocol = "jaxa-earth") -> bool:
        """Return `True` once :meth:`configure` has run for `protocol`.

        Args:
            protocol: The protocol to query. Defaults to `"jaxa-earth"`.

        Returns:
            bool: `True` after a successful :meth:`configure` for the same
                protocol, `False` before.
        """
        return protocol in self._configured_protocols
