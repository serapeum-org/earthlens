"""Tests for the no-op WorldPop auth and the backend output kind."""

from __future__ import annotations

import pytest
from earthlens.base.auth import AbstractAuth

from earthlens.worldpop import (
    WORLDPOP_ATTRIBUTION,
    WorldPopAuth,
    WorldPopCredentials,
)

pytestmark = pytest.mark.worldpop


def test_auth_is_abstractauth_subclass():
    """WorldPopAuth conforms to the shared AbstractAuth shape."""
    assert issubclass(WorldPopAuth, AbstractAuth)


def test_auth_always_authenticated():
    """is_authenticated() is True with nothing to configure."""
    auth = WorldPopAuth()
    assert auth.is_authenticated() is True


def test_configure_is_idempotent_noop():
    """configure() is a no-op that stays authenticated."""
    auth = WorldPopAuth()
    auth.configure()
    auth.configure()
    assert auth.is_authenticated() is True


def test_context_manager_configures():
    """The context-manager form enters authenticated."""
    with WorldPopAuth() as auth:
        assert auth.is_authenticated() is True


def test_credentials_are_empty():
    """The credentials value object carries no fields."""
    assert WorldPopCredentials().model_dump() == {}


def test_attribution_mentions_worldpop():
    """The attribution string names WorldPop and CC-BY-4.0."""
    assert "WorldPop" in WORLDPOP_ATTRIBUTION
    assert "CC-BY-4.0" in WORLDPOP_ATTRIBUTION


def test_output_kind_is_mixed():
    """The backend declares the mixed output kind."""
    from earthlens.worldpop import WorldPop

    assert WorldPop.OUTPUT_KIND == "mixed"
