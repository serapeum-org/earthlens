"""Credentials and authentication for the GloH2O MSWEP / MSWX backend.

Hosts :class:`MswepAuth`, an :class:`earthlens.base.AbstractAuth`
subclass that builds an authenticated **Google Drive v3** client for the
share GloH2O grants a user once their access request is approved.

Two properties of the upstream shape the whole module:

* **The data is not public.** MSWEP / MSWX carry no anonymous download.
  A non-commercial user submits a request form per product
  (`gloh2o.org/mswep`, `gloh2o.org/mswx`); on approval GloH2O shares a
  Google-Drive folder with **that person's Google account** and emails
  `rclone` instructions. earthlens automates *their* approved download —
  it never obtains access on their behalf.
* **The identity must be a human account, never a service account.**
  Drive authorises per principal. A service account is a separate
  principal owning its own (empty) Drive, so a folder shared with
  `someone@gmail.com` is invisible to it: `files.list` returns an empty
  result rather than an error, which would silently yield a partial time
  series. Only the folder's **owner** can grant a service account
  access, and a share recipient is a viewer who cannot re-share. So
  :meth:`MswepAuth.configure` **rejects a service-account key up front**
  with an explanation instead of failing opaquely later.

The credential-resolution ladder used by :meth:`MswepAuth.configure` is:

1. An explicit `token_path` — a Google **authorized-user** JSON file
   (`client_id` / `client_secret` / `refresh_token`), as written by
   `google_auth_oauthlib`'s `InstalledAppFlow`.
2. An **`rclone` remote** (`rclone_config` + `rclone_remote`). This is
   the recommended default: GloH2O's approval email tells the user to
   configure `rclone`, so the OAuth token usually already exists on
   their machine and needs no second consent flow.
3. The same two, discovered from the environment
   (`MSWEP_TOKEN_FILE`, `MSWEP_RCLONE_CONFIG` / `RCLONE_CONFIG` /
   `MSWEP_RCLONE_REMOTE`) and `rclone`'s default config locations.

Note:
    `rclone` writes its built-in OAuth client id / secret **nowhere** —
    when the user leaves them blank at `rclone config` time, the remote
    carries a `token` that cannot be refreshed by anything but `rclone`
    itself. :func:`credentials_from_rclone_remote` detects that and
    raises with a pointer to `rclone`'s "making your own client id"
    instructions rather than emitting a credential that dies at the
    first refresh.
"""

from __future__ import annotations

import configparser
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

#: Read-only Drive scope. The backend only ever lists and downloads, so
#: it never requests the read-write `drive` scope `rclone` defaults to.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: Google's OAuth 2.0 token endpoint, used to refresh an authorized-user
#: credential. `rclone` remotes do not record it, so it is supplied here.
TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Where a user requests non-commercial access, per product. Named in the
#: `AuthenticationError` message so a stuck user reads the actual fix.
MSWEP_REQUEST_URL = "https://www.gloh2o.org/mswep/"
MSWX_REQUEST_URL = "https://www.gloh2o.org/mswx/"

#: `rclone`'s instructions for minting a personal OAuth client, required
#: when a remote's `client_id` / `client_secret` are blank.
RCLONE_CLIENT_ID_URL = "https://rclone.org/drive/#making-your-own-client-id"

#: Environment fallbacks for every field on :class:`MswepCredentials`.
FOLDER_ID_ENV = "MSWEP_DRIVE_FOLDER"
TOKEN_FILE_ENV = "MSWEP_TOKEN_FILE"
RCLONE_CONFIG_ENV = "MSWEP_RCLONE_CONFIG"
RCLONE_REMOTE_ENV = "MSWEP_RCLONE_REMOTE"


class AuthenticationError(_BaseAuthenticationError):
    """Raised when no usable Google-Drive credential resolves.

    Wraps every failure mode with a message naming a fix: submit the
    GloH2O request form, point at an `rclone` remote or a `token.json`,
    set `MSWEP_DRIVE_FOLDER`, or swap a service-account key for a user
    credential.

    A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch
    every backend's auth failure with one `except` clause.
    """


