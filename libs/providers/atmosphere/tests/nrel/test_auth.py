"""Unit tests for the required two-secret NREL auth (`earthlens.nrel.auth`)."""

from __future__ import annotations

import pytest
from earthlens.nrel.auth import (
    AuthenticationError,
    NrelAuth,
    NrelCredentials,
)

pytestmark = pytest.mark.nrel


def _auth(api_key=None, email=None) -> NrelAuth:
    """Build an NrelAuth from explicit credentials."""
    return NrelAuth(NrelCredentials(api_key=api_key, email=email))


class TestConfigureResolution:
    """Tests for `NrelAuth.configure` credential resolution."""

    def test_explicit_credentials_resolve(self):
        """Explicit api_key + email resolve to the held secrets."""
        auth = _auth("explicit-key", "me@example.com")
        auth.configure()
        assert auth.is_authenticated()
        assert auth.api_key.get_secret_value() == "explicit-key"
        assert auth.email == "me@example.com"

    def test_env_fallback_resolves_both(self, monkeypatch: pytest.MonkeyPatch):
        """An empty credentials object resolves both from the environment."""
        monkeypatch.setenv("NREL_API_KEY", "env-key")
        monkeypatch.setenv("NREL_EMAIL", "env@example.com")
        auth = _auth()
        auth.configure()
        assert auth.api_key.get_secret_value() == "env-key"
        assert auth.email == "env@example.com"

    def test_empty_explicit_key_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An explicit empty api_key falls back to the env var, like email does."""
        monkeypatch.setenv("NREL_API_KEY", "env-key")
        monkeypatch.setenv("NREL_EMAIL", "env@example.com")
        auth = _auth("", "")
        auth.configure()
        assert auth.api_key.get_secret_value() == "env-key"
        assert auth.email == "env@example.com"

    def test_explicit_key_wins_over_env(self, monkeypatch: pytest.MonkeyPatch):
        """An explicit api_key takes priority over the environment variable."""
        monkeypatch.setenv("NREL_API_KEY", "env-key")
        monkeypatch.setenv("NREL_EMAIL", "env@example.com")
        auth = _auth("explicit", "explicit@example.com")
        auth.configure()
        assert auth.api_key.get_secret_value() == "explicit"
        assert auth.email == "explicit@example.com"


class TestConfigureErrors:
    """Tests for the missing-credential error messages."""

    def test_missing_key_names_env_var(self, monkeypatch: pytest.MonkeyPatch):
        """No key anywhere raises an error naming NREL_API_KEY."""
        monkeypatch.delenv("NREL_API_KEY", raising=False)
        monkeypatch.delenv("NREL_EMAIL", raising=False)
        with pytest.raises(AuthenticationError, match="NREL_API_KEY"):
            _auth().configure()

    def test_empty_string_key_is_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An empty-string env key is rejected as missing."""
        monkeypatch.setenv("NREL_API_KEY", "")
        monkeypatch.setenv("NREL_EMAIL", "me@example.com")
        with pytest.raises(AuthenticationError, match="NREL_API_KEY"):
            _auth().configure()

    def test_key_present_but_missing_email_names_email_var(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A key with no email raises an error naming NREL_EMAIL."""
        monkeypatch.delenv("NREL_EMAIL", raising=False)
        with pytest.raises(AuthenticationError, match="NREL_EMAIL"):
            _auth("k").configure()


class TestSecretHandling:
    """Tests that the key stays hidden and properties guard pre-configure access."""

    def test_key_is_not_echoed_in_repr(self):
        """The SecretStr key never appears in the credentials repr."""
        creds = NrelCredentials(api_key="topsecret", email="me@example.com")
        assert "topsecret" not in repr(creds)
        assert "topsecret" not in str(creds)

    def test_api_key_before_configure_raises(self):
        """Reading api_key before configure raises an AuthenticationError."""
        with pytest.raises(AuthenticationError, match="configure"):
            _ = _auth("k", "me@example.com").api_key

    def test_email_before_configure_raises(self):
        """Reading email before configure raises an AuthenticationError."""
        with pytest.raises(AuthenticationError, match="configure"):
            _ = _auth("k", "me@example.com").email


class TestIdempotency:
    """Tests that configure is idempotent and a context manager works."""

    def test_configure_is_idempotent(self):
        """A second configure call after success is a no-op."""
        auth = _auth("k", "me@example.com")
        auth.configure()
        auth.configure()
        assert auth.api_key.get_secret_value() == "k"

    def test_context_manager_configures_on_enter(self):
        """The context-manager form resolves the credentials on enter."""
        with _auth("k", "me@example.com") as auth:
            assert auth.is_authenticated()
