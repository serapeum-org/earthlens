"""Tests for the Tropycal catalog-tooling handlers (`earthlens.tropycal.cli`).

Moved out of core's CLI test suite when the Tropycal handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import earthlens.tropycal.cli as tropycal_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the tropycal backend."""
    return next(b for b in list_backends() if b.provider == "tropycal")


class TestProber:
    """Tests for the Tropycal basin prober (SDK)."""

    def test_reads_field_schema(self, monkeypatch):
        """tropycal probe records the to_dataframe() field dtypes."""
        monkeypatch.setattr(
            tropycal_cli,
            "_tropycal_fields",
            lambda b, s: {"vmax": {"dtype": "int64"}},
        )
        basin = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), basin)
        assert result.status == "ok", "tropycal probe ran"
        assert result.assets["vmax"]["dtype"] == "int64", "field dtype recorded"

    def test_fields_samples_season(self, monkeypatch):
        """_tropycal_fields samples a season's storms and records column dtypes."""

        class _Frame:
            columns = ["vmax", "mslp"]

            def __getitem__(self, key):
                return types.SimpleNamespace(dtype="int64")

        td = types.SimpleNamespace(
            get_season=lambda year: types.SimpleNamespace(
                summary=lambda: {"id": ["AL012020"]}
            ),
            get_storm=lambda sid: types.SimpleNamespace(
                to_dataframe=lambda attrs_as_columns=False: _Frame()
            ),
        )
        tropycal = types.ModuleType("tropycal")
        tracks = types.ModuleType("tropycal.tracks")
        tracks.TrackDataset = lambda basin=None, source=None: td
        monkeypatch.setitem(sys.modules, "tropycal", tropycal)
        monkeypatch.setitem(sys.modules, "tropycal.tracks", tracks)
        out = tropycal_cli._tropycal_fields("north_atlantic", "hurdat")
        assert out["vmax"]["dtype"] == "int64", "column dtype recorded"


class TestValidator:
    """Tests for the Tropycal SDK-universe validator."""

    def test_unknown_basin_and_bad_source_flagged(self):
        """A non-SDK basin and an unsupported (basin, source) pair are flagged."""
        catalog = SimpleNamespace(
            datasets={
                "north_atlantic": SimpleNamespace(sources=["jtwc"]),
                "mars_basin": SimpleNamespace(sources=["ibtracs"]),
            }
        )
        _checked, issues = tropycal_cli.validator(catalog)
        assert any("mars_basin" in i and "not in" in i for i in issues), "bad basin"
        assert any("jtwc" in i for i in issues), "unsupported source flagged"
