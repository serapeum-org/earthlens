"""Tests for the MSWEP branches the main suites leave uncovered."""

from __future__ import annotations

import datetime as dt
import sys

import pytest

from earthlens.mswep import auth as auth_module
from earthlens.mswep.auth import (
    RCLONE_REMOTE_ENV,
    TOKEN_FILE_ENV,
    AuthenticationError,
    MswepAuth,
    MswepCredentials,
    default_rclone_config_paths,
)
from earthlens.mswep.backend import MSWEP
from earthlens.mswep.catalog import Catalog

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


class _StubCredentials:
    """Stand-in for `google.oauth2.credentials.Credentials`."""

    def __init__(self, **fields):
        """Record the fields the caller supplied."""
        self.__dict__.update(fields)

    @classmethod
    def from_authorized_user_info(cls, info, scopes=None):
        """Build a stub from an authorized-user payload."""
        return cls(refresh_token=info.get("refresh_token"), scopes=scopes)


class TestRcloneConfigProbeOrder:
    """Platform-specific default locations for `rclone.conf`."""

    def test_windows_probes_appdata(self, monkeypatch):
        """On Windows the `%APPDATA%\\rclone` location is probed."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        assert any("Roaming" in str(p) for p in default_rclone_config_paths())

    def test_windows_without_appdata_skips_it(self, monkeypatch):
        """A Windows box with no `%APPDATA%` still yields the POSIX defaults."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        assert default_rclone_config_paths()

    def test_posix_uses_xdg_config_home(self, monkeypatch):
        """`$XDG_CONFIG_HOME` wins over `~/.config` on POSIX."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
        assert any("xdg" in str(p) for p in default_rclone_config_paths())

    def test_posix_without_xdg_falls_back_to_home(self, monkeypatch):
        """Without `$XDG_CONFIG_HOME` the probe uses `~/.config`."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        assert any(".config" in str(p) for p in default_rclone_config_paths())

    def test_legacy_dot_rclone_conf_is_probed(self, monkeypatch):
        """The legacy `~/.rclone.conf` location stays in the probe list."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        assert any(p.name == ".rclone.conf" for p in default_rclone_config_paths())

    def test_discovered_default_is_used(self, monkeypatch, tmp_path):
        """An existing default config is picked up with no env var set."""
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        found = tmp_path / "rclone.conf"
        found.write_text("[r]\ntype = drive\n", encoding="utf-8")
        monkeypatch.setattr(auth_module, "default_rclone_config_paths", lambda: [found])
        assert MswepCredentials().resolved_rclone_config() == found


class TestMissingSdk:
    """The `[mswep]` extra is imported lazily and reported clearly."""

    def test_missing_sdk_names_the_extra(self, monkeypatch):
        """An absent Drive SDK raises naming `earthlens[mswep]`."""
        monkeypatch.setitem(sys.modules, "googleapiclient.discovery", None)
        monkeypatch.setitem(sys.modules, "google.oauth2.credentials", None)
        with pytest.raises(ImportError, match=r"earthlens\[mswep\]"):
            auth_module._import_google_modules()


class TestCredentialLadder:
    """Source precedence inside `MswepAuth._resolve_credentials`."""

    def test_rclone_route_is_taken_when_configured(self, tmp_path, monkeypatch):
        """With a config and a remote name, the rclone credential is built."""
        config = tmp_path / "rclone.conf"
        config.write_text(
            "[GoogleDrive]\ntype = drive\nclient_id = a\nclient_secret = b\n"
            'token = {"access_token":"at","refresh_token":"rt"}\n',
            encoding="utf-8",
        )
        monkeypatch.delenv(TOKEN_FILE_ENV, raising=False)
        monkeypatch.setenv(RCLONE_REMOTE_ENV, "GoogleDrive")
        creds = MswepCredentials(folder_id="1AbC", rclone_config=config)
        built = MswepAuth(creds)._resolve_credentials()
        assert built.refresh_token == "rt"

    def test_token_file_wins_over_rclone(self, tmp_path, monkeypatch):
        """An explicit token path is preferred to a configured remote."""
        token = tmp_path / "token.json"
        token.write_text(
            '{"type":"authorized_user","client_id":"c","client_secret":"s",'
            '"refresh_token":"from-token-file"}',
            encoding="utf-8",
        )
        config = tmp_path / "rclone.conf"
        config.write_text(
            "[GoogleDrive]\ntype = drive\nclient_id = a\nclient_secret = b\n"
            'token = {"access_token":"at","refresh_token":"from-rclone"}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(RCLONE_REMOTE_ENV, "GoogleDrive")
        creds = MswepCredentials(
            folder_id="1AbC", token_path=token, rclone_config=config
        )
        built = MswepAuth(creds)._resolve_credentials()
        assert built.refresh_token == "from-token-file"

    def test_unparseable_rclone_config_raises(self, tmp_path):
        """A corrupt config surfaces as an auth error, not a parser error."""
        config = tmp_path / "rclone.conf"
        config.write_text("[a]\n[a]\n", encoding="utf-8")
        creds = MswepCredentials(
            folder_id="1AbC", rclone_config=config, rclone_remote="a"
        )
        auth = MswepAuth(creds)
        with pytest.raises(AuthenticationError, match="could not be parsed"):
            auth._resolve_credentials()

    def test_configure_builds_a_client_from_resolved_credentials(
        self, tmp_path, monkeypatch
    ):
        """With no injected service, `configure` builds a Drive client."""
        token = tmp_path / "token.json"
        token.write_text(
            '{"type":"authorized_user","client_id":"c","client_secret":"s",'
            '"refresh_token":"rt"}',
            encoding="utf-8",
        )
        built = {}

        def _fake_build(name, version, credentials=None, cache_discovery=None):
            built["args"] = (name, version)
            return "drive-client"

        monkeypatch.setattr(
            auth_module,
            "_import_google_modules",
            lambda: (_StubCredentials, _fake_build),
        )
        auth = MswepAuth(MswepCredentials(folder_id="1AbC", token_path=token))
        auth.configure()
        assert auth.service == "drive-client"
        assert built["args"] == ("drive", "v3")


class TestBackendEdges:
    """Backend branches the happy paths do not reach."""

    def test_variables_accepts_a_mapping(self, share, tmp_path):
        """`variables={product: [...]}` flattens to the same list form."""
        source = MSWEP(
            start="2007-05-13",
            end="2007-05-13",
            product="mswx",
            variables={"mswx": ["Temp"]},
            temporal_resolution="daily",
            folder_id="SHARE",
            service=share,
            path=tmp_path,
        )
        assert source._variables() == ["Temp"]

    def test_unknown_resolution_raises(self, share, tmp_path):
        """A cadence the product does not offer is rejected."""
        with pytest.raises(ValueError, match="not a known cadence|resolution"):
            MSWEP(
                start="2020-04-25",
                end="2020-04-26",
                temporal_resolution="fortnightly",
                folder_id="SHARE",
                service=share,
                path=tmp_path,
            )

    def test_revision_window_ignores_a_non_datetime_stamp(self, share, tmp_path):
        """A stamp that is not a datetime cannot be aged, so it is not revised."""
        source = MSWEP(
            start="2025-01-01",
            end="2025-01-01",
            temporal_resolution="daily",
            variant="NRT",
            folder_id="SHARE",
            service=share,
            path=tmp_path,
        )
        assert not source.is_under_revision(dt.date(2025, 1, 1), "x/NRT/Daily")

    def test_now_returns_an_aware_utc_time(self):
        """The default clock is timezone-aware, so subtraction never crashes."""
        assert MSWEP._now().tzinfo is not None

    def test_zero_revision_window_disables_the_policy(self, share, tmp_path):
        """A catalog with no revision window never forces a re-download."""
        catalog = Catalog()
        catalog.nrt_revision_days = 0
        source = MSWEP(
            start="2025-01-01",
            end="2025-01-01",
            temporal_resolution="daily",
            variant="NRT",
            folder_id="SHARE",
            service=share,
            path=tmp_path,
            catalog=catalog,
        )
        stamp = dt.datetime.now(dt.timezone.utc)
        assert not source.is_under_revision(stamp, "x/NRT/Daily")


class TestCatalogValidationErrors:
    """Malformed product rows are reported with their product key."""

    def test_invalid_product_field_type_raises(self, tmp_path):
        """A row whose field has the wrong type names the product."""
        text = (
            "products:\n"
            "  mswep:\n"
            '    path_template: "{root}/{stem}.nc"\n'
            "    default_version: [not, a, string]\n"
        )
        path = tmp_path / "cat.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="mswep"):
            Catalog.load(path)
