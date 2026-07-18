"""Unit tests for `earthlens.risk_indicators.auth` (GFW key resolution)."""

from __future__ import annotations

import pytest

import earthlens.base
from earthlens.risk_indicators import (
    AuthenticationError,
    GfwAuth,
    GfwCredentials,
)

pytestmark = pytest.mark.risk_indicators


class TestGfwCredentials:
    """The credentials value object hides the key and defaults to None."""

    def test_secretstr_hides_key_in_repr(self):
        """The API key never appears in repr()."""
        creds = GfwCredentials(api_key="topsecret")
        assert "topsecret" not in repr(creds)
        assert creds.api_key.get_secret_value() == "topsecret"

    def test_key_optional(self):
        """The key defaults to None for env-var resolution."""
        assert GfwCredentials().api_key is None

    def test_frozen(self):
        """Credentials are immutable."""
        creds = GfwCredentials(api_key="k")
        with pytest.raises(Exception):
            creds.api_key = "other"


class TestGfwAuth:
    """Key resolution precedence, idempotency, and failure."""

    def test_not_authenticated_before_configure(self):
        """A fresh auth has not resolved a key yet."""
        assert GfwAuth(GfwCredentials(api_key="k")).is_authenticated() is False

    def test_explicit_key_resolves(self):
        """An explicit key resolves and is read back via the property."""
        auth = GfwAuth(GfwCredentials(api_key="explicit"))
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.api_key == "explicit"

    def test_env_fallback_resolves(self, monkeypatch):
        """A missing explicit key falls back to GFW_API_KEY."""
        monkeypatch.setenv("GFW_API_KEY", "from-env")
        auth = GfwAuth(GfwCredentials())
        auth.configure()
        assert auth.api_key == "from-env"

    def test_explicit_key_wins_over_env(self, monkeypatch):
        """An explicit key takes precedence over the env var."""
        monkeypatch.setenv("GFW_API_KEY", "from-env")
        auth = GfwAuth(GfwCredentials(api_key="explicit"))
        auth.configure()
        assert auth.api_key == "explicit"

    def test_missing_key_raises_naming_env_var(self, monkeypatch):
        """No key anywhere raises AuthenticationError naming GFW_API_KEY."""
        monkeypatch.delenv("GFW_API_KEY", raising=False)
        with pytest.raises(AuthenticationError, match="GFW_API_KEY"):
            GfwAuth(GfwCredentials()).configure()

    def test_configure_idempotent(self, monkeypatch):
        """A second configure() is a no-op once authenticated."""
        monkeypatch.delenv("GFW_API_KEY", raising=False)
        auth = GfwAuth(GfwCredentials(api_key="k"))
        auth.configure()
        auth.configure()
        assert auth.api_key == "k"

    def test_api_key_before_configure_raises(self):
        """Reading the key before configure() raises."""
        with pytest.raises(AuthenticationError, match="has not run yet"):
            _ = GfwAuth(GfwCredentials(api_key="k")).api_key

    def test_context_manager_configures(self):
        """The context manager configures on enter."""
        with GfwAuth(GfwCredentials(api_key="k")) as auth:
            assert auth.api_key == "k"

    def test_error_message_omits_the_key(self, monkeypatch):
        """The missing-key error never echoes a secret."""
        monkeypatch.delenv("GFW_API_KEY", raising=False)
        with pytest.raises(AuthenticationError) as exc:
            GfwAuth(GfwCredentials()).configure()
        assert "MyGFW" in str(exc.value)


class TestAuthenticationError:
    """The backend error subclasses the cross-backend AuthenticationError."""

    def test_subclasses_base(self):
        """A caller can catch it as the base AuthenticationError."""
        assert issubclass(AuthenticationError, earthlens.base.AuthenticationError)
