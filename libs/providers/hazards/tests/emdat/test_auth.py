"""Tests for the EM-DAT Earthdata Login auth."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from earthlens.base import AuthenticationError
from earthlens.emdat import EmdatAuth, EmdatCredentials

_EDL_VARS = ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")


class _FakeAuth:
    """Stand-in for the handle `earthaccess.login` returns."""

    def __init__(self, authenticated: bool = True) -> None:
        """Record whether this handle claims to be authenticated."""
        self.authenticated = authenticated


class _FakeEarthaccess:
    """Stand-in for the `earthaccess` module's login surface."""

    def __init__(self, result: object | Exception) -> None:
        """Store what `login` should return or raise."""
        self.result = result
        self.calls: list[dict[str, object]] = []

    def login(self, **kwargs: object) -> object:
        """Record the call and return (or raise) the canned result."""
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def _clear_edl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove EDL environment variables so each test starts from nothing."""
    for var in _EDL_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def missing_netrc(tmp_path: Path) -> Path:
    """A path where no `.netrc` exists."""
    return tmp_path / "absent"


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeEarthaccess) -> None:
    """Put a fake `earthaccess` module in `sys.modules`."""
    monkeypatch.setitem(sys.modules, "earthaccess", fake)


@pytest.mark.emdat
class TestCredentials:
    """The credentials value object."""

    def test_defaults_are_empty(self) -> None:
        """Every field is optional so the environment can supply them."""
        creds = EmdatCredentials()
        assert (creds.username, creds.password, creds.token) == (None, None, None)

    def test_password_is_hidden_in_repr(self) -> None:
        """A `SecretStr` password never appears in the repr."""
        creds = EmdatCredentials(username="u", password=SecretStr("topsecret"))
        assert "topsecret" not in repr(creds)

    def test_token_is_hidden_in_repr(self) -> None:
        """A `SecretStr` token never appears in the repr."""
        secret = "supersecretbearervalue"
        assert secret not in repr(EmdatCredentials(token=SecretStr(secret)))

    def test_is_frozen(self) -> None:
        """Credentials are immutable once built."""
        creds = EmdatCredentials(username="u")
        with pytest.raises(Exception):
            creds.username = "other"


@pytest.mark.emdat
class TestStrategyResolution:
    """Which `earthaccess` login strategy each credential source selects."""

    def test_explicit_token_uses_environment(self, missing_netrc: Path) -> None:
        """An explicit token goes through the environment strategy."""
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        assert auth._resolve_strategy() == "environment"

    def test_explicit_pair_uses_environment(self, missing_netrc: Path) -> None:
        """An explicit username and password go through the environment."""
        auth = EmdatAuth(
            EmdatCredentials(
                username="u", password=SecretStr("p"), netrc_path=missing_netrc
            )
        )
        assert auth._resolve_strategy() == "environment"

    def test_username_without_password_is_not_enough(self, missing_netrc: Path) -> None:
        """A half-supplied pair falls through to the next source."""
        auth = EmdatAuth(EmdatCredentials(username="u", netrc_path=missing_netrc))
        assert auth._resolve_strategy() == "interactive"

    def test_env_token_uses_environment(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """`EARTHDATA_TOKEN` selects the environment strategy."""
        monkeypatch.setenv("EARTHDATA_TOKEN", "t")
        auth = EmdatAuth(EmdatCredentials(netrc_path=missing_netrc))
        assert auth._resolve_strategy() == "environment"

    def test_env_pair_uses_environment(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """`EARTHDATA_USERNAME` plus password selects the environment."""
        monkeypatch.setenv("EARTHDATA_USERNAME", "u")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "p")
        auth = EmdatAuth(EmdatCredentials(netrc_path=missing_netrc))
        assert auth._resolve_strategy() == "environment"

    def test_existing_netrc_uses_netrc(self, tmp_path: Path) -> None:
        """A present `.netrc` is preferred over prompting."""
        netrc = tmp_path / "netrc"
        netrc.write_text("machine urs.earthdata.nasa.gov login u password p\n")
        auth = EmdatAuth(EmdatCredentials(netrc_path=netrc))
        assert auth._resolve_strategy() == "netrc"

    def test_nothing_falls_back_to_interactive(self, missing_netrc: Path) -> None:
        """With no credential anywhere, the prompt is the last resort."""
        auth = EmdatAuth(EmdatCredentials(netrc_path=missing_netrc))
        assert auth._resolve_strategy() == "interactive"


@pytest.mark.emdat
class TestConfigure:
    """The login call and its failure modes."""

    def test_successful_login_marks_authenticated(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """A good login records the handle and flips the flag."""
        fake = _FakeEarthaccess(_FakeAuth(authenticated=True))
        _install_fake(monkeypatch, fake)
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        auth.configure()
        assert auth.is_authenticated() is True
        assert fake.calls[0]["persist"] is True

    def test_configure_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """A second configure does not log in again."""
        fake = _FakeEarthaccess(_FakeAuth(authenticated=True))
        _install_fake(monkeypatch, fake)
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        auth.configure()
        auth.configure()
        assert len(fake.calls) == 1

    def test_explicit_token_reaches_the_login(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """The token is in the environment while `earthaccess.login` runs."""
        seen: list[str | None] = []
        fake = _FakeEarthaccess(_FakeAuth())
        original = fake.login

        def record(**kwargs: object) -> object:
            seen.append(os.environ.get("EARTHDATA_TOKEN"))
            return original(**kwargs)

        fake.login = record  # type: ignore[method-assign]
        _install_fake(monkeypatch, fake)
        EmdatAuth(
            EmdatCredentials(token=SecretStr("tok"), netrc_path=missing_netrc)
        ).configure()
        assert seen == ["tok"]

    def test_credentials_do_not_linger_in_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """The exported credential is removed once the login has run."""
        _install_fake(monkeypatch, _FakeEarthaccess(_FakeAuth()))
        EmdatAuth(
            EmdatCredentials(token=SecretStr("tok"), netrc_path=missing_netrc)
        ).configure()
        assert "EARTHDATA_TOKEN" not in os.environ

    def test_a_pre_existing_value_is_restored(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """An env var the caller already set keeps its original value."""
        monkeypatch.setenv("EARTHDATA_TOKEN", "caller-owned")
        _install_fake(monkeypatch, _FakeEarthaccess(_FakeAuth()))
        EmdatAuth(
            EmdatCredentials(token=SecretStr("tok"), netrc_path=missing_netrc)
        ).configure()
        assert os.environ["EARTHDATA_TOKEN"] == "caller-owned"

    def test_explicit_pair_reaches_the_login(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """The username and password are set while `earthaccess.login` runs."""
        seen: list[tuple[str | None, str | None]] = []
        fake = _FakeEarthaccess(_FakeAuth())
        original = fake.login

        def record(**kwargs: object) -> object:
            seen.append(
                (
                    os.environ.get("EARTHDATA_USERNAME"),
                    os.environ.get("EARTHDATA_PASSWORD"),
                )
            )
            return original(**kwargs)

        fake.login = record  # type: ignore[method-assign]
        _install_fake(monkeypatch, fake)
        EmdatAuth(
            EmdatCredentials(
                username="u", password=SecretStr("p"), netrc_path=missing_netrc
            )
        ).configure()
        assert seen == [("u", "p")]

    def test_empty_env_vars_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """An empty EDL var is removed so it cannot mask a real credential."""
        monkeypatch.setenv("EARTHDATA_TOKEN", "")
        _install_fake(monkeypatch, _FakeEarthaccess(_FakeAuth()))
        auth = EmdatAuth(EmdatCredentials(netrc_path=missing_netrc))
        auth.configure()
        import os

        assert os.environ.get("EARTHDATA_TOKEN") != ""

    def test_unauthenticated_handle_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """A handle that reports failure becomes an AuthenticationError."""
        _install_fake(monkeypatch, _FakeEarthaccess(_FakeAuth(authenticated=False)))
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        with pytest.raises(AuthenticationError, match="no valid credentials"):
            auth.configure()

    def test_error_names_the_eula_step(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """The failure text points at the data-use agreement, a common blocker."""
        _install_fake(monkeypatch, _FakeEarthaccess(_FakeAuth(authenticated=False)))
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        with pytest.raises(AuthenticationError, match="unaccepted_eulas"):
            auth.configure()

    def test_login_exception_is_wrapped(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """An exception from `earthaccess` surfaces as an AuthenticationError."""
        _install_fake(monkeypatch, _FakeEarthaccess(RuntimeError("boom")))
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        with pytest.raises(AuthenticationError, match="boom"):
            auth.configure()

    def test_missing_earthaccess_names_the_extra(
        self, monkeypatch: pytest.MonkeyPatch, missing_netrc: Path
    ) -> None:
        """Without the SDK the error tells the user which extra to install."""
        monkeypatch.setitem(sys.modules, "earthaccess", None)
        auth = EmdatAuth(
            EmdatCredentials(token=SecretStr("t"), netrc_path=missing_netrc)
        )
        with pytest.raises(ImportError, match=r"earthlens\[emdat\]"):
            auth.configure()
