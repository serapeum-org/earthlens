"""Unit tests for IUCN token resolution."""

from __future__ import annotations

import pytest

from earthlens.iucn import AuthenticationError, IucnAuth, IucnCredentials


@pytest.mark.iucn
class TestIucnAuth:
    """`IucnAuth` resolves a token from the argument or the environment."""

    def test_explicit_token(self):
        """An explicit token resolves and is readable after configure."""
        auth = IucnAuth(IucnCredentials(token="k"))
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.token == "k"

    def test_env_token(self, monkeypatch):
        """The token falls back to the IUCN_TOKEN environment variable."""
        monkeypatch.setenv("IUCN_TOKEN", "from-env")
        auth = IucnAuth(IucnCredentials())
        auth.configure()
        assert auth.token == "from-env"

    def test_missing_token_raises(self, monkeypatch):
        """A missing token raises an error naming IUCN_TOKEN."""
        monkeypatch.delenv("IUCN_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="IUCN_TOKEN"):
            IucnAuth(IucnCredentials()).configure()

    def test_token_before_configure_raises(self):
        """Reading the token before configure raises a clear error."""
        with pytest.raises(AuthenticationError, match="configure"):
            _ = IucnAuth(IucnCredentials(token="k")).token

    def test_configure_is_idempotent(self):
        """A second configure call short-circuits and keeps the token."""
        auth = IucnAuth(IucnCredentials(token="k"))
        auth.configure()
        auth.configure()
        assert auth.token == "k"
