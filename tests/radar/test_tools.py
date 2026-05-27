"""Unit tests for the radar tools (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "radar"
sys.path.insert(0, str(_TOOLS_DIR))

import audit_radar_catalog as audit  # noqa: E402
import refresh_radar_catalog as refresh  # noqa: E402

pytestmark = [pytest.mark.radar, pytest.mark.unit]

# Field widths matching the NOAA HOMR fixed-width layout (subset of columns).
_COLS = [
    ("NCDCID", 8), ("ICAO", 4), ("WBAN", 5), ("NAME", 20),
    ("ST", 2), ("LAT", 9), ("LON", 10),
]


def _fixed_width(values: list[str]) -> str:
    """Lay values out at the `_COLS` widths, single-space separated."""
    return " ".join(str(v).ljust(w)[:w] for (_, w), v in zip(_COLS, values))


def _sample_table() -> str:
    """A minimal HOMR-style fixed-width table: two valid sites + one bad id."""
    header = _fixed_width([name for name, _ in _COLS])
    separator = " ".join("-" * w for _, w in _COLS)
    rows = [
        _fixed_width(["30001794", "KTLX", "53920", "OKLAHOMA CITY", "OK", "35.3331", "-97.2778"]),
        _fixed_width(["30001795", "KFWS", "53910", "DALLAS FT WORTH", "TX", "32.5731", "-97.3031"]),
        _fixed_width(["30009999", "XX", "00000", "NOT A RADAR", "NA", "10.0", "20.0"]),
    ]
    return "\n".join([header, separator, *rows])


class TestParseStations:
    """Tests for the NOAA HOMR fixed-width parser."""

    def test_extracts_valid_sites(self):
        """Four-letter ICAO rows yield name / state / rounded coordinates."""
        out = refresh.parse_stations(_sample_table())
        assert set(out) == {"KFWS", "KTLX"}
        assert out["KTLX"]["state"] == "OK"
        assert out["KTLX"]["latitude"] == 35.3331
        assert out["KTLX"]["longitude"] == -97.2778

    def test_skips_non_four_letter_id(self):
        """A non-four-letter ICAO (e.g. 'XX') is dropped."""
        assert "XX" not in refresh.parse_stations(_sample_table())

    def test_result_sorted_by_id(self):
        """Stations are returned sorted by site id."""
        assert list(refresh.parse_stations(_sample_table())) == ["KFWS", "KTLX"]


class TestRenderYaml:
    """Tests that rendered YAML round-trips through the catalog loader."""

    def test_render_round_trips(self, tmp_path):
        """render_yaml output reloads via _load_stations to the same sites."""
        from earthlens.radar.catalog import _load_stations, clear_catalog_cache

        stations = refresh.parse_stations(_sample_table())
        path = tmp_path / "radar_data_catalog.yaml"
        path.write_text(refresh.render_yaml(stations), encoding="utf-8")
        clear_catalog_cache()
        loaded = _load_stations(path)
        assert set(loaded) == {"KFWS", "KTLX"}
        assert round(loaded["KTLX"].latitude, 4) == 35.3331
        assert loaded["KTLX"].state == "OK"


class TestFeedStations:
    """Tests for the live-feed station lister (over the fake S3 bucket)."""

    def test_lists_station_prefixes(self, fake_s3):
        """feed_stations returns the top-level station ids present in the feed."""
        assert audit.feed_stations() == {"KTLX"}
