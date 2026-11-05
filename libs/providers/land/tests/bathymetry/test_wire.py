"""Live end-to-end check that a real bathymetry exchange parses.

Gated behind the `e2e` + `bathymetry` markers plus a host-reachability skip.
It cannot run offline in the unit lane: `pyramids.Dataset.from_wcs` makes one
`GetCapabilities` probe over `urllib` (which a cassette can replay), then hands
the read to GDAL's native WCS driver, which runs its own capabilities and
coverage requests over a transport that a Python-level cassette (vcrpy) or
socket guard (pytest-socket) cannot intercept. So a recording replays only
that first probe, never the GDAL-driven read, and the test reaches the live
service either way. This pins the shape of what bathymetry actually sends
back; the sibling fake-based tests cover the error branches.

Run with:

    uv run --active pytest -m "e2e and bathymetry" libs/providers/land/tests/bathymetry
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.bathymetry]


@pytest.mark.requires_host("ows.emodnet-bathymetry.eu")
def test_a_real_request_parses(tmp_path):
    """A live request parses into the backend's declared output kind."""
    result = EarthLens(
        data_source='bathymetry',
        dataset='emodnet',
        lat_lim=[53.0, 54.0],
        lon_lim=[2.0, 3.0],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result is not None, "download never returns None"
    if isinstance(result, list):
        assert result, "a file-writing backend returns the paths it wrote"
        assert all(Path(p).exists() for p in result), "every path exists"
    else:
        assert hasattr(result, "columns"), "a tabular backend returns a frame"
