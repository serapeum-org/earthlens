"""Optional Personal Access Token resolution for the USGS Water backend.

Hosts :class:`UsgsWaterAuth`, an :class:`earthlens.base.AbstractAuth`
subclass that resolves an **optional** USGS Personal Access Token (PAT)
from, in priority order, an explicit `api_token=` argument or the
`API_USGS_PAT` environment variable. Unlike the OpenAQ backend (whose
key is mandatory), USGS NWIS / Water Data works **anonymously** — a
token only lifts the rate limit. So this auth never raises on a missing
token: when none is found, the backend runs anonymously (the modern
`api.waterdata.usgs.gov` endpoint then rate-limits aggressively, which
the backend handles by falling back to the legacy endpoint).

The shape:

* :class:`UsgsWaterCredentials` is a frozen pydantic value object
  carrying the optional token as a :class:`pydantic.SecretStr`.
* :class:`UsgsWaterAuth` binds those credentials and, in
  :meth:`UsgsWaterAuth.configure`, resolves the token (explicit then
  env var) and — when one is present — exports it back to the
  `API_USGS_PAT` environment variable, which is the channel the
  `dataretrieval` SDK reads. `configure()` is a no-op when anonymous.
* :meth:`UsgsWaterAuth.is_authenticated` reports whether a token was
  resolved (i.e. whether the request is token-backed or anonymous);
  both are valid operating modes.

The resolved token is read back via the :attr:`UsgsWaterAuth.token`
property (which returns `None` when anonymous).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth

#: Environment variable the `dataretrieval` SDK reads for the PAT, and
#: where a registered token can be obtained.
_TOKEN_ENV_VAR = "API_USGS_PAT"
_TOKEN_URL = "https://api.waterdata.usgs.gov/docs/ogcapi/keys/"


class UsgsWaterCredentials(BaseModel):
    """Frozen value object holding the optional USGS PAT.

    The token is optional at construction time: `None` means "resolve
    from the `API_USGS_PAT` environment variable at
    :meth:`UsgsWaterAuth.configure` time, and if that is also unset,
    run anonymously".

    Attributes:
        api_token: The USGS Personal Access Token, stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers resolution to the
            environment variable (and then to anonymous access).

    Examples:
        - Build from an explicit token; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.usgs_water import UsgsWaterCredentials
            >>> creds = UsgsWaterCredentials(api_token="topsecret")
            >>> creds.api_token.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - The token is optional — anonymous access is the default:
            ```python
            >>> from earthlens.usgs_water import UsgsWaterCredentials
            >>> UsgsWaterCredentials().api_token is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_token: SecretStr | None = None


class UsgsWaterAuth(AbstractAuth[UsgsWaterCredentials]):
    """Resolve and hold the optional USGS Personal Access Token.

    Implements the :class:`earthlens.base.AbstractAuth` contract for an
    **optional**-secret backend. Construction does not touch the
    environment; :meth:`configure` performs the resolution and is
    idempotent. After `configure()`, the token (or `None` for
    anonymous) is available via the :attr:`token` property, and — when
    a token was resolved — it is exported to the `API_USGS_PAT`
    environment variable so the `dataretrieval` SDK picks it up.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with UsgsWaterAuth(creds) as auth: ...`
    calls `configure()` on enter and the default no-op `close()` on
    exit.

    Attributes:
        _creds: The :class:`UsgsWaterCredentials` passed at
            construction.

    Examples:
        - Resolve an explicit token:
            ```python
            >>> from earthlens.usgs_water import UsgsWaterAuth, UsgsWaterCredentials
            >>> auth = UsgsWaterAuth(UsgsWaterCredentials(api_token="k"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.token
            'k'

            ```
        - With no token, `configure()` succeeds and stays anonymous:
            ```python
            >>> import os
            >>> from earthlens.usgs_water import UsgsWaterAuth, UsgsWaterCredentials
            >>> os.environ.pop("API_USGS_PAT", None) and None
            >>> auth = UsgsWaterAuth(UsgsWaterCredentials())
            >>> auth.configure()
            >>> auth.is_authenticated()
            False
            >>> auth.token is None
            True

            ```
    """

    def __init__(self, credentials: UsgsWaterCredentials) -> None:
        """Store credentials; does not resolve the token yet.

        Args:
            credentials: The :class:`UsgsWaterCredentials` value object
                carrying the optional token.
        """
        super().__init__(credentials)
        self._configured = False
        self._token: str | None = None

    def configure(self) -> None:
        """Resolve the optional token and export it to the environment.

        Idempotent — short-circuits when :meth:`is_authenticated`
        already returns `True`. Resolves the token in this order: the
        explicit `api_token` on the credentials, then the
        `API_USGS_PAT` environment variable. When a token is found it
        is written back to `API_USGS_PAT` (the channel `dataretrieval`
        reads) and :meth:`is_authenticated` flips to `True`. When none
        is found this is a **no-op** — the request runs anonymously
        (rate-limited) and no error is raised.
        """
        if self.is_authenticated():
            return
        token = (
            self._creds.api_token.get_secret_value()
            if self._creds.api_token is not None
            else os.environ.get(_TOKEN_ENV_VAR)
        )
        if token:
            os.environ[_TOKEN_ENV_VAR] = token
            self._token = token
            self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` when a token was resolved (token-backed mode).

        Cheap predicate — does not call the network. `False` is a
        legitimate, non-error state meaning "anonymous access".

        Returns:
            bool: `True` after :meth:`configure` resolved a token,
                `False` when anonymous (no token).
        """
        return self._configured

    @property
    def token(self) -> str | None:
        """The resolved PAT, or `None` for anonymous access.

        Valid after :meth:`configure`; returns `None` both before
        configuration and when no token was available (anonymous).

        Returns:
            str | None: The USGS PAT string, or `None` when anonymous.
        """
        return self._token
