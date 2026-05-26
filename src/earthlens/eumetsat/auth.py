"""Credentials and authentication for the EUMETSAT Data Store backend.

Hosts `EumetsatAuth`, an `earthlens.base.AbstractAuth` subclass that
wraps `eumdac.AccessToken`. A single EUMETSAT consumer key / secret pair
mints the OAuth2 bearer token that unlocks every Data Store collection
the backend reaches — MTG-I1 FCI, MSG SEVIRI, Metop (ASCAT / IASI), the
Sentinel-3 / -5P / -6 mirrors, and the OSI SAF / CDR / FDR families. The
bearer is short-lived (~1 h) and `eumdac` refreshes it transparently;
this wrapper re-mints a fresh `AccessToken` only when the cached one has
expired.

The auth wrapper exists so that:

* The backend builds an `EumetsatCredentials` value object up front,
  validates it with pydantic, and passes it through
  `super().__init__(creds)` — consistent with `EarthdataAuth` /
  `CmemsAuth`.
* `configure()` is idempotent — a second call after `is_authenticated()`
  returns `True` short-circuits, so it is safe to call from long-lived
  workers without re-minting on every `download()`.
* `eumdac` auth failures surface as the cross-backend
  `earthlens.base.AuthenticationError`, so a caller can write one
  `except AuthenticationError` clause across CMEMS / Earthdata /
  EUMETSAT.

The credentials-resolution priority used by `configure()` is:

1. `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` environment
   variables.
2. The `consumer_key` / `consumer_secret` passed to the constructor.
3. A `~/.eumdac/credentials` file (a single `key,secret` line, the
   format `eumdac set-credentials` writes; the directory is overridable
   via `EUMDAC_CONFIG_DIR`, or pointed at explicitly with
   `credentials_file`).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

_KEY_MGMT_URL = "https://api.eumetsat.int/api-key/"
_DOCS_URL = (
    "https://user.eumetsat.int/resources/user-guides/"
    "eumetsat-data-access-client-eumdac-guide"
)
#: Matches the single `key,secret` line `eumdac set-credentials` writes to
#: `~/.eumdac/credentials`. Mirrors the regex `eumdac` itself uses.
_CREDENTIALS_LINE = re.compile(r"(\w+),(\w+)")


def _import_eumdac():
    """Import and return the `eumdac` module, or raise a friendly `ImportError`.

    Centralises the lazy SDK import so every entry point that needs
    `eumdac` (`configure`, `datastore`, `datatailor`) surfaces the same
    actionable message naming the `earthlens[eumetsat]` extra.

    Returns:
        module: The imported `eumdac` module.

    Raises:
        ImportError: When the `[eumetsat]` extra (`eumdac`) is not
            installed.
    """
    try:
        import eumdac
    except ImportError as exc:
        raise ImportError(
            "the EUMETSAT backend needs `eumdac`, which is not installed. "
            "Install the extra with `pip install earthlens[eumetsat]`."
        ) from exc
    return eumdac


class AuthenticationError(_BaseAuthenticationError):
    """Raised when `eumdac` cannot mint an OAuth2 token.

    Wraps the underlying `eumdac` / HTTP failure with a message that
    names a fix: register a consumer key / secret at the EUMETSAT API-key
    page, set the `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`
    environment variables, or write a `~/.eumdac/credentials` file.

    A subclass of the cross-backend `earthlens.base.AuthenticationError`
    so callers can catch every backend's auth failure with one `except`
    clause.
    """


class EumetsatCredentials(BaseModel):
    """Frozen value object holding the EUMETSAT consumer key / secret.

    Every field is optional — the auth wrapper resolves the actual pair
    at `configure()` time from the environment, these fields, then the
    `~/.eumdac/credentials` file. Validation is intentionally permissive;
    the real "do these creds work?" gate is
    `EumetsatAuth.configure`, which mints a token against the OAuth2
    endpoint.

    Attributes:
        consumer_key: The EUMETSAT consumer key (the OAuth2 client id).
            `None` means "look at the environment / credentials file".
        consumer_secret: The matching consumer secret, stored as a
            `pydantic.SecretStr` so it is never echoed by `repr(creds)`
            or in logs. `None` means same as `consumer_key`.
        credentials_file: Optional explicit path to a `key,secret`
            credentials file. `None` falls back to
            `EUMDAC_CONFIG_DIR/credentials`, then `~/.eumdac/credentials`.

    Examples:
        - All fields optional — rely on env / credentials file:
            ```python
            >>> from earthlens.eumetsat import EumetsatCredentials
            >>> creds = EumetsatCredentials()
            >>> creds.consumer_key is None and creds.consumer_secret is None
            True

            ```
        - SecretStr hides the secret in repr:
            ```python
            >>> from earthlens.eumetsat import EumetsatCredentials
            >>> creds = EumetsatCredentials(consumer_key="k", consumer_secret="topsecret")
            >>> "topsecret" in repr(creds)
            False

            ```
    """

    model_config = ConfigDict(frozen=True)

    consumer_key: str | None = None
    consumer_secret: SecretStr | None = None
    credentials_file: Path | None = None


class EumetsatAuth(AbstractAuth[EumetsatCredentials]):
    """Authenticate against the EUMETSAT Data Store (OAuth2).

    Wraps `eumdac.AccessToken` in the `earthlens.base.AbstractAuth`
    contract. `configure()` resolves a consumer key / secret pair
    (environment → constructor kwargs → `~/.eumdac/credentials`) and
    mints an `eumdac.AccessToken`, which auto-refreshes the ~1 h bearer
    internally. The `datastore` / `datatailor` helpers build the matching
    `eumdac` clients from the live token.

    The class is a context manager (inherited from `AbstractAuth`):
    `with EumetsatAuth(creds) as auth: ...` calls `configure()` on enter
    and the default no-op `close()` on exit.

    Attributes:
        _creds: The `EumetsatCredentials` passed at construction. Read by
            `configure` to resolve the credential pair. Treated as
            write-once.
        _token: The `eumdac.AccessToken` minted by a successful
            `configure()`, or `None` before it runs.

    Examples:
        - Build, configure, inspect — marked `# doctest: +SKIP` because
          it mints a real OAuth2 token:

            ```python
            >>> from earthlens.eumetsat import EumetsatAuth, EumetsatCredentials
            >>> auth = EumetsatAuth(EumetsatCredentials())  # doctest: +SKIP
            >>> auth.configure()  # doctest: +SKIP
            >>> auth.is_authenticated()  # doctest: +SKIP
            True

            ```
    """

    def __init__(self, credentials: EumetsatCredentials) -> None:
        """Store credentials; does not authenticate.

        Construction is side-effect-free — the user (or the backend's
        `_initialize`) must call `configure` (or use the context-manager
        form) to mint a token.

        Args:
            credentials: The `EumetsatCredentials` value object carrying
                the resolution rules.
        """
        super().__init__(credentials)
        self._token = None

    def _resolve_pair(self) -> tuple[str | None, str | None]:
        """Resolve the consumer key / secret pair.

        Resolution order: the `EUMETSAT_CONSUMER_KEY` /
        `EUMETSAT_CONSUMER_SECRET` environment variables, then the
        constructor kwargs, then a `key,secret` line in the
        credentials file (the explicit `credentials_file`, else
        `EUMDAC_CONFIG_DIR/credentials`, else `~/.eumdac/credentials`).
        The first source that yields both halves wins.

        Returns:
            tuple[str | None, str | None]: The `(consumer_key,
                consumer_secret)` pair; either element is `None` when no
                source supplied it.
        """
        key = os.getenv("EUMETSAT_CONSUMER_KEY") or self._creds.consumer_key
        secret = os.getenv("EUMETSAT_CONSUMER_SECRET")
        if not secret and self._creds.consumer_secret is not None:
            secret = self._creds.consumer_secret.get_secret_value()
        if key and secret:
            return key, secret
        file_key, file_secret = self._read_credentials_file()
        return key or file_key, secret or file_secret

    def _credentials_path(self) -> Path:
        """Return the credentials-file path to read.

        Honours an explicit `credentials_file`, then the
        `EUMDAC_CONFIG_DIR` environment variable `eumdac` itself reads,
        then the default `~/.eumdac/credentials`.

        Returns:
            Path: The resolved credentials-file path (which may not
                exist).
        """
        if self._creds.credentials_file is not None:
            return self._creds.credentials_file
        config_dir = os.getenv("EUMDAC_CONFIG_DIR")
        base = Path(config_dir) if config_dir else Path.home() / ".eumdac"
        return base / "credentials"

    def _read_credentials_file(self) -> tuple[str | None, str | None]:
        """Parse the `key,secret` credentials file, if present.

        Returns:
            tuple[str | None, str | None]: The `(key, secret)` parsed
                from the single `key,secret` line, or `(None, None)` when
                the file is missing or malformed.
        """
        path = self._credentials_path()
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None, None
        match = _CREDENTIALS_LINE.match(content)
        if match is None:
            return None, None
        return match.group(1), match.group(2)

    def configure(self) -> None:
        """Mint the OAuth2 token via `eumdac.AccessToken`.

        Idempotent — short-circuits when `is_authenticated` already
        returns `True`. Resolves the consumer key / secret pair
        (environment → kwargs → credentials file), then constructs an
        `eumdac.AccessToken((key, secret))`. The token object refreshes
        the ~1 h bearer internally; this wrapper re-mints a fresh one
        only after the cached token's `expiration` has passed (see
        `is_authenticated`).

        Raises:
            ImportError: When the `eumdac` SDK is not installed (the
                `[eumetsat]` extra is missing).
            AuthenticationError: When no credential pair resolves, or
                `eumdac` rejects the pair while minting the token.
        """
        if self.is_authenticated():
            return

        eumdac = _import_eumdac()  # lazy — only needed when authenticating

        key, secret = self._resolve_pair()
        if not key or not secret:
            raise AuthenticationError(
                "no EUMETSAT credentials resolved. Set "
                "EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET, pass "
                "consumer_key= / consumer_secret=, or write a "
                f"'key,secret' line to ~/.eumdac/credentials. Register a "
                f"consumer key/secret at {_KEY_MGMT_URL}. See {_DOCS_URL}."
            )

        try:
            self._token = eumdac.AccessToken((key, secret))
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                "EUMETSAT token request failed "
                f"({type(exc).__name__}: {exc}). Check the consumer "
                f"key/secret at {_KEY_MGMT_URL}."
            ) from exc

    def is_authenticated(self) -> bool:
        """Return whether a live, unexpired token is held.

        Cheap predicate — does not call the network. Returns `True` only
        when `configure()` has minted a token whose `expiration`
        (a `datetime`) is still in the future; an expired token reports
        `False` so `configure()` re-mints. A token whose `expiration`
        cannot be read is treated as live (the SDK refreshes it lazily).

        Returns:
            bool: `True` while the held token is valid, `False` before
                `configure` or once the token has expired.
        """
        if self._token is None:
            return False
        try:
            return datetime.now() < self._token.expiration
        except (AttributeError, TypeError, ValueError):
            # The SDK refreshes the bearer lazily, so a token whose
            # `expiration` is missing/unreadable is treated as live rather
            # than forcing a re-mint. Genuinely unexpected errors propagate.
            return True

    def datastore(self):
        """Return an `eumdac.DataStore` bound to the live token.

        Returns:
            eumdac.DataStore: The Data Store client used to resolve
                collections and search products.

        Raises:
            ImportError: When the `[eumetsat]` extra (`eumdac`) is not
                installed.
            AuthenticationError: When `configure()` has not minted a
                token yet.
        """
        eumdac = _import_eumdac()
        if self._token is None:
            raise AuthenticationError(
                "datastore() called before configure(); authenticate "
                "first via configure() or the context-manager form."
            )
        return eumdac.DataStore(self._token)

    def datatailor(self):
        """Return an `eumdac.DataTailor` bound to the live token.

        Used by the deferred Data Tailor (server-side subset / reproject)
        path; the MVP fetches native products and does not call this.

        Returns:
            eumdac.DataTailor: The Data Tailor client.

        Raises:
            ImportError: When the `[eumetsat]` extra (`eumdac`) is not
                installed.
            AuthenticationError: When `configure()` has not minted a
                token yet.
        """
        eumdac = _import_eumdac()
        if self._token is None:
            raise AuthenticationError(
                "datatailor() called before configure(); authenticate "
                "first via configure() or the context-manager form."
            )
        return eumdac.DataTailor(self._token)
