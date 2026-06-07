"""Unit tests for the radar tools (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "radar"
sys.path.insert(0, str(_TOOLS_DIR))

import audit_radar_catalog as audit  # noqa: E402

pytestmark = [pytest.mark.radar, pytest.mark.unit]


class TestFeedStations:
    """Tests for the live-feed station lister (over the fake S3 bucket)."""

    def test_lists_station_prefixes(self, fake_s3):
        """feed_stations returns the top-level station ids present in the feed."""
        assert audit.feed_stations() == {"KTLX"}
