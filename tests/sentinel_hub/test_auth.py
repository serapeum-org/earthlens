"""Unit tests for `earthlens.sentinel_hub.auth` (OAuth2 client-credentials)."""

from __future__ import annotations

import pytest

from earthlens.sentinel_hub.auth import (
    AuthenticationError,
    SentinelHubAuth,
    SentinelHubCredentials,
)

pytestmark = pytest.mark.sentinel_hub


class TestCredentials:
    """`SentinelHubCredentials` env + secret handling."""

    def test_empty_has_no_id(self):
        """An empty object carries no client id."""
        assert SentinelHubCredentials().client_id is None

    def test_secret_is_opaque(self):
        """The secret is wrapped as a SecretStr."""
        creds = SentinelHubCredentials(client_id="a", client_secret="shh")
        assert creds.client_secret.get_secret_value() == "shh"

    def test_from_env(self, monkeypatch):
        """`from_env` reads the SH_* variables."""
        monkeypatch.setenv("SH_CLIENT_ID", "envid")
        monkeypatch.setenv("SH_CLIENT_SECRET", "envsecret")
        creds = SentinelHubCredentials.from_env()
        assert creds.client_id == "envid"
        assert creds.client_secret.get_secret_value() == "envsecret"


class TestConfigure:
    """`SentinelHubAuth.configure` builds an SHConfig from resolved creds."""

    def test_missing_credentials_raises(self, fake_sh):
        """No id/secret and no profile → AuthenticationError naming the dashboard."""
        with pytest.raises(AuthenticationError, match="CDSE Dashboard"):
            SentinelHubAuth().config()

    def test_kwargs_build_cdse_config(self, fake_sh):
        """Explicit creds populate the CDSE base + token urls."""
        cfg = SentinelHubAuth(
            SentinelHubCredentials(client_id="a", client_secret="b")
        ).config()
        assert cfg.sh_base_url == "https://sh.dataspace.copernicus.eu"
        assert "dataspace" in cfg.sh_token_url
        assert cfg.sh_client_id == "a"
        assert cfg.sh_client_secret == "b"

    def test_env_credentials_used(self, fake_sh, monkeypatch):
        """Environment creds are picked up when no kwargs are passed."""
        monkeypatch.setenv("SH_CLIENT_ID", "envid")
        monkeypatch.setenv("SH_CLIENT_SECRET", "envsecret")
        cfg = SentinelHubAuth().config()
        assert cfg.sh_client_id == "envid"

    def test_kwargs_win_over_env(self, fake_sh, monkeypatch):
        """Explicit kwargs override the environment."""
        monkeypatch.setenv("SH_CLIENT_ID", "envid")
        monkeypatch.setenv("SH_CLIENT_SECRET", "envsecret")
        cfg = SentinelHubAuth(
            SentinelHubCredentials(client_id="kw", client_secret="kws")
        ).config()
        assert cfg.sh_client_id == "kw"

    def test_commercial_endpoint(self, fake_sh):
        """The commercial endpoint sets the commercial base url."""
        cfg = SentinelHubAuth(
            SentinelHubCredentials(client_id="a", client_secret="b"),
            endpoint="commercial",
        ).config()
        assert cfg.sh_base_url == "https://services.sentinel-hub.com"

    def test_profile_only_is_allowed(self, fake_sh):
        """A profile with no id/secret is accepted (creds come from the profile)."""
        auth = SentinelHubAuth(SentinelHubCredentials(profile="myprofile"))
        cfg = auth.config()
        assert cfg.profile == "myprofile"

    def test_idempotent(self, fake_sh):
        """`is_authenticated` flips False→True and a second configure is a no-op."""
        auth = SentinelHubAuth(SentinelHubCredentials(client_id="a", client_secret="b"))
        assert auth.is_authenticated() is False
        auth.configure()
        first = auth.config()
        auth.configure()
        assert auth.config() is first
