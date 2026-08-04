"""Unit tests for `earthlens.mswep.auth`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from earthlens.mswep import auth as auth_module
from earthlens.mswep.auth import (
    DRIVE_SCOPE,
    FOLDER_ID_ENV,
    RCLONE_CONFIG_ENV,
    RCLONE_REMOTE_ENV,
    TOKEN_FILE_ENV,
    AuthenticationError,
    MswepAuth,
    MswepCredentials,
    credentials_from_file,
    credentials_from_rclone_remote,
    default_rclone_config_paths,
)

pytestmark = [pytest.mark.mswep, pytest.mark.unit]

#: A minimal Drive remote as `rclone config` writes it.
RCLONE_DRIVE_REMOTE = """\
[GoogleDrive]
type = drive
client_id = 1234.apps.googleusercontent.com
client_secret = shhh
scope = drive.readonly
token = {"access_token":"at","refresh_token":"rt","expiry":"2030-01-01T00:00:00Z"}
"""


def _write(path: Path, text: str) -> Path:
    """Write `text` to `path` and return it."""
    path.write_text(text, encoding="utf-8")
    return path


def _authorized_user(**overrides: object) -> dict[str, object]:
    """Build an authorized-user token payload, with optional overrides."""
    payload: dict[str, object] = {
        "type": "authorized_user",
        "client_id": "1234.apps.googleusercontent.com",
        "client_secret": "shhh",
        "refresh_token": "rt",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every MSWEP env var so a configured machine cannot leak in."""
    for var in (
        FOLDER_ID_ENV,
        TOKEN_FILE_ENV,
        RCLONE_CONFIG_ENV,
        RCLONE_REMOTE_ENV,
        "RCLONE_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)


class TestCredentialsFromFile:
    """Loading a credential file, dispatched on its `type`."""

    def test_missing_file_raises(self, tmp_path):
        """A non-existent credential file raises with the path."""
        with pytest.raises(AuthenticationError, match="does not exist"):
            credentials_from_file(tmp_path / "nope.json")

    def test_malformed_json_raises(self, tmp_path):
        """A file that is not JSON raises rather than propagating ValueError."""
        path = _write(tmp_path / "token.json", "{not json")
        with pytest.raises(AuthenticationError, match="could not be read as JSON"):
            credentials_from_file(path)

    def test_service_account_key_dispatches(self, tmp_path, monkeypatch):
        """A service-account key routes to the service-account builder now."""
        path = _write(
            tmp_path / "key.json",
            json.dumps({"type": "service_account", "client_email": "b@x.com"}),
        )
        monkeypatch.setattr(
            auth_module, "credentials_from_service_account", lambda p: "sa-creds"
        )
        assert credentials_from_file(path) == "sa-creds"

    def test_missing_refresh_token_raises(self, tmp_path):
        """An authorized-user token with no refresh token cannot renew."""
        payload = _authorized_user()
        del payload["refresh_token"]
        path = _write(tmp_path / "token.json", json.dumps(payload))
        with pytest.raises(AuthenticationError, match="no `refresh_token`"):
            credentials_from_file(path)

    def test_valid_token_builds_scoped_credentials(self, tmp_path):
        """A well-formed token yields credentials carrying the read-only scope."""
        path = _write(tmp_path / "token.json", json.dumps(_authorized_user()))
        creds = credentials_from_file(path)
        assert creds.refresh_token == "rt"
        assert DRIVE_SCOPE in creds.scopes


class TestServiceAccountAndAdc:
    """The service-account builder and the ADC fallback."""

    def test_service_account_builder_scopes_to_drive(self, tmp_path, monkeypatch):
        """The key is loaded via the SDK, scoped read-only to Drive."""
        import google.oauth2.service_account as sa

        seen = {}

        def _from_file(path, scopes=None):
            seen["path"], seen["scopes"] = path, scopes
            return "sa-creds"

        monkeypatch.setattr(sa.Credentials, "from_service_account_file", _from_file)
        key = _write(tmp_path / "key.json", "{}")
        assert auth_module.credentials_from_service_account(key) == "sa-creds"
        assert seen["scopes"] == [DRIVE_SCOPE]

    def test_adc_returns_credentials_when_present(self, monkeypatch):
        """`try_application_default` returns the resolved credential."""
        import google.auth

        monkeypatch.setattr(
            google.auth, "default", lambda scopes=None: ("adc-creds", "proj")
        )
        assert auth_module.try_application_default() == "adc-creds"

    def test_adc_returns_none_when_absent(self, monkeypatch):
        """No ADC configured yields `None`, so the caller can guide the user."""
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        def _raise(scopes=None):
            raise DefaultCredentialsError("none")

        monkeypatch.setattr(google.auth, "default", _raise)
        assert auth_module.try_application_default() is None


class TestCredentialsFromRcloneRemote:
    """Reusing the OAuth token `rclone config` already minted."""

    def test_valid_remote_builds_scoped_credentials(self, tmp_path):
        """A complete Drive remote yields refreshable, read-only credentials."""
        path = _write(tmp_path / "rclone.conf", RCLONE_DRIVE_REMOTE)
        creds = credentials_from_rclone_remote(path, "GoogleDrive")
        assert creds.refresh_token == "rt"
        assert creds.client_id == "1234.apps.googleusercontent.com"
        assert DRIVE_SCOPE in creds.scopes

    def test_missing_config_raises(self, tmp_path):
        """A non-existent rclone.conf raises with the path."""
        with pytest.raises(AuthenticationError, match="does not exist"):
            credentials_from_rclone_remote(tmp_path / "nope.conf", "GoogleDrive")

    def test_unknown_remote_lists_available(self, tmp_path):
        """An unknown remote name reports the remotes that do exist."""
        path = _write(tmp_path / "rclone.conf", RCLONE_DRIVE_REMOTE)
        with pytest.raises(AuthenticationError, match="GoogleDrive"):
            credentials_from_rclone_remote(path, "Typo")

    def test_non_drive_remote_raises(self, tmp_path):
        """An S3 remote is refused — the GloH2O share is a Drive folder."""
        path = _write(tmp_path / "rclone.conf", "[bucket]\ntype = s3\n")
        with pytest.raises(AuthenticationError, match="not 'drive'"):
            credentials_from_rclone_remote(path, "bucket")

    def test_blank_client_id_raises_with_pointer(self, tmp_path):
        """A remote on rclone's built-in OAuth client cannot refresh, so it raises."""
        text = RCLONE_DRIVE_REMOTE.replace(
            "client_id = 1234.apps.googleusercontent.com\n", ""
        ).replace("client_secret = shhh\n", "")
        path = _write(tmp_path / "rclone.conf", text)
        with pytest.raises(AuthenticationError, match="built-in OAuth"):
            credentials_from_rclone_remote(path, "GoogleDrive")

    def test_missing_token_raises(self, tmp_path):
        """A remote that never completed consent has no token."""
        text = "[GoogleDrive]\ntype = drive\nclient_id = a\nclient_secret = b\n"
        path = _write(tmp_path / "rclone.conf", text)
        with pytest.raises(AuthenticationError, match="no `token`"):
            credentials_from_rclone_remote(path, "GoogleDrive")

    def test_unparseable_token_raises(self, tmp_path):
        """A corrupt token blob raises rather than propagating ValueError."""
        text = RCLONE_DRIVE_REMOTE.replace('token = {"access_token"', "token = {oops")
        path = _write(tmp_path / "rclone.conf", text)
        with pytest.raises(AuthenticationError, match="unparseable"):
            credentials_from_rclone_remote(path, "GoogleDrive")

    def test_token_without_refresh_token_raises(self, tmp_path):
        """An access-token-only remote cannot renew, so it is refused."""
        text = RCLONE_DRIVE_REMOTE.replace('"refresh_token":"rt",', "")
        path = _write(tmp_path / "rclone.conf", text)
        with pytest.raises(AuthenticationError, match="no\n?.*`refresh_token`"):
            credentials_from_rclone_remote(path, "GoogleDrive")


class TestCredentialResolution:
    """`MswepCredentials` env fallbacks."""

    def test_folder_id_falls_back_to_env(self, monkeypatch):
        """An unset folder id reads `$MSWEP_DRIVE_FOLDER`."""
        monkeypatch.setenv(FOLDER_ID_ENV, "1FromEnv")
        assert MswepCredentials().resolved_folder_id() == "1FromEnv"

    def test_explicit_folder_id_wins_over_env(self, monkeypatch):
        """An explicit folder id beats the environment."""
        monkeypatch.setenv(FOLDER_ID_ENV, "1FromEnv")
        assert MswepCredentials(folder_id="1Explicit").resolved_folder_id() == (
            "1Explicit"
        )

    def test_token_path_falls_back_to_env(self, monkeypatch):
        """An unset token path reads `$MSWEP_TOKEN_FILE`."""
        monkeypatch.setenv(TOKEN_FILE_ENV, "/tmp/token.json")
        assert MswepCredentials().resolved_token_path() == Path("/tmp/token.json")

    def test_rclone_remote_falls_back_to_env(self, monkeypatch):
        """An unset remote name reads `$MSWEP_RCLONE_REMOTE`."""
        monkeypatch.setenv(RCLONE_REMOTE_ENV, "GoogleDrive")
        assert MswepCredentials().resolved_rclone_remote() == "GoogleDrive"

    def test_explicit_rclone_config_returned_even_if_absent(self, tmp_path):
        """A typo'd explicit path is returned so it fails loudly, not silently."""
        missing = tmp_path / "nope.conf"
        assert MswepCredentials(rclone_config=missing).resolved_rclone_config() == (
            missing
        )

    def test_rclone_config_env_beats_defaults(self, monkeypatch, tmp_path):
        """`$MSWEP_RCLONE_CONFIG` is probed before rclone's own defaults."""
        monkeypatch.setenv(RCLONE_CONFIG_ENV, str(tmp_path / "custom.conf"))
        resolved = MswepCredentials().resolved_rclone_config()
        assert resolved == tmp_path / "custom.conf"

    def test_no_config_anywhere_resolves_none(self, monkeypatch):
        """With nothing set and no default present, resolution yields `None`."""
        monkeypatch.setattr(
            "earthlens.mswep.auth.default_rclone_config_paths", lambda: []
        )
        assert MswepCredentials().resolved_rclone_config() is None

    def test_default_paths_are_probed_in_order(self, monkeypatch):
        """`$RCLONE_CONFIG` leads the default probe order."""
        monkeypatch.setenv("RCLONE_CONFIG", "/custom/rclone.conf")
        assert default_rclone_config_paths()[0] == Path("/custom/rclone.conf")

    def test_credentials_are_frozen(self):
        """The model is immutable, so a resolved credential cannot drift."""
        creds = MswepCredentials(folder_id="1AbC")
        with pytest.raises(Exception):
            creds.folder_id = "other"


class TestMswepAuth:
    """The `AbstractAuth` surface."""

    def test_injected_service_skips_credential_resolution(self):
        """An injected client authenticates with no credentials at all."""
        sentinel = object()
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"), service=sentinel)
        auth.configure()
        assert auth.is_authenticated()
        assert auth.service is sentinel
        assert auth.folder_id == "1AbC"

    def test_configure_is_idempotent(self):
        """A second configure() short-circuits rather than rebuilding."""
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"), service=object())
        auth.configure()
        first = auth.service
        auth.configure()
        assert auth.service is first

    def test_missing_folder_id_raises_naming_the_form(self):
        """No folder id raises with the GloH2O request URL."""
        auth = MswepAuth(MswepCredentials(), service=object())
        with pytest.raises(AuthenticationError, match="gloh2o.org/mswep"):
            auth.configure()

    def test_folder_id_read_from_env(self, monkeypatch):
        """The folder id resolves from `$MSWEP_DRIVE_FOLDER`."""
        monkeypatch.setenv(FOLDER_ID_ENV, "1FromEnv")
        auth = MswepAuth(service=object())
        auth.configure()
        assert auth.folder_id == "1FromEnv"

    def test_service_before_configure_raises(self):
        """Touching the client before configure() is an error, not a `None`."""
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        with pytest.raises(AuthenticationError, match="before configure"):
            _ = auth.service

    def test_folder_id_before_configure_raises(self):
        """Touching the folder id before configure() is an error."""
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        with pytest.raises(AuthenticationError, match="before configure"):
            _ = auth.folder_id

    def test_no_credential_source_names_both_forms(self, monkeypatch):
        """With nothing configured and no ADC, the error names both forms."""
        monkeypatch.setattr(
            "earthlens.mswep.auth.default_rclone_config_paths", lambda: []
        )
        monkeypatch.setattr(auth_module, "try_application_default", lambda: None)
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        with pytest.raises(AuthenticationError, match="gloh2o.org/mswx"):
            auth.configure()

    def test_rclone_config_without_remote_raises(self, tmp_path, monkeypatch):
        """A discovered config with no remote name asks for the remote."""
        path = _write(tmp_path / "rclone.conf", RCLONE_DRIVE_REMOTE)
        monkeypatch.setenv(RCLONE_CONFIG_ENV, str(path))
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        with pytest.raises(AuthenticationError, match="no remote name"):
            auth.configure()

    def test_context_manager_configures(self):
        """The context-manager form authenticates on enter."""
        with MswepAuth(MswepCredentials(folder_id="1AbC"), service=object()) as auth:
            assert auth.is_authenticated()

    def test_service_account_key_is_accepted(self, tmp_path, monkeypatch):
        """A service-account key on `$MSWEP_TOKEN_FILE` builds a client now.

        GloH2O link-shares, so a service account reads the folder; the old
        hard rejection is gone.
        """
        path = _write(
            tmp_path / "key.json",
            json.dumps(
                {
                    "type": "service_account",
                    "client_email": "bot@proj.iam.gserviceaccount.com",
                }
            ),
        )
        monkeypatch.setenv(TOKEN_FILE_ENV, str(path))
        monkeypatch.setattr(
            auth_module, "credentials_from_service_account", lambda p: "sa-creds"
        )
        built = {}

        def _fake_build(name, version, credentials=None, cache_discovery=None):
            built["credentials"] = credentials
            return "drive-client"

        monkeypatch.setattr(
            auth_module, "_import_google_modules", lambda: (object(), _fake_build)
        )
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        auth.configure()
        assert auth.service == "drive-client"
        assert built["credentials"] == "sa-creds"

    def test_application_default_is_the_final_fallback(self, monkeypatch):
        """With no explicit credential, ADC resolves the client."""
        monkeypatch.setattr(
            "earthlens.mswep.auth.default_rclone_config_paths", lambda: []
        )
        monkeypatch.setattr(auth_module, "try_application_default", lambda: "adc-creds")
        built = {}

        def _fake_build(name, version, credentials=None, cache_discovery=None):
            built["credentials"] = credentials
            return "drive-client"

        monkeypatch.setattr(
            auth_module, "_import_google_modules", lambda: (object(), _fake_build)
        )
        auth = MswepAuth(MswepCredentials(folder_id="1AbC"))
        auth.configure()
        assert built["credentials"] == "adc-creds"
