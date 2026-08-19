"""Tests for the C2 `AbstractAuth` ABC + cross-backend `AuthenticationError`.

Covers:
- `AuthenticationError` is a flat `Exception` subclass that preserves
  its message.
- `AbstractAuth` cannot be instantiated without overriding both abstract
  methods.
- A minimal concrete subclass stores its credentials, exposes idempotent
  `configure()`, and works as a context manager (`__enter__` calls
  `configure`, `__exit__` calls `close`).
- The default `close()` is a no-op (subclasses override only when they
  hold a closeable resource).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base import AbstractAuth, AuthenticationError, SingleSecretAuth


class _Creds(BaseModel):
    """Minimal credentials value object used by the test subclasses."""

    model_config = ConfigDict(frozen=True)

    token: SecretStr
    project: str | None = None


class _CountingAuth(AbstractAuth[_Creds]):
    """Concrete `AbstractAuth` subclass that counts configure/close calls."""

    def __init__(self, creds: _Creds) -> None:
        super().__init__(creds)
        self.configure_calls = 0
        self.close_calls = 0
        self._authed = False

    def configure(self) -> None:
        if self.is_authenticated():
            return  # idempotent — match the AbstractAuth contract
        self.configure_calls += 1
        self._authed = True

    def is_authenticated(self) -> bool:
        return self._authed

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.unit
class TestAuthenticationError:
    """The cross-backend `AuthenticationError` is a flat `Exception`."""

    def test_is_exception_subclass(self):
        """`AuthenticationError` is a plain `Exception` subclass."""
        assert issubclass(AuthenticationError, Exception)

    def test_message_preserved(self):
        """`str(exc)` returns the original constructor message."""
        assert str(AuthenticationError("boom")) == "boom"

    def test_can_be_raised_and_caught(self):
        """A raised `AuthenticationError` is caught by `except AuthenticationError`."""
        with pytest.raises(AuthenticationError, match="creds"):
            raise AuthenticationError("missing creds")


@pytest.mark.unit
class TestAbstractAuth:
    """The `AbstractAuth` ABC contract — abstractness + ctx-manager wiring."""

    def test_cannot_instantiate_when_methods_missing(self):
        """A subclass that omits the abstract methods is itself abstract."""

        class _Partial(AbstractAuth[_Creds]):
            pass

        with pytest.raises(TypeError, match="abstract"):
            _Partial(_Creds(token="x"))

    def test_concrete_subclass_stores_credentials(self):
        """`__init__(creds)` stashes the value object on `self._creds`."""
        creds = _Creds(token="abc", project="p")
        auth = _CountingAuth(creds)
        assert auth._creds is creds

    def test_pre_configure_not_authenticated(self):
        """A freshly constructed instance is not yet authenticated."""
        auth = _CountingAuth(_Creds(token="x"))
        assert auth.is_authenticated() is False
        assert auth.configure_calls == 0

    def test_configure_flips_authenticated(self):
        """A successful `configure()` makes `is_authenticated()` return True."""
        auth = _CountingAuth(_Creds(token="x"))
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.configure_calls == 1

    def test_configure_is_idempotent(self):
        """Calling `configure()` twice does the real work only once."""
        auth = _CountingAuth(_Creds(token="x"))
        auth.configure()
        auth.configure()
        assert auth.configure_calls == 1, (
            f"configure should be idempotent; got {auth.configure_calls} calls"
        )

    def test_context_manager_enter_calls_configure(self):
        """`with auth as a` calls `configure()` on entry and returns `auth`."""
        auth = _CountingAuth(_Creds(token="x"))
        with auth as a:
            assert a is auth
            assert auth.is_authenticated() is True

    def test_context_manager_exit_calls_close(self):
        """`__exit__` calls `close()` exactly once on normal exit."""
        auth = _CountingAuth(_Creds(token="x"))
        with auth:
            pass
        assert auth.close_calls == 1

    def test_context_manager_calls_close_on_exception(self):
        """`__exit__` still calls `close()` when the block raises."""
        auth = _CountingAuth(_Creds(token="x"))
        with pytest.raises(RuntimeError), auth:
            raise RuntimeError("boom")
        assert auth.close_calls == 1

    def test_default_close_is_a_no_op(self):
        """A subclass that does not override `close()` has a working no-op."""

        class _NoCloseAuth(AbstractAuth[_Creds]):
            def configure(self):
                pass

            def is_authenticated(self):
                return True

        auth = _NoCloseAuth(_Creds(token="x"))
        # Must not raise:
        auth.close()
        with auth:
            pass


class TestProviderErrorHierarchy:
    """ARC-5: the per-provider `AuthenticationError` subclasses are deliberate.

    Each provider re-exports a zero-body subclass of the base error. That looked
    like duplication, but it is the documented design: the base is what you catch
    to handle *any* backend's auth failure, and the provider name is what you
    catch to single one out — and it keeps existing
    `except earthlens.gee.AuthenticationError` consumers working.

    Pinned here because that intent lives only in docstrings, so a future tidy-up
    could collapse the hierarchy without anything objecting.
    """

    def _provider_error_classes(self):
        """Import each provider's `AuthenticationError`, skipping absent SDKs."""
        import importlib

        found = {}
        for module in (
            "earthlens.airnow.auth",
            "earthlens.firms.auth",
            "earthlens.gee.auth",
            "earthlens.iucn.auth",
            "earthlens.nrel.auth",
            "earthlens.openaq.auth",
            "earthlens.wdpa.auth",
        ):
            try:
                found[module] = importlib.import_module(module).AuthenticationError
            except (ImportError, AttributeError):  # optional SDK not installed
                continue
        return found

    def test_provider_errors_were_found(self):
        """Guard the guard: at least a few provider modules must be importable."""
        found = self._provider_error_classes()
        # 7 provider error modules import today; a floor of 3 would still
        # pass with four of them gone.
        assert len(found) >= 6, f"only imported {sorted(found)}"

    def test_one_base_clause_catches_every_provider(self):
        """The broad catch works: each provider error is a base subclass."""
        from earthlens.base import AuthenticationError

        offenders = [
            module
            for module, cls in self._provider_error_classes().items()
            if not issubclass(cls, AuthenticationError)
        ]
        assert offenders == [], (
            f"these no longer subclass the base error, so one `except "
            f"AuthenticationError` clause would miss them: {offenders}"
        )

    def test_each_provider_error_is_distinct(self):
        """The narrow catch works: no two providers share one error class.

        If these were collapsed into re-exports of the base, catching one
        provider's failure would silently catch every provider's.
        """
        from earthlens.base import AuthenticationError

        classes = self._provider_error_classes()
        aliased = [
            module for module, cls in classes.items() if cls is AuthenticationError
        ]
        assert aliased == [], (
            f"these are the base class itself, so a narrow `except` cannot "
            f"single out one provider: {aliased}"
        )
        assert len(set(classes.values())) == len(classes), (
            f"two providers share an error class: {sorted(classes)}"
        )


