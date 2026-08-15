"""Tests for the GOES catalog-tooling handlers (`earthlens.goes.cli`).

Moved out of core's CLI test suite when the GOES handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.goes.cli as goes_cli

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the GOES structural lint."""

    def test_clean_catalog_passes(self):
        """A well-formed GOES product yields no issues."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None, "M1": None, "M2": None},
            datasets={
                "abi-l2-mcmip": SimpleNamespace(
                    product_group="ABI-L2-MCMIP",
                    domains=["C", "F"],
                    default_domain="C",
                    band_split=False,
                    bands=[],
                ),
            },
        )
        checked, issues = goes_cli.validator(catalog)
        assert (checked, issues) == (1, []), "a clean product lints clean"

    def test_flags_missing_product_group(self):
        """A GOES product missing product_group / domains is flagged (require branch)."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None},
            datasets={
                "bare": SimpleNamespace(
                    product_group="",
                    domains=[],
                    default_domain="C",
                    band_split=False,
                    bands=[],
                ),
            },
        )
        _checked, issues = goes_cli.validator(catalog)
        assert any("product_group" in i for i in issues), (
            "missing product_group flagged"
        )
        assert any("domains" in i for i in issues), "empty domains flagged"

    def test_flags_unknown_domain_and_empty_bands(self):
        """An unknown domain, a stray default, and empty band-split bands are flagged."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None},
            datasets={
                "bad": SimpleNamespace(
                    product_group="ABI-L2-BAD",
                    domains=["C", "Z"],
                    default_domain="F",
                    band_split=True,
                    bands=[],
                ),
            },
        )
        _checked, issues = goes_cli.validator(catalog)
        assert any("unknown domain" in i for i in issues), "bad domain flagged"
        assert any("default_domain" in i for i in issues), "stray default flagged"
        assert any("bands" in i for i in issues), "empty band-split bands flagged"
