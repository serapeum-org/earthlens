"""Tests for `earthlens.asf.auth.ASFAuth`."""

from __future__ import annotations

import errno

import pytest
import requests

from earthlens.asf import (
    ASFAuth,
    ASFCredentials,
    AuthenticationError,
)


def _enetunreach_error() -> requests.ConnectionError:
    """A ConnectionError shaped like a real dead-IPv6-route failure."""
    return requests.ConnectionError(
        "HTTPSConnectionPool(host='urs.earthdata.nasa.gov', port=443): "
        f"(Caused by NewConnectionError('[Errno {errno.ENETUNREACH}] Network is unreachable'))"
    )


@pytest.fixture
def has_ipv6():
    """Reset urllib3's HAS_IPV6 to True and restore it after the test."""
    import urllib3.util.connection as connection

    saved = connection.HAS_IPV6
    connection.HAS_IPV6 = True
    try:
        yield connection
    finally:
        connection.HAS_IPV6 = saved


@pytest.mark.asf
@pytest.mark.unit
def test_credentials_accepts_all_optional_fields() -> None:
    """An empty credentials object defers to env / netrc."""
    creds = ASFCredentials()
    assert creds.token is None and creds.username is None
    assert creds.password is None and creds.netrc_path is None


@pytest.mark.asf
@pytest.mark.unit
def test_credentials_secretstr_hides_token_in_repr() -> None:
    """The bearer token must not leak through `repr`."""
    creds = ASFCredentials(token="EDL.SHHHHH")
    assert "EDL.SHHHHH" not in repr(creds)


@pytest.mark.asf
@pytest.mark.unit
def test_asfauth_is_not_authenticated_on_construction() -> None:
    """Construction is side-effect-free — no session yet."""
    auth = ASFAuth(ASFCredentials())
    assert auth.is_authenticated() is False


@pytest.mark.asf
@pytest.mark.unit
def test_configure_builds_session_via_auth_with_token(
    fake_asf_search, fake_earthdata_auth
) -> None:
    """`configure` reads the token from the EDL handle and builds a session."""
    auth = ASFAuth(ASFCredentials(token="EDL.HEAD.BODY"))
    auth.configure()
    assert auth.is_authenticated()
    session = auth.session()
    assert session.token == "EDL.FAKE.TOKEN"


@pytest.mark.asf
@pytest.mark.unit
def test_configure_is_idempotent(fake_asf_search, fake_earthdata_auth) -> None:
    """A second `configure` is a no-op once the session is built."""
    auth = ASFAuth(ASFCredentials(token="EDL.X"))
    auth.configure()
    first_session = auth.session()
    auth.configure()
    assert auth.session() is first_session


@pytest.mark.asf
@pytest.mark.unit
def test_configure_raises_when_token_dict_missing(
    fake_asf_search, fake_earthdata_auth, monkeypatch
) -> None:
    """A login that returns no usable token surfaces a clear error."""

    class _BlankHandle:
        token = None

    def _patched_configure(self):
        self.configured = True
        self._auth.token = None

    monkeypatch.setattr(fake_earthdata_auth, "configure", _patched_configure)
    auth = ASFAuth(ASFCredentials(token="EDL.X"))
    with pytest.raises(AuthenticationError, match="no bearer token"):
        auth.configure()


@pytest.mark.asf
@pytest.mark.unit
def test_configure_raises_friendly_importerror_when_sdk_missing(
    monkeypatch, fake_earthdata_auth
) -> None:
    """Missing `asf_search` surfaces an actionable ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "asf_search":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    auth = ASFAuth(ASFCredentials())
    with pytest.raises(ImportError, match=r"earthlens\[asf\]"):
        auth.configure()


@pytest.mark.asf
@pytest.mark.unit
def test_asfsession_dead_ipv6_route_forces_ipv4_and_retries(
    fake_asf_search, fake_earthdata_auth, has_ipv6
) -> None:
    """An ENETUNREACH on the ASFSession dial forces IPv4 and retries once."""
    calls = {"n": 0}

    class _FlakySession(fake_asf_search.ASFSession):
        """An ASFSession whose first token exchange hits a dead IPv6 route."""

        def auth_with_token(self, token: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _enetunreach_error()
            return super().auth_with_token(token)

    fake_asf_search.ASFSession = _FlakySession
    auth = ASFAuth(ASFCredentials(token="EDL.HEAD.BODY"))
    auth.configure()
    assert auth.is_authenticated()
    assert calls["n"] == 2
    assert has_ipv6.HAS_IPV6 is False
