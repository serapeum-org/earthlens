"""The real risk_indicators exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what risk_indicators actually sends back, so a
change in that shape fails here rather than in the weekly live lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.core import EarthLens


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path, monkeypatch):
    """A recorded request replays into the backend's declared output kind."""
    # A placeholder credential, because the backend refuses to build a
    # request without one and the real value is scrubbed from the
    # cassette. The replay never authenticates against anything.
    monkeypatch.setenv("GFW_API_KEY", "placeholder-key")

    result = EarthLens(
        data_source='gfw',
        variables=['gfw:admin_boundary'],
        country='KEN',
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result is not None, "download never returns None"
    if isinstance(result, list):
        assert result, "a file-writing backend returns the paths it wrote"
        assert all(Path(p).exists() for p in result), "every path exists"
    else:
        assert hasattr(result, "columns"), "a tabular backend returns a frame"
