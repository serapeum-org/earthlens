"""Conformance tests for the NSI subpackage source files."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.nsi

pytestmark = pytest.mark.nsi

SUBPACKAGE = Path(earthlens.nsi.__file__).parent
SOURCES = sorted(SUBPACKAGE.glob("*.py"))


@pytest.mark.unit
@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
class TestSourceConformance:
    """Repo style contract for each `earthlens.nsi` source file."""

    def test_no_xarray_import(self, path: Path) -> None:
        """Geometry decode is pyramids'; the backend never imports xarray (`G5`)."""
        text = path.read_text(encoding="utf-8")
        assert "import xarray" not in text

    def test_future_annotations_first(self, path: Path) -> None:
        """Every module activates PEP 563 deferred annotations."""
        text = path.read_text(encoding="utf-8")
        assert "from __future__ import annotations" in text
