"""Unit tests for `earthlens.cmems.auth`."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from earthlens.base import AbstractAuth
from earthlens.base import AuthenticationError as BaseAuthenticationError
from earthlens.cmems import AuthenticationError, CmemsAuth, CmemsCredentials


class _FakeCmemsModule(types.ModuleType):
    """Stub `copernicusmarine` module with just enough surface for auth tests."""

    InvalidUsernameOrPassword: type
    CouldNotConnectToAuthenticationSystem: type
    CredentialsCannotBeNone: type

    def __init__(self, login_result: object) -> None:
        super().__init__("copernicusmarine")
        self._login_result = login_result
        self.login_calls: list[dict[str, object]] = []
        self.InvalidUsernameOrPassword = type(
            "InvalidUsernameOrPassword", (Exception,), {}
        )
        self.CouldNotConnectToAuthenticationSystem = type(
            "CouldNotConnectToAuthenticationSystem", (Exception,), {}
        )
        self.CredentialsCannotBeNone = type("CredentialsCannotBeNone", (Exception,), {})

    def login(self, **kwargs: object) -> bool:
        self.login_calls.append(dict(kwargs))
        if isinstance(self._login_result, BaseException):
            raise self._login_result
        return bool(self._login_result)


def _install_fake_cmems(
    monkeypatch: pytest.MonkeyPatch, login_result: object
) -> _FakeCmemsModule:
    fake = _FakeCmemsModule(login_result)
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
    return fake


@pytest.mark.cmems
class TestCmemsCredentials:
    """Credentials value object behaviour."""

    def test_default_construction(self):
        """No-arg construction leaves every field None."""
        creds = CmemsCredentials()
        assert creds.username is None, (
            f"username should default to None, got {creds.username!r}"
        )
        assert creds.password is None, (
            f"password should default to None, got {creds.password!r}"
        )
        assert creds.credentials_file is None, "credentials_file should default to None"

    def test_password_is_secret(self):
        """SecretStr hides the password from repr."""
        creds = CmemsCredentials(username="u", password="pw")
        assert "pw" not in repr(creds), f"password leaked into repr: {repr(creds)}"
        assert creds.password.get_secret_value() == "pw", (
            "SecretStr.get_secret_value() must round-trip the plaintext"
        )

    def test_frozen(self):
        """CmemsCredentials is frozen — assignment after construction raises."""
        creds = CmemsCredentials(username="u")
        with pytest.raises(Exception, match="frozen|immutable") as exc_info:
            creds.username = "other"
        assert (
            "frozen" in str(exc_info.value).lower()
            or "immutable" in str(exc_info.value).lower()
        ), f"expected frozen-instance error, got {exc_info.value!r}"

    def test_credentials_file_path(self, tmp_path: Path):
        """credentials_file is stored as `pathlib.Path`."""
        target = tmp_path / "saved-creds"
        target.write_text("")
        creds = CmemsCredentials(credentials_file=target)
        assert creds.credentials_file == target, (
            f"credentials_file did not round-trip: got {creds.credentials_file!r}"
        )


@pytest.mark.cmems
class TestAuthenticationErrorInheritance:
    """`earthlens.cmems.AuthenticationError` participates in the C2 hierarchy."""

    def test_is_subclass_of_base(self):
        """The CMEMS auth error inherits from the cross-backend base."""
        assert issubclass(AuthenticationError, BaseAuthenticationError), (
            "earthlens.cmems.AuthenticationError must inherit from "
            "earthlens.base.AuthenticationError"
        )

    def test_message_preserved(self):
        """Constructor message is preserved on the standard exception."""
        exc = AuthenticationError("boom")
        assert str(exc) == "boom", f"message did not round-trip: {exc!s}"


@pytest.mark.cmems
class TestCmemsAuthAbstractAuthShape:
    """`CmemsAuth` satisfies the `AbstractAuth` contract."""

    def test_subclasses_abstract_auth(self):
        """`CmemsAuth` is an `AbstractAuth` subclass."""
        assert issubclass(CmemsAuth, AbstractAuth), (
            "CmemsAuth must inherit from earthlens.base.AbstractAuth"
        )

    def test_fresh_instance_not_authenticated(self):
        """A freshly built auth has not yet authenticated."""
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        assert auth.is_authenticated() is False, (
            "freshly constructed CmemsAuth should report is_authenticated() == False"
        )

    def test_configure_flips_predicate(self, monkeypatch: pytest.MonkeyPatch):
        """`configure()` sets `is_authenticated()` to True on success."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        auth.configure()
        assert auth.is_authenticated() is True, (
            "is_authenticated() must be True after a successful configure()"
        )
        assert len(fake.login_calls) == 1, (
            f"expected one login() call, got {len(fake.login_calls)}"
        )

    def test_configure_is_idempotent(self, monkeypatch: pytest.MonkeyPatch):
        """Second `configure()` is a no-op once authenticated."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        auth.configure()
        auth.configure()
        auth.configure()
        assert len(fake.login_calls) == 1, (
            f"login() should be called exactly once; got {len(fake.login_calls)}"
        )


@pytest.mark.cmems
class TestCmemsAuthErrorPaths:
    """Backend-specific exceptions are wrapped in `AuthenticationError`."""

    def test_invalid_credentials_wrapped(self, monkeypatch: pytest.MonkeyPatch):
        """`InvalidUsernameOrPassword` is re-raised as `AuthenticationError`."""
        fake = _install_fake_cmems(
            monkeypatch,
            login_result=Exception("placeholder"),
        )
        fake._login_result = fake.InvalidUsernameOrPassword("bad creds")
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        with pytest.raises(AuthenticationError, match="rejected") as exc_info:
            auth.configure()
        assert isinstance(exc_info.value.__cause__, fake.InvalidUsernameOrPassword), (
            "raw InvalidUsernameOrPassword must be preserved as __cause__"
        )

    def test_connection_error_wrapped(self, monkeypatch: pytest.MonkeyPatch):
        """Connectivity failures are wrapped with a network hint."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        fake._login_result = fake.CouldNotConnectToAuthenticationSystem("offline")
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        with pytest.raises(AuthenticationError, match="reach") as exc_info:
            auth.configure()
        assert isinstance(
            exc_info.value.__cause__, fake.CouldNotConnectToAuthenticationSystem
        )

    def test_empty_credentials_wrapped(self, monkeypatch: pytest.MonkeyPatch):
        """`CredentialsCannotBeNone` is wrapped with a "pass creds" hint."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        fake._login_result = fake.CredentialsCannotBeNone("empty")
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        with pytest.raises(AuthenticationError, match="empty credentials") as exc_info:
            auth.configure()
        assert isinstance(exc_info.value.__cause__, fake.CredentialsCannotBeNone)

    def test_login_returns_false_raises(self, monkeypatch: pytest.MonkeyPatch):
        """A `False` return without an exception is still surfaced."""
        _install_fake_cmems(monkeypatch, login_result=False)
        auth = CmemsAuth(CmemsCredentials(username="u", password="p"))
        with pytest.raises(AuthenticationError, match="False"):
            auth.configure()

    def test_no_credentials_no_env_no_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """No explicit creds, no env vars, no saved file → clean error before SDK call."""
        monkeypatch.delenv("COPERNICUSMARINE_SERVICE_USERNAME", raising=False)
        monkeypatch.delenv("COPERNICUSMARINE_SERVICE_PASSWORD", raising=False)
        # Redirect HOME so no real saved credentials are discovered.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials())
        with pytest.raises(
            AuthenticationError, match="no Copernicus Marine credentials"
        ):
            auth.configure()
        assert fake.login_calls == [], (
            "SDK login() must not be called when no creds resolve"
        )


@pytest.mark.cmems
class TestCmemsAuthCredentialResolution:
    """`configure()` follows the explicit > env > saved-file priority."""

    def test_explicit_username_password_pass_through(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Explicit username + password are forwarded verbatim to login()."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials(username="u", password="pw"))
        auth.configure()
        call = fake.login_calls[0]
        assert call["username"] == "u", f"username not forwarded; got {call!r}"
        assert call["password"] == "pw", f"password not forwarded; got {call!r}"
        assert call.get("check_credentials_valid") is True, (
            "check_credentials_valid must be True so login() actually validates"
        )

    def test_environment_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """When credentials are empty, env vars supply them."""
        monkeypatch.setenv("COPERNICUSMARINE_SERVICE_USERNAME", "env-user")
        monkeypatch.setenv("COPERNICUSMARINE_SERVICE_PASSWORD", "envpw")
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials())
        auth.configure()
        call = fake.login_calls[0]
        assert call["username"] == "env-user", (
            f"env username not picked up; got {call!r}"
        )
        assert call["password"] == "envpw", f"env password not picked up; got {call!r}"

    def test_credentials_file_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A pre-existing credentials_file is forwarded verbatim."""
        creds_file = tmp_path / ".copernicusmarine-credentials"
        creds_file.write_text("")
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        auth = CmemsAuth(CmemsCredentials(credentials_file=creds_file))
        auth.configure()
        assert fake.login_calls[0]["credentials_file"] == creds_file


@pytest.mark.cmems
class TestCmemsAuthContextManager:
    """The inherited context-manager protocol runs `configure()` on enter."""

    def test_context_manager_calls_configure(self, monkeypatch: pytest.MonkeyPatch):
        """`with CmemsAuth(creds) as auth:` authenticates on enter."""
        fake = _install_fake_cmems(monkeypatch, login_result=True)
        with CmemsAuth(CmemsCredentials(username="u", password="p")) as auth:
            assert auth.is_authenticated() is True, (
                "context manager entry should configure() the auth"
            )
        assert len(fake.login_calls) == 1, (
            f"expected one login() call inside the with-block, got {len(fake.login_calls)}"
        )
