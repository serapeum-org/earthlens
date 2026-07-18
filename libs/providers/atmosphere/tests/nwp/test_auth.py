"""Unit tests for the NWP auth placeholder."""

from __future__ import annotations

import pytest

from earthlens.nwp.auth import requires_auth

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


class TestRequiresAuth:
    """Tests for requires_auth."""

    def test_returns_false(self):
        """Every MVP NWP centre is an open bucket, so no auth is required."""
        assert requires_auth() is False
