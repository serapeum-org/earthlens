"""Unit tests for the NWP provider-agnostic helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.nwp._helpers import (
    cog_name,
    ensure_dir,
    enumerate_cycles,
    grib_name,
    valid_time,
)

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


class TestEnumerateCycles:
    """Tests for enumerate_cycles."""

    def test_single_day_four_cycles(self):
        """One day with four run hours yields four ascending datetimes."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [0, 6, 12, 18]
        )
        assert out == [
            dt.datetime(2024, 6, 1, 0),
            dt.datetime(2024, 6, 1, 6),
            dt.datetime(2024, 6, 1, 12),
            dt.datetime(2024, 6, 1, 18),
        ], out

    def test_multi_day_count(self):
        """Three days with two run hours yield six cycles."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 3), [0, 12]
        )
        assert len(out) == 6, out

    def test_uses_calendar_date_of_bounds(self):
        """A datetime bound is reduced to its calendar day for enumeration."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1, 9, 30), dt.datetime(2024, 6, 1, 23), [0]
        )
        assert out == [dt.datetime(2024, 6, 1, 0)], out

    def test_dedupes_and_sorts_hours(self):
        """Duplicate / unsorted run hours are de-duplicated and ordered."""
        out = enumerate_cycles(
            dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [12, 0, 12]
        )
        assert out == [dt.datetime(2024, 6, 1, 0), dt.datetime(2024, 6, 1, 12)], out

    def test_inverted_range_raises(self):
        """A start after end raises ValueError."""
        with pytest.raises(ValueError, match="after end"):
            enumerate_cycles(dt.datetime(2024, 6, 2), dt.datetime(2024, 6, 1), [0])

    @pytest.mark.parametrize("hour", [-1, 24, 99])
    def test_out_of_range_hour_raises(self, hour):
        """A run hour outside [0, 23] raises ValueError."""
        with pytest.raises(ValueError, match="outside"):
            enumerate_cycles(dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [hour])


class TestNameHelpers:
    """Tests for cog_name, grib_name, and valid_time."""

    def test_cog_name(self):
        """The COG name embeds the model key, cycle stamp, and f-step."""
        name = cog_name("gfs", dt.datetime(2024, 6, 1, 12), 24)
        assert name == "gfs_2024060112_f024.tif", name

    def test_grib_name(self):
        """The GRIB name mirrors the COG name with a .grib2 suffix."""
        name = grib_name("icon", dt.datetime(2024, 6, 1, 0), 3)
        assert name == "icon_2024060100_f003.grib2", name

    def test_valid_time(self):
        """valid_time adds the step hours to the cycle."""
        assert valid_time(dt.datetime(2024, 6, 1, 0), 30) == dt.datetime(
            2024, 6, 2, 6
        )


class TestEnsureDir:
    """Tests for ensure_dir."""

    def test_creates_and_returns_absolute(self, tmp_path):
        """A missing directory is created and returned as an absolute Path."""
        target = tmp_path / "a" / "b"
        result = ensure_dir(target)
        assert result.exists() and result.is_absolute(), result

    def test_idempotent_on_existing(self, tmp_path):
        """Calling on an existing directory is a no-op that still returns it."""
        result = ensure_dir(tmp_path)
        assert result == tmp_path.absolute(), result
