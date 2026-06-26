"""Unit tests for the climate-indices ASCII parsers and the empty frame."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from earthlens.climate_indices import empty_canonical, parse_climexp, parse_psl

DATA = Path(__file__).parent / "data"


@pytest.fixture()
def oni_text() -> str:
    """The captured NOAA PSL ONI fixture body."""
    return (DATA / "psl" / "oni.data").read_text()


@pytest.fixture()
def nao_text() -> str:
    """The captured NOAA PSL NAO fixture body."""
    return (DATA / "psl" / "nao.data").read_text()


@pytest.fixture()
def amo_text() -> str:
    """The captured KNMI climexp AMO fixture body (13-token grid)."""
    return (DATA / "climexp" / "iamo_ersst.dat").read_text()


@pytest.fixture()
def nao_climexp_text() -> str:
    """The captured KNMI climexp NAO fixture body (14-token grid)."""
    return (DATA / "climexp" / "inao.dat").read_text()


def test_parse_psl_row_count_is_twelve_per_year(oni_text: str) -> None:
    """parse_psl yields exactly twelve rows per parsed year."""
    df = parse_psl(oni_text)
    assert list(df.columns) == ["date", "value"]
    assert len(df) % 12 == 0
    assert len(df) // 12 == df["date"].dt.year.nunique()


def test_parse_psl_spot_value_and_monthly_dates(oni_text: str) -> None:
    """A known ONI value lands on the first of its month."""
    df = parse_psl(oni_text).set_index("date")
    assert df.loc[pd.Timestamp("1950-01-01"), "value"] == pytest.approx(-1.53)
    assert (df.index.day == 1).all()


def test_parse_psl_sentinel_becomes_nan(oni_text: str) -> None:
    """The PSL sentinel maps to NaN and never survives as a raw value."""
    df = parse_psl(oni_text)
    assert df["value"].isna().any()
    assert not (df["value"] == -99.9).any()


def test_parse_psl_ignores_header_and_footer(nao_text: str) -> None:
    """Only 13-token year rows parse — the header and prose footer are skipped."""
    df = parse_psl(nao_text)
    # NAO 1948 is entirely sentinel-valued, so the whole year is NaN.
    first_year = df[df["date"].dt.year == 1948]
    assert len(first_year) == 12
    assert first_year["value"].isna().all()
    assert df.loc[df["date"] == pd.Timestamp("1950-01-01"), "value"].iloc[0] == (
        pytest.approx(0.56)
    )


def test_parse_climexp_drops_annual_column(nao_climexp_text: str) -> None:
    """The 14-token climexp grid keeps 12 months and drops the annual mean."""
    df = parse_climexp(nao_climexp_text).set_index("date")
    assert df.loc[pd.Timestamp("1821-07-01"), "value"] == pytest.approx(-2.624)
    assert df.loc[pd.Timestamp("1821-08-01"), "value"] == pytest.approx(-0.143)
    assert math.isnan(df.loc[pd.Timestamp("1821-01-01"), "value"])


def test_parse_climexp_13_token_scientific_notation(amo_text: str) -> None:
    """A 13-token climexp grid parses, including E-notation values."""
    df = parse_climexp(amo_text).set_index("date")
    assert len(df) % 12 == 0
    assert df.loc[pd.Timestamp("1880-01-01"), "value"] == pytest.approx(0.2720515)
    assert df.loc[pd.Timestamp("1880-05-01"), "value"] == pytest.approx(-0.04388506)


def test_parse_climexp_sentinel_becomes_nan(nao_climexp_text: str) -> None:
    """The climexp -999.9 sentinel maps to NaN and never survives raw."""
    df = parse_climexp(nao_climexp_text)
    assert df["value"].isna().any()
    assert not (df["value"] == -999.9).any()


def test_parse_climexp_non_grid_body_yields_no_rows() -> None:
    """An HTML error page (or any non-grid body) parses to zero rows."""
    df = parse_climexp("<!DOCTYPE HTML>\n<html><body>not found</body></html>")
    assert df.empty


def test_empty_canonical_schema() -> None:
    """empty_canonical has the canonical columns and no rows."""
    df = empty_canonical()
    assert list(df.columns) == ["date", "index", "value", "source"]
    assert len(df) == 0
