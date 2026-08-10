"""Unit tests for the RADKLIM URL / listing helpers (no network)."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.radklim._helpers import (
    FORMAT_EXTENSION,
    FORMAT_MAGIC,
    operational_dir_url,
    operational_granule_url,
    parse_listing,
    reproc_archive_url,
    timestamp_from_name,
)

pytestmark = [pytest.mark.radklim, pytest.mark.unit]


class TestReprocArchiveUrl:
    """Tests for reproc_archive_url."""

    @pytest.mark.parametrize(
        "frequency, code, year, tail",
        [
            (
                "5_minutes",
                "YW",
                2024,
                "5_minutes/radolan/reproc/2017_002/netCDF/2024/YW2017.002_2024_netcdf.tar.gz",
            ),
            (
                "hourly",
                "RW",
                2001,
                "hourly/radolan/reproc/2017_002/netCDF/2001/RW2017.002_2001_netcdf.tar.gz",
            ),
        ],
    )
    def test_builds_yearly_archive_url(self, frequency, code, year, tail):
        """The version underscore becomes a dot in the archive file name."""
        url = reproc_archive_url(frequency, "2017_002", code, year)
        assert url.endswith(tail), f"unexpected URL: {url}"
        assert url.startswith("https://opendata.dwd.de/climate_environment/CDC/")


class TestOperationalUrls:
    """Tests for the operational directory / granule URL builders."""

    def test_dir_url_has_trailing_slash(self):
        """The operational directory URL ends with the product slash."""
        assert (
            operational_dir_url("yw")
            == "https://opendata.dwd.de/weather/radar/radolan/yw/"
        )

    def test_granule_url_joins_dir_and_name(self):
        """A granule URL is the directory URL plus the file name."""
        url = operational_granule_url("rw", "raa01-rw_10000-2401011200-dwd---bin.hdf5")
        assert url == (
            "https://opendata.dwd.de/weather/radar/radolan/rw/"
            "raa01-rw_10000-2401011200-dwd---bin.hdf5"
        )


class TestParseListing:
    """Tests for parse_listing."""

    def test_filters_by_code_and_ext(self, operational_listing):
        """Only the requested product code and extension are kept."""
        names = parse_listing(operational_listing, "yw", "hdf5")
        assert names == [
            "raa01-yw_10000-2401011200-dwd---bin.hdf5",
            "raa01-yw_10000-2401011205-dwd---bin.hdf5",
            "raa01-yw_10000-2401011210-dwd---bin.hdf5",
        ], names

    def test_bz2_extension_selects_the_binary(self, operational_listing):
        """Asking for bz2 returns the RADOLAN binary rows only."""
        names = parse_listing(operational_listing, "yw", "bz2")
        assert names == ["raa01-yw_10000-2401011200-dwd---bin.bz2"], names

    def test_deduplicates_repeated_names(self):
        """A name appearing twice in the listing is returned once."""
        one = "raa01-rw_10000-2401011200-dwd---bin.hdf5"
        names = parse_listing(f"{one} {one}", "rw", "hdf5")
        assert names == [one], names

    def test_empty_listing_returns_empty(self):
        """A listing with no matching rows yields no names."""
        assert parse_listing("nothing here", "yw", "hdf5") == []


class TestTimestampFromName:
    """Tests for timestamp_from_name."""

    def test_parses_yymmddhhmm(self):
        """The 10-digit stamp parses to a naive-UTC datetime."""
        got = timestamp_from_name("raa01-yw_10000-2608081820-dwd---bin.hdf5")
        assert got == dt.datetime(2026, 8, 8, 18, 20), got

    def test_unrecognised_name_raises(self):
        """A name without a RADOLAN stamp raises ValueError."""
        with pytest.raises(ValueError, match="no RADOLAN timestamp"):
            timestamp_from_name("not-a-granule.txt")


class TestFormatMaps:
    """Tests for the format magic-bytes and extension maps."""

    @pytest.mark.parametrize(
        "fmt, magic, ext",
        [
            ("nc", b"\x1f\x8b", "tar.gz"),
            ("hdf5", b"\x89HDF", "hdf5"),
            ("bin", b"BZh", "bz2"),
        ],
    )
    def test_magic_and_extension_agree(self, fmt, magic, ext):
        """Each format maps to its file magic and on-disk extension."""
        assert FORMAT_MAGIC[fmt] == magic, FORMAT_MAGIC[fmt]
        assert FORMAT_EXTENSION[fmt] == ext, FORMAT_EXTENSION[fmt]
