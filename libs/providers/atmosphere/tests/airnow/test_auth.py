"""Tests for `AirnowAuth` API-key resolution."""

from __future__ import annotations

import pytest

from earthlens.airnow.auth import (
    AirnowAuth,
    AirnowCredentials,
    AuthenticationError,
)


@pytest.mark.airnow
class TestAirnowCredentials:
    """The frozen credentials value object."""

    def test_explicit_key_kept_as_secret(self):
        """An explicit key is stored and never echoed by `repr`."""
        creds = AirnowCredentials(api_key="topsecret")
        assert creds.api_key.get_secret_value() == "topsecret"
        assert "topsecret" not in repr(creds)

    def test_key_optional(self):
        """The key defaults to `None` (resolve from the environment)."""
        assert AirnowCredentials().api_key is None


@pytest.mark.airnow
class TestAirnowAuth:
    """Key resolution and the authenticated-state predicate."""

    def test_explicit_key_resolves(self):
        """An explicit key configures and reads back."""
        auth = AirnowAuth(AirnowCredentials(api_key="k"))
        assert auth.is_authenticated() is False
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.api_key == "k"

    def test_env_key_resolves(self, monkeypatch: pytest.MonkeyPatch):
        """A missing explicit key falls back to `AIRNOW_API_KEY`."""
        monkeypatch.setenv("AIRNOW_API_KEY", "envkey")
        auth = AirnowAuth(AirnowCredentials())
        auth.configure()
        assert auth.api_key == "envkey"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch):
        """No explicit key and no env var raises with the register URL."""
        monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
        auth = AirnowAuth(AirnowCredentials())
        with pytest.raises(AuthenticationError) as exc:
            auth.configure()
        assert "AIRNOW_API_KEY" in str(exc.value)
        assert "api_key=" in str(exc.value), str(exc.value)

    def test_configure_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        """A second `configure` after success short-circuits."""
        monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
        auth = AirnowAuth(AirnowCredentials(api_key="k"))
        auth.configure()
        auth.configure()
        assert auth.api_key == "k"

    def test_api_key_before_configure_raises(self):
        """Reading `api_key` before `configure` raises."""
        auth = AirnowAuth(AirnowCredentials(api_key="k"))
        with pytest.raises(AuthenticationError):
            _ = auth.api_key

    def test_context_manager_configures(self):
        """The context manager configures on enter."""
        with AirnowAuth(AirnowCredentials(api_key="k")) as auth:
            assert auth.api_key == "k"
