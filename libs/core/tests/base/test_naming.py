from __future__ import annotations

import pytest

from earthlens.base import safe_filename


class TestSafeFilename:
    """Shared filesystem-safe filename sanitiser."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Dots and hyphens survive (real CMEMS / STAC ids).
            (
                "cmems_mod_glo_phy_my_0.083deg_P1D-m",
                "cmems_mod_glo_phy_my_0.083deg_P1D-m",
            ),
            ("planetary-computer/sentinel-2-l2a", "planetary-computer_sentinel-2-l2a"),
            # Path separators and Windows-illegal characters collapse to `_`.
            ("a/b\\c:d", "a_b_c_d"),
            ('a*b?c"d<e>f|g', "a_b_c_d_e_f_g"),
        ],
    )
    def test_sanitises_to_whitelist(self, raw: str, expected: str):
        """Only `A-Z a-z 0-9 . _ -` survive; everything else becomes `_`."""
        assert safe_filename(raw) == expected

    def test_runs_collapse_to_single_underscore(self):
        """A run of unsafe characters collapses to one `_`."""
        assert safe_filename("a///b") == "a_b"

    def test_leading_and_trailing_underscores_stripped(self):
        """Leading and trailing separators are trimmed."""
        assert safe_filename("/a/b/") == "a_b"