class _SecretCreds(BaseModel):
    """Credentials value object carrying one optional secret."""

    model_config = ConfigDict(frozen=True)

    token: SecretStr | None = None


class _DemoError(AuthenticationError):
    """A provider-specific error so the raised type can be asserted."""


class _DemoAuth(SingleSecretAuth[_SecretCreds]):
    """Minimal single-secret auth resolving `DEMO_A` then `DEMO_B`."""

    ENV_VARS = ("DEMO_A", "DEMO_B")
    PROVIDER = "Demo"
    CREDENTIAL_ARG = "demo_key"
    CREDENTIAL_HINT = "Get one at example.test."
    AUTH_ERROR = _DemoError

    def _explicit_credential(self) -> str | None:
        """Unwrap the explicit token off the credentials, if present."""
        token = self._creds.token
        return token.get_secret_value() if token is not None else None

    def _connect(self, credential: str) -> None:
        """Record the resolved secret and count the calls."""
        self.connected = getattr(self, "connected", 0) + 1
        self.secret = credential


class TestSingleSecretAuth:
    """The shared explicit -> env -> raise -> memoise ceremony."""

    def test_explicit_credential_wins(self, monkeypatch):
        """An explicit credential is used even when an env var is also set."""
        monkeypatch.setenv("DEMO_A", "from-env")
        auth = _DemoAuth(_SecretCreds(token="explicit"))
        auth.configure()
        assert auth.secret == "explicit", auth.secret

    def test_env_fallback_in_priority_order(self, monkeypatch):
        """With no explicit value, the first set ENV_VAR (in order) wins."""
        monkeypatch.delenv("DEMO_A", raising=False)
        monkeypatch.setenv("DEMO_B", "second")
        auth = _DemoAuth(_SecretCreds())
        auth.configure()
        assert auth.secret == "second", auth.secret

    def test_first_env_var_takes_priority(self, monkeypatch):
        """DEMO_A is preferred over DEMO_B when both are set."""
        monkeypatch.setenv("DEMO_A", "first")
        monkeypatch.setenv("DEMO_B", "second")
        auth = _DemoAuth(_SecretCreds())
        auth.configure()
        assert auth.secret == "first", auth.secret

    def test_missing_raises_provider_error_with_consistent_message(self, monkeypatch):
        """No credential raises the provider's error type, naming provider+vars+hint."""
        monkeypatch.delenv("DEMO_A", raising=False)
        monkeypatch.delenv("DEMO_B", raising=False)
        auth = _DemoAuth(_SecretCreds())
        with pytest.raises(_DemoError) as exc_info:
            auth.configure()
        message = str(exc_info.value)
        assert "Demo" in message, message
        assert "pass demo_key=" in message, message
        assert "DEMO_A or DEMO_B" in message, message
        assert "Get one at example.test." in message, message

    def test_missing_error_is_catchable_as_base(self, monkeypatch):
        """The provider error is still a base AuthenticationError for the broad catch."""
        monkeypatch.delenv("DEMO_A", raising=False)
        monkeypatch.delenv("DEMO_B", raising=False)
        auth = _DemoAuth(_SecretCreds())
        with pytest.raises(AuthenticationError):
            auth.configure()

    def test_configure_is_idempotent(self, monkeypatch):
        """A second configure() after success does not re-connect."""
        monkeypatch.setenv("DEMO_A", "x")
        auth = _DemoAuth(_SecretCreds())
        auth.configure()
        auth.configure()
        assert auth.connected == 1, auth.connected
        assert auth.is_authenticated() is True

    def test_empty_explicit_credential_raises_without_env_fallback(self, monkeypatch):
        """A present-but-empty explicit secret raises and never consults the env.

        Only an absent (None) explicit credential falls back to ENV_VARS; an
        empty one is an error, matching each backend's original behaviour.
        """
        monkeypatch.setenv("DEMO_A", "from-env")
        auth = _DemoAuth(_SecretCreds(token=""))
        with pytest.raises(_DemoError) as exc_info:
            auth.configure()
        assert "no Demo credential available" in str(exc_info.value), exc_info.value
        assert getattr(auth, "connected", 0) == 0, "must not connect on empty secret"


