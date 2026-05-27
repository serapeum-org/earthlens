"""Tests for the optional USGS Water Personal Access Token auth."""

from __future__ import annotations

import pytest

from earthlens.usgs_water import UsgsWaterAuth, UsgsWaterCredentials

pytestmark = pytest.mark.usgs_water


def test_explicit_token_resolves_and_authenticates(monkeypatch):
    """An explicit api_token resolves and flips is_authenticated to True."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    auth = UsgsWaterAuth(UsgsWaterCredentials(api_token="tok"))
    assert auth.is_authenticated() is False
    auth.configure()
    assert auth.is_authenticated() is True
    assert auth.token == "tok"


def test_explicit_token_exported_to_env(monkeypatch):
    """configure() exports the resolved token to API_USGS_PAT for the SDK."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    UsgsWaterAuth(UsgsWaterCredentials(api_token="secret")).configure()
    import os

    assert os.environ["API_USGS_PAT"] == "secret"


def test_token_resolved_from_env(monkeypatch):
    """With no explicit token, the env var supplies it."""
    monkeypatch.setenv("API_USGS_PAT", "from-env")
    auth = UsgsWaterAuth(UsgsWaterCredentials())
    auth.configure()
    assert auth.token == "from-env"
    assert auth.is_authenticated() is True


def test_anonymous_is_not_an_error(monkeypatch):
    """No token anywhere is a valid anonymous mode, not an error."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    auth = UsgsWaterAuth(UsgsWaterCredentials())
    auth.configure()
    assert auth.is_authenticated() is False
    assert auth.token is None


def test_configure_is_idempotent(monkeypatch):
    """A second configure() after success is a no-op."""
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    auth = UsgsWaterAuth(UsgsWaterCredentials(api_token="tok"))
    auth.configure()
    auth.configure()
    assert auth.token == "tok"


def test_token_secret_hidden_in_repr():
    """The token is a SecretStr and never appears in repr()."""
    creds = UsgsWaterCredentials(api_token="hunter2")
    assert "hunter2" not in repr(creds)


def test_context_manager_configures_on_enter(monkeypatch):
    """The auth context manager configures on enter."""
    monkeypatch.setenv("API_USGS_PAT", "ctx")
    with UsgsWaterAuth(UsgsWaterCredentials()) as auth:
        assert auth.is_authenticated() is True
