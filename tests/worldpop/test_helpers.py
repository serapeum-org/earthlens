"""Tests for the WorldPop helper functions (EPSG, ISO3, cohort, bbox)."""

from __future__ import annotations

import pytest

from earthlens.worldpop._helpers import (
    cohort_of,
    epsg_int,
    iso3_for_bbox,
    load_iso3_bbox,
    normalise_iso3,
)

pytestmark = pytest.mark.worldpop


@pytest.mark.parametrize(
    "value, expected",
    [(4326, 4326), ("4326", 4326), ("EPSG:4326", 4326), ("epsg:3857", 3857)],
)
def test_epsg_int_parses(value, expected):
    """epsg_int parses ints and the common EPSG spellings."""
    assert epsg_int(value) == expected


def test_epsg_int_bad_value_raises():
    """A non-EPSG string raises a clear ValueError."""
    with pytest.raises(ValueError, match="could not parse"):
        epsg_int("not-a-crs")


@pytest.mark.parametrize("code, expected", [("ken", "KEN"), (" Com ", "COM")])
def test_normalise_iso3(code, expected):
    """normalise_iso3 upper-cases and strips a valid code."""
    assert normalise_iso3(code) == expected


def test_normalise_iso3_invalid_raises():
    """A non-3-letter code is rejected."""
    with pytest.raises(ValueError, match="ISO3"):
        normalise_iso3("KENYA")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ken_f_0_2020.tif", ("f", 0)),
        ("https://x/com_m_80_2000.tif", ("m", 80)),
        ("KEN_M_15_2015.TIF", ("m", 15)),
        ("ken_ppp_2020.tif", None),
        ("not_a_match.txt", None),
    ],
)
def test_cohort_of(name, expected):
    """cohort_of parses age/sex files and rejects plain population files."""
    assert cohort_of(name) == expected


def test_load_iso3_bbox_has_kenya():
    """The bundled bbox table loads and contains a known country."""
    table = load_iso3_bbox()
    assert "KEN" in table
    assert len(table["KEN"]) == 4


def test_load_iso3_bbox_missing_block_raises(tmp_path):
    """A table file with no bboxes block raises a clear error."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("bboxes:\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'bboxes:' block"):
        load_iso3_bbox(bad)


def test_iso3_for_bbox_returns_neighbours():
    """A bbox over Kenya returns Kenya and its neighbours, not far countries."""
    table = load_iso3_bbox()
    got = iso3_for_bbox([34, -1, 35, 1], table)
    assert "KEN" in got
    assert "FRA" not in got


def test_iso3_for_bbox_empty_when_no_overlap():
    """A bbox over open ocean intersects no country."""
    table = load_iso3_bbox()
    assert iso3_for_bbox([-30, -40, -29, -39], table) == []
