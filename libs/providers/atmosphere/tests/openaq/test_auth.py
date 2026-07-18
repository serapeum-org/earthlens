"""Unit tests for `earthlens.openaq.auth` (API-key resolution)."""

from __future__ import annotations

import pytest

import earthlens.base
from earthlens.openaq import (
    AuthenticationError,
    OpenaqAuth,
    OpenaqCredentials,
)


@pytest.mark.openaq
class TestOpenaqCredentials:
    """The credentials value object hides the key and defaults to None."""

    def test_secretstr_hides_key_in_repr(self):
        """The API key never appears in repr()."""
        creds = OpenaqCredentials(api_key="topsecret")
        assert "topsecret" not in repr(creds)
        assert creds.api_key.get_secret_value() == "topsecret"

    def test_key_optional(self):
        """The key defaults to None for env-var resolution."""
        assert OpenaqCredentials().api_key is None

    def test_frozen(self):
        """Credentials are immutable."""
        creds = OpenaqCredentials(api_key="k")
        with pytest.raises(Exception):
            creds.api_key = "other"


@pytest.mark.openaq
class TestOpenaqAuth:
    """Key resolution precedence and idempotency."""

    def test_not_authenticated_before_configure(self):
        """A fresh auth has not resolved a key yet."""
        auth = OpenaqAuth(OpenaqCredentials(api_key="k"))
        assert auth.is_authenticated() is False

    def test_explicit_key_resolves(self):
        """An explicit api_key resolves and is exposed after configure()."""
        auth = OpenaqAuth(OpenaqCredentials(api_key="explicit"))
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.api_key == "explicit"

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """OPENAQ_API_KEY is used when no explicit key is given."""
        monkeypatch.setenv("OPENAQ_API_KEY", "from-env")
        auth = OpenaqAuth(OpenaqCredentials())
        auth.configure()
        assert auth.api_key == "from-env"

    def test_explicit_beats_env(self, monkeypatch: pytest.MonkeyPatch):
        """An explicit key wins over the environment variable."""
        monkeypatch.setenv("OPENAQ_API_KEY", "from-env")
        auth = OpenaqAuth(OpenaqCredentials(api_key="explicit"))
        auth.configure()
        assert auth.api_key == "explicit"

    def test_configure_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        """A second configure() after success is a no-op (no re-resolution)."""
        monkeypatch.setenv("OPENAQ_API_KEY", "first")
        auth = OpenaqAuth(OpenaqCredentials())
        auth.configure()
        monkeypatch.setenv("OPENAQ_API_KEY", "second")
        auth.configure()
        assert auth.api_key == "first"

    def test_missing_key_raises_naming_register_url(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """No key and no env var raises AuthenticationError naming the URL."""
        monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
        auth = OpenaqAuth(OpenaqCredentials())
        with pytest.raises(AuthenticationError, match="explore.openaq.org/register"):
            auth.configure()

    def test_authentication_error_is_subclass_of_base(self):
        """The backend error subclasses the cross-backend base error."""
        assert issubclass(AuthenticationError, earthlens.base.AuthenticationError)

    def test_api_key_property_raises_before_configure(self):
        """Reading api_key before configure() raises AuthenticationError."""
        auth = OpenaqAuth(OpenaqCredentials(api_key="k"))
        with pytest.raises(AuthenticationError, match="configure"):
            _ = auth.api_key

    def test_context_manager_configures(self):
        """The context-manager form resolves the key on enter."""
        with OpenaqAuth(OpenaqCredentials(api_key="k")) as auth:
            assert auth.api_key == "k"
