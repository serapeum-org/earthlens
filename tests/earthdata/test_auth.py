"""Unit tests for EarthdataAuth / EarthdataCredentials."""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

import pytest

from earthlens.earthdata import (
    AuthenticationError,
    EarthdataAuth,
    EarthdataCredentials,
)

pytestmark = [pytest.mark.earthdata, pytest.mark.unit]

_REAL_IMPORT = builtins.__import__


def _block_earthaccess(name, *args, **kwargs):
    """Import hook that makes only `earthaccess` unimportable."""
    if name == "earthaccess":
        raise ImportError("no earthaccess")
    return _REAL_IMPORT(name, *args, **kwargs)


class TestEarthdataCredentials:
    """The frozen credentials value object."""

    def test_defaults_all_none(self):
        """Every field defaults to None."""
        creds = EarthdataCredentials()
        assert creds.username is None and creds.password is None
        assert creds.token is None and creds.netrc_path is None

    def test_password_is_secret(self):
        """The password is not echoed in repr."""
        creds = EarthdataCredentials(username="u", password="topsecret")
        assert "topsecret" not in repr(creds)
        assert creds.password.get_secret_value() == "topsecret"

    def test_frozen(self):
        """The model is immutable."""
        creds = EarthdataCredentials(username="u")
        with pytest.raises(Exception):
            creds.username = "other"


class TestEarthdataAuth:
    """Strategy resolution, configure(), s3_credentials()."""

    def test_strategy_environment(self, monkeypatch):
        """Env credentials select the environment strategy."""
        monkeypatch.setenv("EARTHDATA_USERNAME", "u")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "p")
        auth = EarthdataAuth(EarthdataCredentials())
        assert auth._resolve_strategy() == "environment"

    def test_strategy_netrc(self, monkeypatch, tmp_path):
        """A present netrc with no env creds selects the netrc strategy."""
        monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
        monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
        netrc = tmp_path / ".netrc"
        netrc.write_text("machine urs.earthdata.nasa.gov login u password p\n")
        auth = EarthdataAuth(EarthdataCredentials(netrc_path=netrc))
        assert auth._resolve_strategy() == "netrc"

    def test_strategy_interactive(self, monkeypatch):
        """No env and no netrc falls back to interactive."""
        monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
        monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
        auth = EarthdataAuth(EarthdataCredentials(netrc_path=Path("/no/such/netrc")))
        assert auth._resolve_strategy() == "interactive"

    def test_explicit_credentials_authenticate(self, fake_earthaccess, monkeypatch):
        """Explicit username/password authenticate even without env vars or netrc."""
        monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
        monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
        auth = EarthdataAuth(
            EarthdataCredentials(
                username="explicit-user",
                password="explicit-pass",
                netrc_path=Path("/no/such/netrc"),
            )
        )
        auth.configure()
        assert auth.is_authenticated() is True
        assert fake_earthaccess.login_calls == [
            {"strategy": "environment", "persist": True}
        ]
        assert os.environ["EARTHDATA_USERNAME"] == "explicit-user"
        assert os.environ["EARTHDATA_PASSWORD"] == "explicit-pass"

    def test_configure_logs_in(self, fake_earthaccess, edl_env):
        """configure() logs in via earthaccess and marks authenticated."""
        auth = EarthdataAuth(EarthdataCredentials())
        auth.configure()
        assert auth.is_authenticated() is True
        assert fake_earthaccess.login_calls == [
            {"strategy": "environment", "persist": True}
        ]

    def test_configure_idempotent(self, fake_earthaccess, edl_env):
        """A second configure() is a no-op (no second login)."""
        auth = EarthdataAuth(EarthdataCredentials())
        auth.configure()
        auth.configure()
        assert len(fake_earthaccess.login_calls) == 1

    def test_configure_unauthenticated_raises(self, fake_earthaccess, edl_env):
        """An unauthenticated handle raises AuthenticationError."""
        fake_earthaccess.authenticated = False
        auth = EarthdataAuth(EarthdataCredentials())
        with pytest.raises(AuthenticationError, match="no valid credentials"):
            auth.configure()

    def test_configure_login_exception_wrapped(self, fake_earthaccess, edl_env):
        """An earthaccess.login exception is wrapped as AuthenticationError."""
        fake_earthaccess.login_raises = RuntimeError("boom")
        auth = EarthdataAuth(EarthdataCredentials())
        with pytest.raises(AuthenticationError, match="EDL"):
            auth.configure()

    def test_configure_missing_extra_friendly(self, monkeypatch, edl_env):
        """A missing earthaccess raises a friendly ImportError naming the extra."""
        monkeypatch.delitem(sys.modules, "earthaccess", raising=False)
        monkeypatch.setattr("builtins.__import__", _block_earthaccess)
        auth = EarthdataAuth(EarthdataCredentials())
        with pytest.raises(ImportError, match=r"earthlens\[earthdata\]"):
            auth.configure()

    def test_s3_credentials_passes_provider_keyword(self, fake_earthaccess, edl_env):
        """s3_credentials forwards the provider by keyword, not positionally."""
        auth = EarthdataAuth(EarthdataCredentials())
        auth.configure()
        auth.s3_credentials("GES_DISC")
        assert fake_earthaccess._auth.s3_calls == [
            {"daac": None, "provider": "GES_DISC"}
        ]

    def test_s3_credentials_before_configure_raises(self):
        """Requesting S3 creds before configure() raises AuthenticationError."""
        auth = EarthdataAuth(EarthdataCredentials())
        with pytest.raises(AuthenticationError, match="before configure"):
            auth.s3_credentials("GES_DISC")

    def test_context_manager_configures(self, fake_earthaccess, edl_env):
        """The context-manager form configures on enter."""
        with EarthdataAuth(EarthdataCredentials()) as auth:
            assert auth.is_authenticated() is True
