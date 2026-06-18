"""Unit tests for the optional `JaxaAuth` credentials resolver."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from earthlens.jaxa import AuthenticationError, JaxaAuth, JaxaCredentials


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_protocol_needs_no_credentials() -> None:
    """The jaxa-earth protocol's configure() is a no-op."""
    auth = JaxaAuth(JaxaCredentials(), protocol="jaxa-earth")
    assert not auth.is_authenticated()
    auth.configure()
    assert auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_configure_idempotent() -> None:
    """A repeated configure call short-circuits via is_authenticated."""
    auth = JaxaAuth(JaxaCredentials(), protocol="jaxa-earth")
    auth.configure()
    auth.configure()  # no error, no extra work
    assert auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_credentials_from_explicit_kwargs(monkeypatch) -> None:
    """Explicit gportal_username/password are stored on the auth object."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="alice",
            gportal_password=SecretStr("topsecret"),
        ),
        protocol="gportal",
    )
    auth.configure()
    assert auth.username == "alice"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "topsecret"
    assert auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_credentials_from_environment(monkeypatch) -> None:
    """When kwargs are absent, env vars are read."""
    monkeypatch.setenv("GPORTAL_USERNAME", "bob")
    monkeypatch.setenv("GPORTAL_PASSWORD", "envpass")
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")
    auth.configure()
    assert auth.username == "bob"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "envpass"


@pytest.mark.jaxa
@pytest.mark.unit
def test_missing_gportal_credentials_raise(monkeypatch) -> None:
    """No kwargs + no env vars → AuthenticationError naming the env vars."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")
    with pytest.raises(AuthenticationError, match="GPORTAL_USERNAME"):
        auth.configure()


@pytest.mark.jaxa
@pytest.mark.unit
def test_unknown_protocol_rejected() -> None:
    """`JaxaAuth(protocol=...)` rejects a protocol that's not one of the two."""
    with pytest.raises(ValueError, match="must be 'jaxa-earth' or 'gportal'"):
        JaxaAuth(JaxaCredentials(), protocol="ptree")  # type: ignore[arg-type]


@pytest.mark.jaxa
@pytest.mark.unit
def test_auth_does_not_mutate_sdk_globals(monkeypatch) -> None:
    """`configure("gportal")` no longer writes to `gportal.username` / `.password`.

    Threading credentials through `auth.username` / `.password` and into
    `gportal.download(username=, password=)` kwargs means the SDK's
    module-level globals stay untouched, so requests don't leak creds
    into each other.
    """
    pytest.importorskip("gportal")
    import gportal

    monkeypatch.setattr(gportal, "username", None, raising=False)
    monkeypatch.setattr(gportal, "password", None, raising=False)
    auth = JaxaAuth(
        JaxaCredentials(
            gportal_username="alice",
            gportal_password=SecretStr("topsecret"),
        ),
        protocol="gportal",
    )
    auth.configure()
    assert gportal.username is None
    assert gportal.password is None
