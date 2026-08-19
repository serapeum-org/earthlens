"""Tests for FIRMS MAP_KEY credentials and resolution."""

from __future__ import annotations

import pytest

from earthlens.base import AuthenticationError as BaseAuthenticationError
from earthlens.firms import (
    AuthenticationError,
    FirmsAuth,
    FirmsCredentials,
)
from earthlens.firms.auth import _MAP_KEY_URL

pytestmark = pytest.mark.firms


def test_secretstr_hides_key():
    """The MAP_KEY never appears in the credentials repr."""
    creds = FirmsCredentials(api_key="topsecret")
    assert "topsecret" not in repr(creds)
    assert creds.api_key.get_secret_value() == "topsecret"


def test_explicit_key_resolves():
    """configure() resolves an explicit api_key and exposes it."""
    auth = FirmsAuth(FirmsCredentials(api_key="k"))
    assert auth.is_authenticated() is False
    auth.configure()
    assert auth.is_authenticated() is True
    assert auth.api_key == "k"


def test_env_var_fallback(monkeypatch: pytest.MonkeyPatch):
    """An absent explicit key falls back to FIRMS_MAP_KEY."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "from-env")
    auth = FirmsAuth(FirmsCredentials())
    auth.configure()
    assert auth.api_key == "from-env"


def test_explicit_key_beats_env(monkeypatch: pytest.MonkeyPatch):
    """An explicit api_key wins over FIRMS_MAP_KEY."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "from-env")
    auth = FirmsAuth(FirmsCredentials(api_key="explicit"))
    auth.configure()
    assert auth.api_key == "explicit"


def test_missing_key_raises_naming_arg_env_and_url(monkeypatch: pytest.MonkeyPatch):
    """No key anywhere raises AuthenticationError naming the arg, env var, and URL."""
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    auth = FirmsAuth(FirmsCredentials())
    with pytest.raises(AuthenticationError) as exc:
        auth.configure()
    message = str(exc.value)
    assert "api_key=" in message, message
    assert "FIRMS_MAP_KEY" in message, message
    assert _MAP_KEY_URL in message, message


def test_configure_is_idempotent():
    """A second configure() after success is a no-op."""
    auth = FirmsAuth(FirmsCredentials(api_key="k"))
    auth.configure()
    auth.configure()
    assert auth.api_key == "k"


def test_api_key_property_raises_before_configure():
    """Reading api_key before configure() raises."""
    auth = FirmsAuth(FirmsCredentials(api_key="k"))
    with pytest.raises(AuthenticationError, match="has not run"):
        _ = auth.api_key


def test_subclasses_base_authentication_error():
    """The FIRMS error is catchable as the cross-backend base error."""
    assert issubclass(AuthenticationError, BaseAuthenticationError)
