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
            gportal_password=SecretStr("pytest-fixture-not-a-real-pw"),
        ),
        protocol="gportal",
    )
    auth.configure()
    assert auth.username == "alice"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "pytest-fixture-not-a-real-pw"
    assert auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_credentials_from_environment(monkeypatch) -> None:
    """When kwargs are absent, env vars are read."""
    monkeypatch.setenv("GPORTAL_USERNAME", "bob")
    monkeypatch.setenv("GPORTAL_PASSWORD", "pytest-fixture-env-not-real")
    auth = JaxaAuth(JaxaCredentials(), protocol="gportal")
    auth.configure()
    assert auth.username == "bob"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "pytest-fixture-env-not-real"


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
    """`JaxaAuth(protocol=...)` rejects a protocol not in the supported set."""
    with pytest.raises(ValueError, match="'jaxa-earth', 'gportal', 'ptree'"):
        JaxaAuth(JaxaCredentials(), protocol="nonsense")  # type: ignore[arg-type]


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_credentials_from_explicit_kwargs(monkeypatch) -> None:
    """Explicit ptree_username/password are stored on the auth object."""
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    auth = JaxaAuth(
        JaxaCredentials(
            ptree_username="alice@example.org",
            ptree_password=SecretStr("pytest-fixture-not-a-real-pw"),
        ),
        protocol="ptree",
    )
    auth.configure()
    assert auth.username == "alice@example.org"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "pytest-fixture-not-a-real-pw"
    assert auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_credentials_from_environment(monkeypatch) -> None:
    """The ptree protocol reads its own JAXA_PTREE_* env fallback."""
    monkeypatch.setenv("JAXA_PTREE_USERNAME", "env-user@example.org")
    monkeypatch.setenv("JAXA_PTREE_PASSWORD", "pytest-fixture-env-not-real")
    auth = JaxaAuth(JaxaCredentials(), protocol="ptree")
    auth.configure()
    assert auth.username == "env-user@example.org"
    assert auth.password is not None
    assert auth.password.get_secret_value() == "pytest-fixture-env-not-real"


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_missing_credentials_raise(monkeypatch) -> None:
    """The ptree branch raises with a P-Tree-specific message when creds are absent."""
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    auth = JaxaAuth(JaxaCredentials(), protocol="ptree")
    with pytest.raises(AuthenticationError, match="JAXA_PTREE_USERNAME"):
        auth.configure()


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_error_names_registration_url(monkeypatch) -> None:
    """The ptree error message points at the P-Tree registration URL."""
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    auth = JaxaAuth(JaxaCredentials(), protocol="ptree")
    with pytest.raises(AuthenticationError, match=r"eorc\.jaxa\.jp/ptree"):
        auth.configure()


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_and_gportal_use_separate_credential_pairs(monkeypatch) -> None:
    """P-Tree creds on the credentials object do not satisfy the gportal branch."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    creds = JaxaCredentials(
        ptree_username="alice@example.org",
        ptree_password=SecretStr("pytest-fixture-not-a-real-pw"),
    )
    auth = JaxaAuth(creds, protocol="gportal")
    with pytest.raises(AuthenticationError, match="GPORTAL_USERNAME"):
        auth.configure()


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
            gportal_password=SecretStr("pytest-fixture-not-a-real-pw"),
        ),
        protocol="gportal",
    )
    auth.configure()
    assert gportal.username is None
    assert gportal.password is None
