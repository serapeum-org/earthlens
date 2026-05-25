"""Tests for FIRMS MAP_KEY credentials and resolution."""

from __future__ import annotations

import pytest

from earthlens.base import AuthenticationError as BaseAuthenticationError
from earthlens.firms import (
    AuthenticationError,
    FirmsAuth,
    FirmsCredentials,
)

pytestmark = pytest.mark.firms


def test_secretstr_hides_key():
    """The MAP_KEY never appears in the credentials repr."""
    creds = FirmsCredentials(map_key="topsecret")
    assert "topsecret" not in repr(creds)
    assert creds.map_key.get_secret_value() == "topsecret"


def test_explicit_key_resolves():
    """configure() resolves an explicit map_key and exposes it."""
    auth = FirmsAuth(FirmsCredentials(map_key="k"))
    assert auth.is_authenticated() is False
    auth.configure()
    assert auth.is_authenticated() is True
    assert auth.map_key == "k"


def test_env_var_fallback(monkeypatch: pytest.MonkeyPatch):
    """An absent explicit key falls back to FIRMS_MAP_KEY."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "from-env")
    auth = FirmsAuth(FirmsCredentials())
    auth.configure()
    assert auth.map_key == "from-env"


def test_explicit_key_beats_env(monkeypatch: pytest.MonkeyPatch):
    """An explicit map_key wins over FIRMS_MAP_KEY."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "from-env")
    auth = FirmsAuth(FirmsCredentials(map_key="explicit"))
    auth.configure()
    assert auth.map_key == "explicit"


def test_missing_key_raises_naming_url(monkeypatch: pytest.MonkeyPatch):
    """No key anywhere raises AuthenticationError naming the map_key URL."""
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    auth = FirmsAuth(FirmsCredentials())
    with pytest.raises(AuthenticationError, match="map_key"):
        auth.configure()


def test_configure_is_idempotent():
    """A second configure() after success is a no-op."""
    auth = FirmsAuth(FirmsCredentials(map_key="k"))
    auth.configure()
    auth.configure()
    assert auth.map_key == "k"


def test_map_key_property_raises_before_configure():
    """Reading map_key before configure() raises."""
    auth = FirmsAuth(FirmsCredentials(map_key="k"))
    with pytest.raises(AuthenticationError, match="has not run"):
        _ = auth.map_key


def test_subclasses_base_authentication_error():
    """The FIRMS error is catchable as the cross-backend base error."""
    assert issubclass(AuthenticationError, BaseAuthenticationError)
