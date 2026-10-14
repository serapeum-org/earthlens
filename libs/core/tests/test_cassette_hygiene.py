"""Every committed cassette is checked for credentials, in CI and not by eye.

A cassette is a recording of a real exchange, so the one way it can go wrong
badly is by carrying a credential into the repository. The vcrpy filters
scrub what they were told about; this walks what actually landed on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.testing import assert_cassette_is_scrubbed

_ROOT = Path(__file__).resolve().parents[3]
_CASSETTES = sorted(_ROOT.glob("libs/providers/*/tests/*/cassettes/**/*.yaml"))
_IDS = [str(p.relative_to(_ROOT).as_posix()) for p in _CASSETTES]


@pytest.mark.skipif(not _CASSETTES, reason="no cassettes are committed yet")
@pytest.mark.parametrize("cassette", _CASSETTES, ids=_IDS)
def test_cassette_carries_no_credential(cassette):
    """A committed recording has no unscrubbed header or query credential."""
    assert_cassette_is_scrubbed(cassette)
