"""Unit tests for WDPA token resolution."""

from __future__ import annotations

import pytest

from earthlens.wdpa import AuthenticationError, WdpaAuth, WdpaCredentials


@pytest.mark.wdpa
class TestWdpaAuth:
    """`WdpaAuth` resolves a token from the argument or the environment."""

    def test_explicit_token(self):
        """An explicit token resolves and is readable after configure."""
        auth = WdpaAuth(WdpaCredentials(token="k"))
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.token == "k"

    def test_env_token(self, monkeypatch):
        """The token falls back to the WDPA_TOKEN environment variable."""
        monkeypatch.setenv("WDPA_TOKEN", "from-env")
        auth = WdpaAuth(WdpaCredentials())
        auth.configure()
        assert auth.token == "from-env"

    def test_missing_token_raises(self, monkeypatch):
        """A missing token raises an error naming WDPA_TOKEN."""
        monkeypatch.delenv("WDPA_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="WDPA_TOKEN"):
            WdpaAuth(WdpaCredentials()).configure()

    def test_token_before_configure_raises(self):
        """Reading the token before configure raises a clear error."""
        with pytest.raises(AuthenticationError, match="configure"):
            _ = WdpaAuth(WdpaCredentials(token="k")).token
