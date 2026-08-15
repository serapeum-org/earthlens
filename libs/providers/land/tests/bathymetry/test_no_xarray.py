"""Guard that the bathymetry backend never imports xarray (the competitor rule)."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.bathymetry as bathymetry_pkg

pytestmark = pytest.mark.bathymetry


def test_no_xarray_import_in_src():
    """No source file under earthlens.bathymetry imports xarray."""
    package_dir = Path(bathymetry_pkg.__file__).parent
    offenders = [
        path.name
        for path in package_dir.rglob("*.py")
        if "import xarray" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"xarray imported in: {offenders}"