def default_rclone_config_paths() -> list[Path]:
    """Return `rclone`'s default config locations, most-likely first.

    `rclone` resolves its config from `RCLONE_CONFIG`, then a
    platform-specific default. Windows uses `%APPDATA%\\rclone`, POSIX
    uses `$XDG_CONFIG_HOME` (falling back to `~/.config`); the legacy
    `~/.rclone.conf` is still honoured on both.

    Returns:
        list[Path]: Candidate paths, in probe order. Existence is not
            checked — the caller filters.
    """
    candidates: list[Path] = []
    from_env = os.getenv("RCLONE_CONFIG")
    if from_env:
        candidates.append(Path(from_env))
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "rclone" / "rclone.conf")
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    candidates.append(base / "rclone" / "rclone.conf")
    candidates.append(Path.home() / ".rclone.conf")
    return candidates


def _import_google_modules() -> tuple[Any, Any]:
    """Import the Drive SDK lazily; re-raise a friendly `ImportError`.

    Returns:
        tuple[Any, Any]: The `google.oauth2.credentials.Credentials`
            class and `googleapiclient.discovery.build` function.

    Raises:
        ImportError: When the `mswep` extra is not installed.
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "the MSWEP / MSWX backend needs the Google Drive SDK, which is "
            "not installed. Install the extra with "
            "`pip install earthlens[mswep]` (google-api-python-client, "
            "google-auth)."
        ) from exc
    return Credentials, build


def reject_service_account(payload: dict[str, Any], source: str) -> None:
    """Raise when a credential file is a service-account key.

    A service account cannot see a folder shared with a human account —
    it is a distinct principal with its own Drive, and `files.list`
    returns an empty result rather than a permission error. Catching it
    at credential-load time converts a silent wrong answer into an
    actionable message.

    Args:
        payload: The parsed JSON credential file.
        source: Path or description of the file, quoted in the message.

    Raises:
        AuthenticationError: When `payload["type"]` is
            `"service_account"`.
    """
    if payload.get("type") != "service_account":
        return
    email = payload.get("client_email", "<unknown>")
    raise AuthenticationError(
        f"{source} is a Google **service-account** key ({email}), which "
        "cannot read the GloH2O share. Drive authorises per principal: "
        "GloH2O shares the folder with your personal Google account, and a "
        "service account is a separate identity whose 'Shared with me' is "
        "empty — listing the folder would silently return nothing rather "
        "than raise. Supply a user credential instead: an rclone remote "
        f"(see {RCLONE_CLIENT_ID_URL}) or an authorized-user token.json."
    )


def credentials_from_token_file(path: Path) -> Any:
    """Build a Drive credential from an authorized-user JSON file.

    Args:
        path: Path to a Google authorized-user file carrying
            `client_id`, `client_secret` and `refresh_token`.

    Returns:
        Any: A `google.oauth2.credentials.Credentials` scoped to
            :data:`DRIVE_SCOPE`.

    Raises:
        AuthenticationError: When the file is missing, is not valid
            JSON, is a service-account key, or lacks a refresh token.
        ImportError: When the `mswep` extra is not installed.
    """
    credentials_cls, _ = _import_google_modules()
    if not path.exists():
        raise AuthenticationError(
            f"MSWEP token file {path} does not exist. Mint one with "
            "google-auth-oauthlib's InstalledAppFlow, or point "
            f"`rclone_config=` / ${RCLONE_CONFIG_ENV} at an rclone remote."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthenticationError(
            f"MSWEP token file {path} could not be read as JSON: "
            f"{type(exc).__name__}: {exc}."
        ) from exc

    reject_service_account(payload, f"MSWEP token file {path}")
    if not payload.get("refresh_token"):
        raise AuthenticationError(
            f"MSWEP token file {path} carries no `refresh_token`, so the "
            "credential expires within the hour and cannot renew. Re-run "
            "the OAuth flow with `access_type='offline'`."
        )
    return credentials_cls.from_authorized_user_info(payload, scopes=[DRIVE_SCOPE])


def credentials_from_rclone_remote(config_path: Path, remote: str) -> Any:
    """Build a Drive credential from an `rclone` Drive remote.

    Reads the `[<remote>]` section of `rclone.conf` and reuses the OAuth
    token `rclone config` already minted, so the user consents once —
    to `rclone`, as GloH2O's approval email instructs — rather than
    twice.

    Args:
        config_path: Path to `rclone.conf`.
        remote: Name of the Drive remote inside it (e.g.
            `"GoogleDrive"`).

    Returns:
        Any: A `google.oauth2.credentials.Credentials` scoped to
            :data:`DRIVE_SCOPE`.

    Raises:
        AuthenticationError: When the config or remote is missing, the
            remote is not a Drive remote, the token is absent or
            unparseable, or the remote carries no OAuth client id /
            secret (so the token could never be refreshed).
        ImportError: When the `mswep` extra is not installed.
    """
    credentials_cls, _ = _import_google_modules()
    if not config_path.exists():
        raise AuthenticationError(
            f"rclone config {config_path} does not exist. Run `rclone config` "
            "to create the Drive remote GloH2O's approval email describes, or "
            f"set ${TOKEN_FILE_ENV} to an authorized-user token.json."
        )

    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise AuthenticationError(
            f"rclone config {config_path} could not be parsed: "
            f"{type(exc).__name__}: {exc}."
        ) from exc

    if not parser.has_section(remote):
        available = ", ".join(parser.sections()) or "<none>"
        raise AuthenticationError(
            f"rclone config {config_path} has no remote named {remote!r}. "
            f"Remotes present: {available}. Set ${RCLONE_REMOTE_ENV} or pass "
            "`rclone_remote=` to pick one."
        )

    section = parser[remote]
    if section.get("type", "") != "drive":
        raise AuthenticationError(
            f"rclone remote {remote!r} in {config_path} has type "
            f"{section.get('type', '<unset>')!r}, not 'drive'. The GloH2O "
            "share is a Google Drive folder — point at the Drive remote."
        )

    raw_token = section.get("token", "")
    if not raw_token:
        raise AuthenticationError(
            f"rclone remote {remote!r} in {config_path} carries no `token`. "
            "Re-run `rclone config` and complete the browser consent step."
        )
    try:
        token = json.loads(raw_token)
    except ValueError as exc:
        raise AuthenticationError(
            f"rclone remote {remote!r} in {config_path} has an unparseable "
            f"`token` value: {exc}."
        ) from exc

    client_id = section.get("client_id", "")
    client_secret = section.get("client_secret", "")
    if not client_id or not client_secret:
        raise AuthenticationError(
            f"rclone remote {remote!r} in {config_path} has no `client_id` / "
            "`client_secret`. rclone falls back to its own built-in OAuth "
            "client, which it never writes to the config, so the token "
            "cannot be refreshed outside rclone. Create a personal OAuth "
            f"client and re-run `rclone config` — see {RCLONE_CLIENT_ID_URL}."
        )
    if not token.get("refresh_token"):
        raise AuthenticationError(
            f"rclone remote {remote!r} in {config_path} has no "
            "`refresh_token`, so its access token cannot be renewed. Re-run "
            "`rclone config` and complete the browser consent step."
        )

    return credentials_cls(
        token=token.get("access_token"),
        refresh_token=token["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[DRIVE_SCOPE],
    )


class MswepCredentials(BaseModel):
    """Locators for the user's approved GloH2O Drive share.

    Holds *where to find* the credential rather than the credential
    itself, so the object stays cheap, frozen and safe to log — no
    secret ever lands on an attribute. Every field falls back to an
    environment variable, so a configured machine can construct
    `MswepCredentials()` with no arguments.

    Attributes:
        folder_id: Drive id of the shared folder from GloH2O's approval
            email. `None` falls back to `$MSWEP_DRIVE_FOLDER`.
        token_path: Path to a Google authorized-user JSON file. `None`
            falls back to `$MSWEP_TOKEN_FILE`.
        rclone_config: Path to `rclone.conf`. `None` falls back to
            `$MSWEP_RCLONE_CONFIG`, then `$RCLONE_CONFIG`, then
            `rclone`'s platform defaults.
        rclone_remote: Name of the Drive remote inside `rclone.conf`.
            `None` falls back to `$MSWEP_RCLONE_REMOTE`.

    Examples:
        - All fields optional — rely on the environment:
            ```python
            >>> from earthlens.mswep import MswepCredentials
            >>> creds = MswepCredentials()
            >>> creds.folder_id is None and creds.token_path is None
            True

            ```
        - The model is frozen, so a resolved credential cannot drift:
            ```python
            >>> from earthlens.mswep import MswepCredentials
            >>> creds = MswepCredentials(folder_id="1AbC")
            >>> creds.folder_id
            '1AbC'

            ```
    """

    model_config = ConfigDict(frozen=True)

    folder_id: str | None = None
    token_path: Path | None = None
    rclone_config: Path | None = None
    rclone_remote: str | None = None

    def resolved_folder_id(self) -> str | None:
        """Return the folder id, falling back to `$MSWEP_DRIVE_FOLDER`."""
        return self.folder_id or os.getenv(FOLDER_ID_ENV) or None

    def resolved_token_path(self) -> Path | None:
        """Return the token file path, falling back to `$MSWEP_TOKEN_FILE`."""
        if self.token_path is not None:
            return self.token_path
        from_env = os.getenv(TOKEN_FILE_ENV)
        return Path(from_env) if from_env else None

    def resolved_rclone_remote(self) -> str | None:
        """Return the remote name, falling back to `$MSWEP_RCLONE_REMOTE`."""
        return self.rclone_remote or os.getenv(RCLONE_REMOTE_ENV) or None

    def resolved_rclone_config(self) -> Path | None:
        """Return the first `rclone.conf` that exists, or `None`.

        Probes the explicit `rclone_config`, then `$MSWEP_RCLONE_CONFIG`,
        then `rclone`'s own defaults (which include `$RCLONE_CONFIG`).
        An explicitly-supplied path is returned whether or not it
        exists, so a typo surfaces as "does not exist" rather than
        silently falling through to another remote.
        """
        if self.rclone_config is not None:
            return self.rclone_config
        from_env = os.getenv(RCLONE_CONFIG_ENV)
        if from_env:
            return Path(from_env)
        for candidate in default_rclone_config_paths():
            if candidate.exists():
                return candidate
        return None


class MswepAuth(AbstractAuth[MswepCredentials]):
    """Build an authenticated Google Drive v3 client for the GloH2O share.

    Wraps the credential ladder documented at module level in the
    :class:`earthlens.base.AbstractAuth` contract. :meth:`configure` is
    idempotent, so it is safe to call on every `download()`.

    The Drive client is **injectable**: passing `service=` skips
    credential resolution entirely, which is how the unit suite drives
    the backend against a fake `files()` API with no network and no
    Google SDK.

    Attributes:
        _creds: The :class:`MswepCredentials` passed at construction.
        _service: The built Drive v3 client, or `None` before
            :meth:`configure` runs.
        _folder_id: The resolved shared-folder id, or `None` before
            :meth:`configure` runs.

    Examples:
        - An injected service authenticates with no credentials at all:
            ```python
            >>> from earthlens.mswep import MswepAuth, MswepCredentials
            >>> auth = MswepAuth(
            ...     MswepCredentials(folder_id="1AbC"), service=object()
            ... )
            >>> auth.configure()
            >>> auth.is_authenticated()
            True
            >>> auth.folder_id
            '1AbC'

            ```
    """

    def __init__(
        self,
        credentials: MswepCredentials | None = None,
        *,
        service: Any = None,
    ) -> None:
        """Store the locators; does not contact Google.

        Construction is side-effect-free — the caller (or the backend's
        `_initialize`) must call :meth:`configure`, or use the
        context-manager form.

        Args:
            credentials: Where to find the user's Drive credential.
                Defaults to a fresh :class:`MswepCredentials`, which
                resolves everything from the environment.
            service: Optional pre-built Drive client. When given,
                :meth:`configure` uses it verbatim and resolves no
                credentials — the seam the unit suite drives.
        """
        super().__init__(credentials or MswepCredentials())
        self._service: Any = service
        self._folder_id: str | None = None

    @property
    def service(self) -> Any:
        """Return the Drive v3 client.

        Raises:
            AuthenticationError: When :meth:`configure` has not run.
        """
        if self._service is None:
            raise AuthenticationError(
                "MswepAuth.service accessed before configure(); authenticate "
                "first via configure() or the context-manager form."
            )
        return self._service

    @property
    def folder_id(self) -> str:
        """Return the resolved shared-folder id.

        Raises:
            AuthenticationError: When :meth:`configure` has not run.
        """
        if self._folder_id is None:
            raise AuthenticationError(
                "MswepAuth.folder_id accessed before configure(); "
                "authenticate first via configure()."
            )
        return self._folder_id

    def _resolve_credentials(self) -> Any:
        """Resolve a Drive credential from the first source that answers.

        Returns:
            Any: A `google.oauth2.credentials.Credentials`.

        Raises:
            AuthenticationError: When no source resolves.
        """
        token_path = self._creds.resolved_token_path()
        if token_path is not None:
            return credentials_from_token_file(token_path)

        config_path = self._creds.resolved_rclone_config()
        remote = self._creds.resolved_rclone_remote()
        if config_path is not None and remote is not None:
            return credentials_from_rclone_remote(config_path, remote)

        if config_path is not None and remote is None:
            raise AuthenticationError(
                f"found an rclone config at {config_path} but no remote name. "
                f"Pass `rclone_remote=` or set ${RCLONE_REMOTE_ENV} to the "
                "Drive remote you configured for the GloH2O share."
            )
        raise AuthenticationError(
            "no Google Drive credential resolved for MSWEP / MSWX. This "
            "backend automates *your own* approved GloH2O download, so it "
            "needs two things. (1) Approved access: request it at "
            f"{MSWEP_REQUEST_URL} (MSWEP) and {MSWX_REQUEST_URL} (MSWX) — "
            "GloH2O shares a Drive folder with your Google account. (2) A "
            "user credential for that account: configure an rclone Drive "
            f"remote (then set ${RCLONE_REMOTE_ENV}) or point "
            f"${TOKEN_FILE_ENV} at an authorized-user token.json. A service "
            "account will not work — see the backend documentation."
        )

    def configure(self) -> None:
        """Resolve credentials and build the Drive v3 client.

        Idempotent — short-circuits once :meth:`is_authenticated`
        returns `True`. An injected `service=` is used verbatim, so no
        credential resolution or SDK import happens in tests.

        Raises:
            AuthenticationError: When the shared-folder id is missing,
                no credential source resolves, or a resolved credential
                is a service-account key.
            ImportError: When the `mswep` extra is not installed.
        """
        if self.is_authenticated():
            return

        folder_id = self._creds.resolved_folder_id()
        if not folder_id:
            raise AuthenticationError(
                "no MSWEP / MSWX shared-folder id. GloH2O's approval email "
                "links the Drive folder it shared with you; pass its id as "
                f"`folder_id=` or set ${FOLDER_ID_ENV}. Request access at "
                f"{MSWEP_REQUEST_URL} (MSWEP) / {MSWX_REQUEST_URL} (MSWX)."
            )
        self._folder_id = folder_id

        if self._service is None:
            _, build = _import_google_modules()
            credentials = self._resolve_credentials()
            self._service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )

        self.mark_configured()