class _EnvOnlyAuth(SingleSecretAuth[_SecretCreds]):
    """A single-secret auth that keeps the base's default `_explicit_credential`.

    It declares only an env var and provider name and no `CREDENTIAL_HINT`, so it
    exercises the inherited `_explicit_credential` (always `None`, env-only) and
    the missing-credential message with no trailing hint sentence.
    """

    ENV_VARS = ("ENVONLY_VAR",)
    PROVIDER = "EnvOnly"

    def _connect(self, credential: str) -> None:
        """Record the resolved secret."""
        self.secret = credential


class TestSingleSecretAuthDefaults:
    """The base defaults: env-only resolution and the hint-free error message."""

    def test_env_resolution_uses_the_inherited_explicit_credential(self, monkeypatch):
        """With no `_explicit_credential` override, the env var supplies the secret."""
        monkeypatch.setenv("ENVONLY_VAR", "from-env")
        auth = _EnvOnlyAuth(_SecretCreds(token="ignored-because-not-read"))
        auth.configure()
        assert auth.secret == "from-env", auth.secret

    def test_missing_credential_without_hint_omits_the_hint_sentence(self, monkeypatch):
        """A backend with no CREDENTIAL_HINT raises the bare provider-named message."""
        monkeypatch.delenv("ENVONLY_VAR", raising=False)
        auth = _EnvOnlyAuth(_SecretCreds())
        with pytest.raises(AuthenticationError) as exc_info:
            auth.configure()
        message = str(exc_info.value)
        assert (
            message == "no EnvOnly credential available: pass it explicitly or set "
            "ENVONLY_VAR."
        ), message
