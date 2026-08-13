"""Tests for the FDSN catalog-tooling handlers (`earthlens.fdsn.cli`).

Moved out of core's CLI test suite when the FDSN refresh/validate handlers moved
into this distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.fdsn.cli as fdsn_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import refresh_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the fdsn backend."""
    return next(b for b in list_backends() if b.provider == "fdsn")


class TestRefresher:
    """Tests for the FDSN (obspy URL_MAPPINGS) lister."""

    def test_provider_ids_nonempty(self):
        """obspy's URL_MAPPINGS yields a non-empty provider id list."""
        assert fdsn_cli._provider_ids(), "obspy registry is non-empty"

    def test_diffs_obspy_providers_against_curated(self, monkeypatch):
        """fdsn live providers diff against the curated fdsn_id set."""
        monkeypatch.setattr(
            fdsn_cli, "_provider_ids", lambda: ["USGS", "IRIS", "NEWCENTER"]
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "fdsn refresh ran"
        assert outcome.live_count == 3, "three obspy providers listed"
        assert "NEWCENTER" in outcome.new_ids, "an uncurated centre is new"
