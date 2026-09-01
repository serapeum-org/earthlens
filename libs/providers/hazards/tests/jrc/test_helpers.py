"""Unit tests for the JRC URL helper (no network)."""

from __future__ import annotations

import pytest

from earthlens.jrc import _helpers as h

pytestmark = pytest.mark.jrc


class TestEfhmUrl:
    """Tests for efhm_url."""

    def test_default_url(self):
        """efhm_url builds the verified RP100 URL."""
        assert h.efhm_url(100) == (
            "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/"
            "flood_hazard/Europe_RP100_filled_depth.tif"
        )

    def test_custom_base_and_template(self):
        """A custom base + template are honoured."""
        assert h.efhm_url(50, base_url="http://x", template="rp{rp}.tif") == (
            "http://x/rp50.tif"
        )
