"""Unit tests for `earthlens.openeo.auth` (OIDC flow selection)."""

from __future__ import annotations

import sys

import pytest

from earthlens.openeo import auth as auth_mod
from earthlens.openeo.auth import (
    AuthenticationError,
    OpeneoAuth,
    OpeneoCredentials,
)

from .conftest import FakeConnection, FakeOpeneoModule


@pytest.mark.openeo
class TestOpeneoCredentials:
    """`OpeneoCredentials` carries secrets and reads the environment."""

    def test_empty_is_interactive(self):
        """An empty credentials object has no client id (interactive flow)."""
        assert OpeneoCredentials().client_id is None

    def test_secret_is_hidden_then_readable(self):
        """The client secret is a SecretStr, readable via get_secret_value."""
        creds = OpeneoCredentials(client_id="svc", client_secret="shh")
        assert creds.client_secret.get_secret_value() == "shh"

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch):
        """`from_env` reads the OPENEO_* variables."""
        monkeypatch.setenv("OPENEO_CLIENT_ID", "cid")
        monkeypatch.setenv("OPENEO_CLIENT_SECRET", "csec")
        monkeypatch.delenv("OPENEO_REFRESH_TOKEN", raising=False)
        monkeypatch.setenv("OPENEO_PROVIDER_ID", "egi")
        creds = OpeneoCredentials.from_env()
        assert creds.client_id == "cid"
        assert creds.client_secret.get_secret_value() == "csec"
        assert creds.provider_id == "egi"

    def test_from_env_all_absent(self, monkeypatch: pytest.MonkeyPatch):
        """With no OPENEO_* vars, every field is None."""
        for var in (
            "OPENEO_CLIENT_ID",
            "OPENEO_CLIENT_SECRET",
            "OPENEO_REFRESH_TOKEN",
            "OPENEO_PROVIDER_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        creds = OpeneoCredentials.from_env()
        assert creds.client_id is None and creds.refresh_token is None


@pytest.mark.openeo
class TestOpeneoAuth:
    """`OpeneoAuth` selects the flow by which credentials are present."""

    def test_endpoint_resolved_at_construction(self):
        """A named endpoint alias is resolved eagerly."""
        assert OpeneoAuth(endpoint="cdse").endpoint == (
            "https://openeo.dataspace.copernicus.eu"
        )

    def test_not_authenticated_before_configure(self):
        """A fresh instance is not authenticated until configure runs."""
        assert OpeneoAuth().is_authenticated() is False

    def test_interactive_flow(self, monkeypatch: pytest.MonkeyPatch):
        """With no credentials, the interactive device flow is used."""
        conn = FakeConnection()
        monkeypatch.setattr(auth_mod, "import_openeo", lambda: FakeOpeneoModule(conn))
        auth = OpeneoAuth(OpeneoCredentials(provider_id="egi"))
        auth.configure()
        assert auth.is_authenticated()
        assert ("authenticate_oidc", "egi") in conn.log

    def test_client_credentials_flow(self, monkeypatch: pytest.MonkeyPatch):
        """A client id + secret selects the client-credentials flow."""
        conn = FakeConnection()
        monkeypatch.setattr(auth_mod, "import_openeo", lambda: FakeOpeneoModule(conn))
        auth = OpeneoAuth(OpeneoCredentials(client_id="svc", client_secret="sec"))
        auth.configure()
        assert ("client_credentials", "svc", "sec", None) in conn.log

    def test_refresh_token_flow(self, monkeypatch: pytest.MonkeyPatch):
        """A refresh token selects the refresh-token flow."""
        conn = FakeConnection()
        monkeypatch.setattr(auth_mod, "import_openeo", lambda: FakeOpeneoModule(conn))
        auth = OpeneoAuth(OpeneoCredentials(refresh_token="rt"))
        auth.configure()
        assert any(entry[0] == "refresh_token" for entry in conn.log)

    def test_configure_is_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        """A second configure call does no further auth work."""
        conn = FakeConnection()
        monkeypatch.setattr(auth_mod, "import_openeo", lambda: FakeOpeneoModule(conn))
        auth = OpeneoAuth()
        auth.configure()
        auth.configure()
        assert sum(1 for e in conn.log if e[0] == "authenticate_oidc") == 1

    def test_connection_configures_on_first_use(self, monkeypatch: pytest.MonkeyPatch):
        """`connection()` configures lazily and returns the connection."""
        conn = FakeConnection()
        monkeypatch.setattr(auth_mod, "import_openeo", lambda: FakeOpeneoModule(conn))
        auth = OpeneoAuth()
        assert auth.connection() is conn
        assert auth.is_authenticated()

    def test_failure_wrapped_as_authentication_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An SDK auth failure is wrapped as AuthenticationError."""

        class _BoomConn(FakeConnection):
            def authenticate_oidc(self, provider_id=None):
                raise RuntimeError("device flow timed out")

        monkeypatch.setattr(
            auth_mod, "import_openeo", lambda: FakeOpeneoModule(_BoomConn())
        )
        with pytest.raises(AuthenticationError, match="OIDC authentication failed"):
            OpeneoAuth().configure()

    def test_missing_extra_raises_import_error(self, monkeypatch: pytest.MonkeyPatch):
        """With openeo absent, configure surfaces the friendly ImportError."""
        monkeypatch.setitem(sys.modules, "openeo", None)
        with pytest.raises(ImportError, match=r"earthlens\[openeo\]"):
            OpeneoAuth().configure()

    def test_authentication_error_is_base_subclass(self):
        """The backend AuthenticationError subclasses the cross-backend one."""
        from earthlens.base import AuthenticationError as BaseAuthError

        assert issubclass(AuthenticationError, BaseAuthError)
