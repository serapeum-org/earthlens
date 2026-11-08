"""The real nrel exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what nrel actually sends back, so a
change in that shape fails here rather than in the weekly live lane.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from earthlens.core import EarthLens


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path, monkeypatch):
    """A recorded request replays into the backend's declared output kind."""
    # NREL carries its credential in the *query string*, which the cassette
    # scrubs to `REDACTED` — and the recorded query is part of what a replay
    # matches on. So with no real key present the placeholder must be that
    # same literal, or the replayed request would not match its own
    # recording. When a real key *is* exported the value passes through
    # untouched, which is what makes re-recording possible; the unit lane
    # exports no secrets, so there it is always the placeholder.
    for name in ("NREL_API_KEY", "NREL_EMAIL"):
        monkeypatch.setenv(name, os.environ.get(name) or "REDACTED")

    result = EarthLens(
        data_source='nrel',
        start='2020-01-01',
        end='2020-12-31',
        variables=['ghi', 'dni', 'dhi'],
        point=(39.74, -105.18),
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert result is not None, "download never returns None"
    if isinstance(result, list):
        assert result, "a file-writing backend returns the paths it wrote"
        assert all(Path(p).exists() for p in result), "every path exists"
    else:
        assert hasattr(result, "columns"), "a tabular backend returns a frame"
