"""Tests for the EUMETSAT catalog-tooling handlers (`earthlens.eumetsat.cli`).

Moved out of core's CLI test suite when the EUMETSAT handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.eumetsat.cli as eumetsat_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import refresh_one
from earthlens.cli.stanza import emit_stanza

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the eumetsat backend."""
    return next(b for b in list_backends() if b.provider == "eumetsat")


class TestRefresher:
    """Tests for the EUMETSAT (public browse) lister."""

    def test_lists_collection_ids_from_links(self, monkeypatch):
        """eumetsat refresh reads each browse link's title as the id."""
        monkeypatch.setattr(
            eumetsat_cli,
            "get_json",
            lambda url, **kw: {
                "links": [{"title": "EO:EUM:DAT:1"}, {"title": "EO:EUM:DAT:2"}]
            },
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "eumetsat refresh ran"
        assert outcome.live_count == 2, "two collection ids listed"

    def test_grouped_reads_link_titles(self, monkeypatch):
        """Each browse link's title is taken as the collection id."""
        monkeypatch.setattr(
            eumetsat_cli,
            "get_json",
            lambda url, **kw: {"links": [{"title": "EO:1"}, {"title": "EO:2"}]},
        )
        assert eumetsat_cli.refresher(None) == {"eumetsat": ["EO:1", "EO:2"]}


class TestProber:
    """Tests for the EUMETSAT browse prober (public, no auth)."""

    def test_reads_browse_metadata(self, monkeypatch):
        """eumetsat probe reads the public browse title/abstract/date."""
        monkeypatch.setattr(
            eumetsat_cli,
            "get_json",
            lambda url, **kw: {
                "collection": {"properties": {"title": "HRSEVIRI", "date": "2020"}}
            },
        )
        from earthlens.cli.curate import probe_dataset

        result = probe_dataset(_info(), "EO:EUM:DAT:MSG:HRSEVIRI")
        assert result.status == "ok", "eumetsat probe ran"
        entry = next(iter(result.assets.values()))
        assert entry["title"] == "HRSEVIRI", "title parsed"


class TestEmitter:
    """Tests for the EUMETSAT emitter (public browse)."""

    def test_seeds_collection_row(self, monkeypatch):
        """The browse fetch validates the id and the row carries the group."""
        monkeypatch.setattr(
            eumetsat_cli,
            "get_json",
            lambda url, **kw: {"collection": {"properties": {"title": "HRSEVIRI"}}},
        )
        result = emit_stanza(_info(), "EO:EUM:DAT:MSG:HRSEVIRI", key="msg", group="MSG")
        assert result.status == "ok", "eumetsat emitter ran"
        assert result.row["collection_id"] == "EO:EUM:DAT:MSG:HRSEVIRI"
        assert result.row["group"] == "MSG"
        assert result.row["output_kind"] == "raster"
