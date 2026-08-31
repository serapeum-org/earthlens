"""Credentials and `MAP_KEY` resolution for the NASA FIRMS backend.

Hosts :class:`FirmsAuth`, an :class:`earthlens.base.SingleSecretAuth`
subclass that resolves a single FIRMS **`MAP_KEY`** from, in priority
order, an explicit `api_key=` argument or the `FIRMS_MAP_KEY`
environment variable. FIRMS requires a (free) key on every request;
there is no username/password and no saved config-file dance, so this is
the same single-secret shape as :class:`earthlens.openaq.OpenaqAuth` —
mirrored on the `CmemsAuth` resolution chain but without the toolbox
login.

Unlike OpenAQ (which attaches its key as an `X-API-Key` header via a
dedicated client), FIRMS sends the `MAP_KEY` as a **path segment** in
the request URL, so there is no separate client module: the resolved key
is read back via the :attr:`FirmsAuth.api_key` property and dropped into
the URL by :class:`earthlens.firms.FIRMS` directly.

The shape:

* :class:`FirmsCredentials` is a frozen pydantic value object carrying
  the optional key as a :class:`pydantic.SecretStr`.
* :class:`FirmsAuth` binds those credentials and lets the shared
  :meth:`earthlens.base.SingleSecretAuth.configure` resolve the key —
  explicit key first, then the `FIRMS_MAP_KEY` env var, then a clear
  :class:`AuthenticationError` naming the free-registration URL (never
  an interactive prompt).
* `configure()` is idempotent — a second call after
  :meth:`FirmsAuth.is_authenticated` returns `True` short-circuits, so
  it is safe to call from long-lived workers.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError
from earthlens.base.auth import SingleSecretAuth

#: Where a user requests a free FIRMS MAP_KEY.
_MAP_KEY_URL = "https://firms.modaps.eosdis.nasa.gov/api/map_key/"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable FIRMS `MAP_KEY` can be resolved.

    Carries a message that names a fix: pass `api_key=` to
    `EarthLens(...).authenticate()`, set the `FIRMS_MAP_KEY` environment
    variable, or request a free key at
    `firms.modaps.eosdis.nasa.gov/api/map_key/`. A subclass of the
    cross-backend :class:`earthlens.base.AuthenticationError` so callers
    can catch every backend's auth failure with one `except` clause.
    """


class FirmsCredentials(BaseModel):
    """Frozen value object holding the FIRMS `MAP_KEY`.

    The key is optional at construction time: `None` means "resolve from
    the `FIRMS_MAP_KEY` environment variable at
    :meth:`FirmsAuth.configure` time". The real "is there a usable key?"
    gate is :meth:`FirmsAuth.configure`, not this model.

    Attributes:
        api_key: The FIRMS `MAP_KEY`, stored as a
            :class:`pydantic.SecretStr` so it is never echoed by
            `repr(creds)` or in logs. `None` defers resolution to the
            environment variable.

    Examples:
        - Build from an explicit key; the secret is hidden in `repr`:
            ```python
            >>> from earthlens.firms import FirmsCredentials
            >>> creds = FirmsCredentials(api_key="topsecret")
            >>> creds.api_key.get_secret_value()
            'topsecret'
            >>> "topsecret" in repr(creds)
            False

            ```
        - The key is optional — rely on the environment instead:
            ```python
            >>> from earthlens.firms import FirmsCredentials
            >>> FirmsCredentials().api_key is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None


class FirmsAuth(SingleSecretAuth[FirmsCredentials]):
    """Resolve and hold the FIRMS `MAP_KEY`.

    Implements the :class:`earthlens.base.SingleSecretAuth` contract for
    a single-secret backend: it declares its env var and provider name
    and supplies `_explicit_credential` / `_connect`, while the inherited
    :meth:`configure` performs the resolution and is idempotent. After a
    successful `configure()`, the key is available via the
    :attr:`api_key` property for the backend to drop into the request
    URL.

    The class is a context manager (inherited from
    :class:`AbstractAuth`): `with FirmsAuth(creds) as auth: ...` calls
    `configure()` on enter and the default no-op `close()` on exit —
    there is no per-instance resource to release.

    Attributes:
        _creds: The :class:`FirmsCredentials` passed at construction.

    Examples:
        - Resolve an explicit key:
            ```python
            >>> from earthlens.firms import FirmsAuth, FirmsCredentials
            >>> auth = FirmsAuth(FirmsCredentials(api_key="k"))
            >>> auth.is_authenticated()
            False
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.api_key
            'k'

            ```
    """

    ENV_VARS = ("FIRMS_MAP_KEY",)
    PROVIDER = "FIRMS"
    CREDENTIAL_ARG = "api_key"
    CREDENTIAL_HINT = f"Request a free key at {_MAP_KEY_URL}."
    AUTH_ERROR = AuthenticationError

    #: The resolved MAP_KEY, set by `_connect`; `None` until `configure` runs.
    _key: str | None = None

    def _explicit_credential(self) -> str | None:
        """Return the explicit `api_key` off the credentials, if any."""
        api_key = self._creds.api_key
        return api_key.get_secret_value() if api_key is not None else None

    def _connect(self, credential: str) -> None:
        """Store the resolved MAP_KEY for the `api_key` property to read back."""
        self._key = credential

    @property
    def api_key(self) -> str:
        """The resolved `MAP_KEY`; valid only after :meth:`configure`.

        Returns:
            str: The FIRMS `MAP_KEY` string.

        Raises:
            AuthenticationError: When read before :meth:`configure` has
                resolved a key.
        """
        if self._key is None:
            raise AuthenticationError(
                "FirmsAuth.configure() has not run yet; no MAP_KEY resolved."
            )
        return self._key
