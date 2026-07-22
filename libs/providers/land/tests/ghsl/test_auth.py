"""Unit tests for `earthlens.ghsl.auth`."""

from __future__ import annotations

import pytest
from earthlens.base.auth import AbstractAuth
from earthlens.ghsl.auth import GHSL_ATTRIBUTION, GhslAuth, GhslCredentials


@pytest.mark.ghsl
class TestGhslAuth:
    """No-op open-data auth conformance."""

    def test_is_abstract_auth_subclass(self):
        """`GhslAuth` is an `AbstractAuth` subclass (shape conformance)."""
        assert issubclass(GhslAuth, AbstractAuth)

    def test_default_credentials(self):
        """Constructing without creds defaults to an empty `GhslCredentials`."""
        auth = GhslAuth()
        assert isinstance(auth._creds, GhslCredentials)

    def test_is_authenticated_always_true(self):
        """`is_authenticated` is `True` even before `configure`."""
        assert GhslAuth().is_authenticated() is True

    def test_configure_is_idempotent_noop(self):
        """`configure` flips the internal flag and is safe to repeat."""
        auth = GhslAuth()
        auth.configure()
        auth.configure()
        assert auth._configured is True
        assert auth.is_authenticated() is True

    def test_context_manager(self):
        """The context-manager form configures on enter and is authenticated."""
        with GhslAuth() as auth:
            assert auth.is_authenticated() is True

    def test_explicit_credentials_stored(self):
        """An explicitly-passed credentials object is stored verbatim."""
        creds = GhslCredentials()
        assert GhslAuth(creds)._creds is creds


@pytest.mark.ghsl
class TestGhslCredentials:
    """The empty frozen credentials value object."""

    def test_is_frozen(self):
        """`GhslCredentials` is frozen (assignment raises)."""
        creds = GhslCredentials()
        with pytest.raises(Exception):
            creds.x = 1  # type: ignore[attr-defined]

    def test_equal_instances(self):
        """Two empty credentials compare equal."""
        assert GhslCredentials() == GhslCredentials()


@pytest.mark.ghsl
class TestAttribution:
    """The required attribution string."""

    def test_mentions_jrc_and_ghsl(self):
        """The attribution names the JRC and GHSL."""
        assert "JRC" in GHSL_ATTRIBUTION
        assert "Global Human Settlement Layer" in GHSL_ATTRIBUTION
