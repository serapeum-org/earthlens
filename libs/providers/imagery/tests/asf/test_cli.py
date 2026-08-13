"""Tests for the ASF catalog-tooling validator (`earthlens.asf.cli`).

Moved out of core's CLI test suite when the validator moved into this
distribution (issue #863).
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from earthlens.asf.cli import validator

pytestmark = pytest.mark.cli


class TestValidator:
    """Tests for the ASF constant-name validator."""

    def test_flags_unknown_constant_names(self):
        """An asf row whose PLATFORM/PRODUCT_TYPE constant is gone is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad-row": SimpleNamespace(
                    platform="NOT_A_PLATFORM",
                    dataset=None,
                    product_type="NOT_A_TYPE",
                ),
            }
        )
        _checked, issues = validator(catalog)
        assert any("NOT_A_PLATFORM" in i for i in issues), "platform miss flagged"
        assert any("NOT_A_TYPE" in i for i in issues), "product_type miss flagged"

    def test_flags_unknown_dataset_constant(self):
        """An asf row whose DATASET constant is gone is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad-row": SimpleNamespace(
                    platform=None,
                    dataset="NOT_A_DATASET",
                    product_type="SLC",
                ),
            }
        )
        _checked, issues = validator(catalog)
        assert any("NOT_A_DATASET" in i for i in issues), "dataset miss flagged"

    def test_reports_zero_checked_when_sdk_missing(self, monkeypatch):
        """A missing `asf_search` returns checked=0 and an install-hint issue."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "asf_search":
                raise ImportError("missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        catalog = SimpleNamespace(
            datasets={"row-1": SimpleNamespace(), "row-2": SimpleNamespace()}
        )
        checked, issues = validator(catalog)
        assert checked == 0
        assert issues and "asf_search" in issues[0]
        assert "2 curated" in issues[0]
