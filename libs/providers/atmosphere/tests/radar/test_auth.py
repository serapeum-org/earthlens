"""Unit tests for the radar auth placeholder."""

from __future__ import annotations

import pytest

from earthlens.radar.auth import requires_auth

pytestmark = [pytest.mark.radar, pytest.mark.unit]


def test_requires_auth_false():
    """The NEXRAD chunk bucket is anonymous, so no auth is required."""
    assert requires_auth() is False
