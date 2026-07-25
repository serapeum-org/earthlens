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

from earthlens.base import AbstractAuth, AuthenticationError


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
