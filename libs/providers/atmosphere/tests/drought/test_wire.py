"""The real drought exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what drought actually sends back, so a
change in that shape fails here rather than in the weekly live lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path):
    """A recorded request replays into the backend's declared output kind."""
    result = EarthLens(
        data_source='drought',
        dataset='edo-spaST',
        variables=[],
        start='2025-12-21',
        end='2025-12-21',
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result is not None, "download never returns None"
    if isinstance(result, list):
        assert result, "a file-writing backend returns the paths it wrote"
        assert all(Path(p).exists() for p in result), "every path exists"
    else:
        assert hasattr(result, "columns"), "a tabular backend returns a frame"
