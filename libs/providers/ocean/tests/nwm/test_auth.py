"""Unit tests for the NWM auth placeholder."""

from __future__ import annotations

import pytest

from earthlens.nwm.auth import requires_auth

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


def test_requires_auth_is_false():
    """The NWM bucket is anonymous, so no credentials are required."""
    assert requires_auth() is False
