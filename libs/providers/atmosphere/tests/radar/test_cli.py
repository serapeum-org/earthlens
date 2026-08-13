"""Tests for the radar catalog-tooling handlers (`earthlens.radar.cli`).

Moved out of core's CLI test suite when the radar handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import importlib
import shutil

import pytest
import yaml

import earthlens.radar.backend as radar_backend
import earthlens.radar.cli as radar_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one

pytestmark = pytest.mark.cli

_RADAR_TABLE = (
    "NCDCID   ICAO  NAME            ST LAT      LON\n"
    "-------- ----- --------------- -- -------- ---------\n"
    "10000001 KABR  ABERDEEN        SD 45.4558  -98.4133\n"
    "10000002 PAEC  NOME            AK 64.5114  -165.295\n"
    "10000003 xx    BAD ROW         ZZ 0.0      0.0\n"
)

_COLS = [("ICAO", 5), ("NAME", 11), ("ST", 3), ("LAT", 10), ("LON", 11)]


def _info():
    """Return the BackendInfo for the radar backend."""
    return next(b for b in list_backends() if b.provider == "radar")


def _homr():
    """Build a tiny fixed-width HOMR table aligned to the dash rule."""

    def line(vals):
        return " ".join(v.ljust(w) for v, (_, w) in zip(vals, _COLS))

    rule = " ".join("-" * w for _, w in _COLS)
    header = line([name for name, _ in _COLS])
    rows = [
        line(["KABR", "ABERDEEN", "SD", "45.4558", "-98.4131"]),
        line(["KXXX", "BADLAT", "ZZ", "999.0", "0.0"]),
        line(["AB", "SHORT", "XX", "1.0", "2.0"]),
        line(["K1AB", "NUM", "XX", "1.0", "2.0"]),
        line(["KZZZ", "NOLAT", "XX", "abc", "2.0"]),
    ]
    return "\n".join([header, rule, *rows])


class TestRefresher:
    """Tests for the radar (NOAA HOMR) lister."""

    def test_parses_icao_ids(self):
        """Only four-letter alphabetic ICAO ids are parsed from the table."""
        assert radar_cli._radar_station_ids(_RADAR_TABLE) == ["KABR", "PAEC"]

    def test_refresh_diffs_against_curated_stations(self, monkeypatch):
        """radar has no available_* block, so live diffs vs curated stations."""
        monkeypatch.setattr(radar_cli, "get_text", lambda url: _RADAR_TABLE)
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "radar refresh ran"
        assert outcome.live_count == 2, "two ICAO ids parsed"


class TestParse:
    """Tests for the HOMR fixed-width radar parser."""

    def test_keeps_only_valid_stations(self):
        """Only 4-letter alphabetic ICAO rows with in-range coords survive."""
        rows = radar_cli._radar_station_rows(_homr())
        assert list(rows) == ["KABR"], rows
        assert rows["KABR"]["name"] == "Aberdeen", "name title-cased"
        assert rows["KABR"]["state"] == "SD" and rows["KABR"]["latitude"] == 45.4558

    def test_short_table_is_empty(self):
        """A table with fewer than three lines yields no rows."""
        assert radar_cli._radar_station_rows("only\ntwo") == {}

    def test_no_icao_column_is_empty(self):
        """A table whose header lacks ICAO yields no rows."""
        assert radar_cli._radar_station_rows("FOO BAR\n--- ---\nx   y") == {}

    def test_station_ids_sorted(self):
        """_radar_station_ids returns the sorted ICAO ids only."""
        assert radar_cli._radar_station_ids(_homr()) == ["KABR"]

    def test_missing_name_column_returns_empty(self):
        """A HOMR header without NAME yields {} instead of raising KeyError."""
        text = "ICAO  LAT      LON\n----- -------- --------\nKABR  45.0     -98.0"
        assert radar_cli._radar_station_rows(text) == {}

    def test_absent_st_column_defaults_state_blank(self):
        """A table with the required columns but no ST keeps the row, state=''."""
        text = (
            "ICAO  NAME       LAT      LON\n"
            "----- ---------- -------- --------\n"
            "KABR  ABERDEEN   45.0     -98.0"
        )
        rows = radar_cli._radar_station_rows(text)
        assert rows["KABR"]["state"] == "", "absent ST column -> empty state"


class TestWriter:
    """Tests for radar --write (regenerate the curated stations block)."""

    def test_rewrites_stations(self, tmp_path, monkeypatch):
        """writer re-parses HOMR into the curated stations: block."""
        monkeypatch.setattr(radar_cli, "get_text", lambda url: _homr())
        target = tmp_path / "radar_data_catalog.yaml"
        target.write_text("stations:\n  KOLD:\n    name: Old\n", encoding="utf-8")
        monkeypatch.setattr(radar_cli, "index_path", lambda info: target)
        path = radar_cli.writer(_info(), {"radar": ["KABR"]})
        data = yaml.safe_load(open(path))
        assert "KABR" in data["stations"], "HOMR rows written"

    def test_regenerates_stations_block(self, tmp_path, monkeypatch):
        """radar --write re-parses HOMR into the full curated stations: block."""
        info = _info()
        module = importlib.import_module(f"{info.module}.catalog")
        dst = tmp_path / module.CATALOG_PATH.name
        shutil.copy(module.CATALOG_PATH, dst)
        monkeypatch.setattr(module, "CATALOG_PATH", dst)
        module.clear_catalog_cache()
        homr = (
            "NCDCID   ICAO  NAME            ST LAT      LON\n"
            "-------- ----- --------------- -- -------- ---------\n"
            "10000001 KABR  ABERDEEN        SD 45.4558  -98.4133\n"
            "10000002 PAEC  NOME            AK 64.5114  -165.295\n"
        )
        monkeypatch.setattr(radar_cli, "get_text", lambda url: homr)
        refresh_one(info, write=True)
        module.clear_catalog_cache()
        catalog = load_catalog(info)
        assert sorted(catalog.datasets) == ["KABR", "PAEC"], "stations regenerated"
        assert catalog.datasets["KABR"].name == "Aberdeen", "row fields parsed"
        assert catalog.datasets["KABR"].latitude == 45.4558, "latitude parsed"


class TestValidator:
    """Tests for the radar structural + live validators."""

    def test_out_of_range_coords_flagged(self):
        """A station with an impossible latitude is flagged."""
        from types import SimpleNamespace

        catalog = SimpleNamespace(
            datasets={"KXXX": SimpleNamespace(name="X", latitude=999.0, longitude=0.0)}
        )
        _checked, issues = radar_cli.validator(catalog)
        assert any("latitude" in i for i in issues), "bad latitude flagged"

    def test_live_flags_empty_feed(self, monkeypatch):
        """An unreachable / empty NEXRAD chunk feed is flagged live."""
        monkeypatch.setattr(radar_cli, "_radar_feed_stations", lambda: set())
        result = validate_one(_info(), live=True)
        assert result.status == "ok" and result.issues, "empty feed -> issue"

    def test_live_clean_when_streaming(self, monkeypatch):
        """A feed containing a catalogued station clears the radar live check."""
        station = next(iter(load_catalog(_info()).datasets))
        monkeypatch.setattr(
            radar_cli, "_radar_feed_stations", lambda: {station, "KZZZ"}
        )
        result = validate_one(_info(), live=True)
        assert result.issues == [], "streaming station -> clean"

    def test_feed_stations_paginates(self, monkeypatch):
        """_radar_feed_stations follows the continuation token across pages."""
        pages = [
            {
                "CommonPrefixes": [{"Prefix": "KAAA/"}],
                "IsTruncated": True,
                "NextContinuationToken": "t",
            },
            {"CommonPrefixes": [{"Prefix": "KBBB/"}], "IsTruncated": False},
        ]

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def list_objects_v2(self, **kw):
                page = pages[self.calls]
                self.calls += 1
                return page

        monkeypatch.setattr(radar_backend, "_s3_client", lambda region: FakeClient())
        assert radar_cli._radar_feed_stations() == {"KAAA", "KBBB"}, "both pages"
