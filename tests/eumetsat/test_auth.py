"""Unit tests for `EumetsatAuth` credential resolution and token minting."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from earthlens.base import AuthenticationError as BaseAuthenticationError
from earthlens.eumetsat.auth import (
    AuthenticationError,
    EumetsatAuth,
    EumetsatCredentials,
)

pytestmark = pytest.mark.eumetsat


def test_credentials_secret_hidden_in_repr():
    """The consumer secret is a SecretStr and never appears in repr."""
    creds = EumetsatCredentials(consumer_key="k", consumer_secret="topsecret")
    assert "topsecret" not in repr(creds)


def test_resolve_pair_prefers_env(monkeypatch):
    """Environment variables win over constructor kwargs."""
    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "envkey")
    monkeypatch.setenv("EUMETSAT_CONSUMER_SECRET", "envsecret")
    auth = EumetsatAuth(
        EumetsatCredentials(consumer_key="argkey", consumer_secret="argsecret")
    )
    assert auth._resolve_pair() == ("envkey", "envsecret")


def test_resolve_pair_falls_back_to_kwargs():
    """With no env, the constructor kwargs supply the pair."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    assert auth._resolve_pair() == ("k", "s")


def test_resolve_pair_reads_credentials_file(tmp_path):
    """A `key,secret` credentials file is parsed when env/kwargs are absent."""
    cred_file = tmp_path / "credentials"
    cred_file.write_text("filekey,filesecret\n", encoding="utf-8")
    auth = EumetsatAuth(EumetsatCredentials(credentials_file=cred_file))
    assert auth._resolve_pair() == ("filekey", "filesecret")


def test_resolve_pair_uses_eumdac_config_dir_env(tmp_path, monkeypatch):
    """`EUMDAC_CONFIG_DIR` redirects the default credentials-file lookup."""
    (tmp_path / "credentials").write_text("dirkey,dirsecret", encoding="utf-8")
    monkeypatch.setenv("EUMDAC_CONFIG_DIR", str(tmp_path))
    auth = EumetsatAuth(EumetsatCredentials())
    assert auth._resolve_pair() == ("dirkey", "dirsecret")


def test_missing_credentials_file_resolves_to_none(tmp_path):
    """An absent credentials file yields a `(None, None)` pair, not an error."""
    auth = EumetsatAuth(EumetsatCredentials(credentials_file=tmp_path / "nope"))
    assert auth._resolve_pair() == (None, None)


def test_malformed_credentials_file_resolves_to_none(tmp_path):
    """A credentials file without a `key,secret` line is ignored."""
    cred_file = tmp_path / "credentials"
    cred_file.write_text("not a valid line", encoding="utf-8")
    auth = EumetsatAuth(EumetsatCredentials(credentials_file=cred_file))
    assert auth._resolve_pair() == (None, None)


def test_configure_mints_token(fake_eumdac):
    """A resolved pair mints an `AccessToken` and flips is_authenticated."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    assert auth.is_authenticated() is False
    auth.configure()
    assert auth.is_authenticated() is True
    assert fake_eumdac.tokens[0].credentials == ("k", "s")


def test_configure_is_idempotent(fake_eumdac):
    """A second configure() after success mints no new token."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    auth.configure()
    auth.configure()
    assert len(fake_eumdac.tokens) == 1


def test_configure_without_credentials_raises(fake_eumdac):
    """No resolvable pair raises an AuthenticationError naming the api-key URL."""
    auth = EumetsatAuth(EumetsatCredentials())
    with pytest.raises(AuthenticationError, match="api.eumetsat.int/api-key"):
        auth.configure()


def test_authentication_error_is_cross_backend():
    """The backend AuthenticationError subclasses the base one."""
    assert issubclass(AuthenticationError, BaseAuthenticationError)


def test_expired_token_reports_not_authenticated(fake_eumdac):
    """A token whose expiration has passed makes is_authenticated False."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    auth.configure()
    fake_eumdac.tokens[0].expiration = datetime.now() - timedelta(seconds=1)
    assert auth.is_authenticated() is False


def test_datastore_before_configure_raises(fake_eumdac):
    """datastore() before configure() raises AuthenticationError."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    with pytest.raises(AuthenticationError, match="before configure"):
        auth.datastore()


def test_datastore_and_datatailor_after_configure(fake_eumdac):
    """datastore() / datatailor() build clients from the live token."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    auth.configure()
    assert auth.datastore() is fake_eumdac.store
    assert auth.datatailor().token is fake_eumdac.tokens[0]


def test_context_manager_configures(fake_eumdac):
    """The context-manager form authenticates on enter."""
    with EumetsatAuth(
        EumetsatCredentials(consumer_key="k", consumer_secret="s")
    ) as auth:
        assert auth.is_authenticated() is True


def test_configure_without_eumdac_raises_import_error(monkeypatch):
    """A missing `eumdac` surfaces a friendly ImportError naming the extra."""
    import sys

    monkeypatch.setitem(sys.modules, "eumdac", None)
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    with pytest.raises(ImportError, match=r"earthlens\[eumetsat\]"):
        auth.configure()


def test_configure_wraps_token_failure(fake_eumdac, monkeypatch):
    """A token-minting failure is wrapped as AuthenticationError."""

    def _boom(*_a, **_k):
        raise RuntimeError("server said no")

    monkeypatch.setattr(fake_eumdac, "AccessToken", _boom)
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    with pytest.raises(AuthenticationError, match="token request failed"):
        auth.configure()


def test_is_authenticated_unreadable_expiration_treated_live(fake_eumdac):
    """A token whose expiration cannot be compared is treated as live."""

    class _Unreadable:
        @property
        def expiration(self):
            raise ValueError("no expiry")

    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    auth._token = _Unreadable()
    assert auth.is_authenticated() is True


def test_is_authenticated_propagates_unexpected_error(fake_eumdac):
    """An unexpected error reading `expiration` is not swallowed."""

    class _Broken:
        @property
        def expiration(self):
            raise RuntimeError("unexpected")

    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    auth._token = _Broken()
    with pytest.raises(RuntimeError, match="unexpected"):
        auth.is_authenticated()


def test_datatailor_before_configure_raises(fake_eumdac):
    """datatailor() before configure() raises AuthenticationError."""
    auth = EumetsatAuth(EumetsatCredentials(consumer_key="k", consumer_secret="s"))
    with pytest.raises(AuthenticationError, match="before configure"):
        auth.datatailor()
