"""Unit tests for the optional `JaxaAuth` credentials resolver."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from earthlens.jaxa import AuthenticationError, JaxaAuth, JaxaCredentials


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_protocol_needs_no_credentials() -> None:
    """The jaxa-earth branch is authless — configure() is a no-op."""
    auth = JaxaAuth(JaxaCredentials())
    assert auth.is_authenticated("jaxa-earth") is False
    auth.configure("jaxa-earth")
    assert auth.is_authenticated("jaxa-earth") is True


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_configure_idempotent() -> None:
    """A repeated configure call short-circuits via is_authenticated."""
    auth = JaxaAuth(JaxaCredentials())
    auth.configure("jaxa-earth")
    auth.configure("jaxa-earth")  # no error, no extra work
    assert auth.is_authenticated("jaxa-earth")


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_credentials_from_explicit_kwargs(monkeypatch) -> None:
    """Explicit gportal_username/password take precedence over the env."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    pytest.importorskip("gportal")
    import gportal

    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="alice",
            gportal_password=SecretStr("topsecret"),
        )
    )
    auth.configure("gportal")
    assert gportal.username == "alice"
    assert gportal.password == "topsecret"
    assert auth.is_authenticated("gportal")
    gportal.username = None
    gportal.password = None


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_credentials_from_environment(monkeypatch) -> None:
    """When kwargs are absent, env vars are read."""
    pytest.importorskip("gportal")
    import gportal

    monkeypatch.setenv("GPORTAL_USERNAME", "bob")
    monkeypatch.setenv("GPORTAL_PASSWORD", "envpass")
    auth = JaxaAuth(JaxaCredentials())
    auth.configure("gportal")
    assert gportal.username == "bob"
    assert gportal.password == "envpass"
    gportal.username = None
    gportal.password = None


@pytest.mark.jaxa
@pytest.mark.unit
def test_missing_gportal_credentials_raise(monkeypatch) -> None:
    """No kwargs + no env vars → AuthenticationError naming the env vars."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    auth = JaxaAuth(JaxaCredentials())
    with pytest.raises(AuthenticationError, match="GPORTAL_USERNAME"):
        auth.configure("gportal")


@pytest.mark.jaxa
@pytest.mark.unit
def test_unknown_protocol_rejected() -> None:
    """`configure` rejects a protocol that's not one of the two."""
    auth = JaxaAuth(JaxaCredentials())
    with pytest.raises(ValueError, match="must be 'jaxa-earth' or 'gportal'"):
        auth.configure("ptree")
