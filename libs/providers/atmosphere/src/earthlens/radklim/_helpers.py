"""URL builders, filename parsing, and format magic bytes for `earthlens.radklim`.

Stateless helpers the backend uses to turn a `(product, date-window)` request
into concrete DWD Open Data URLs, and to read the operational stream's Apache
directory listing back into per-timestamp granules. No network, no decode.
"""

from __future__ import annotations

import datetime as dt
import re

#: DWD Open Data CDC grids tree — the RADKLIM (reproc) home.
BASE_CDC = "https://opendata.dwd.de/climate_environment/CDC"

#: DWD Open Data weather-radar tree — the operational RADOLAN home.
BASE_WEATHER = "https://opendata.dwd.de/weather"

#: Leading bytes each granule format starts with, so a `200` error page served
#: under a granule name is rejected at the download site rather than on read.
FORMAT_MAGIC: dict[str, bytes] = {
    "nc": b"\x1f\x8b",  # the reproc archive is gzip-of-tar-of-NetCDF
    "hdf5": b"\x89HDF",
    "bin": b"BZh",  # the operational RADOLAN binary is bzip2-compressed
}

#: File extension written to disk for each format token.
FORMAT_EXTENSION: dict[str, str] = {"nc": "tar.gz", "hdf5": "hdf5", "bin": "bz2"}


def reproc_archive_url(frequency: str, version: str, code: str, year: int) -> str:
    """Build the yearly RADKLIM reproc NetCDF archive URL for one year.

    The reprocessing is served as a single `{CODE}{version}_{year}_netcdf.tar.gz`
    archive per year (the folder version `2017_002` appears dotted — `2017.002` —
    in the file name).

    Args:
        frequency: CDC path token — `"5_minutes"` (YW) or `"hourly"` (RW).
        version: Reprocessing version folder, e.g. `"2017_002"`.
        code: Product code in upper case — `"RW"` or `"YW"`.
        year: Calendar year (2001-).

    Returns:
        str: The absolute `.tar.gz` URL.

    Examples:
        - The 2024 5-min archive:
            ```python
            >>> from earthlens.radklim._helpers import reproc_archive_url
            >>> reproc_archive_url("5_minutes", "2017_002", "YW", 2024)
            'https://opendata.dwd.de/climate_environment/CDC/grids_germany/5_minutes/radolan/reproc/2017_002/netCDF/2024/YW2017.002_2024_netcdf.tar.gz'

            ```
    """
    name = f"{code}{version.replace('_', '.')}_{year}_netcdf.tar.gz"
    return (
        f"{BASE_CDC}/grids_germany/{frequency}/radolan/reproc/{version}"
        f"/netCDF/{year}/{name}"
    )


def operational_dir_url(code: str) -> str:
    """Return the operational RADOLAN directory URL for a product code.

    Args:
        code: Product code in lower case — `"rw"` or `"yw"`.

    Returns:
        str: The Apache-indexed directory URL (trailing slash).

    Examples:
        - The 5-min operational directory:
            ```python
            >>> from earthlens.radklim._helpers import operational_dir_url
            >>> operational_dir_url("yw")
            'https://opendata.dwd.de/weather/radar/radolan/yw/'

            ```
    """
    return f"{BASE_WEATHER}/radar/radolan/{code}/"


def operational_granule_url(code: str, name: str) -> str:
    """Return the absolute URL of one operational granule file.

    Args:
        code: Product code in lower case (`"rw"` / `"yw"`).
        name: The granule file name (e.g. `raa01-rw_10000-...-dwd---bin.hdf5`).

    Returns:
        str: The absolute granule URL.
    """
    return f"{operational_dir_url(code)}{name}"


#: Matches an operational granule name and captures its 10-digit timestamp.
_GRANULE_RE = re.compile(
    r"raa01-(?P<code>[a-z0-9]+)_10000-(?P<ts>\d{10})-dwd---bin\.(?P<ext>bz2|hdf5)"
)


def parse_listing(html: str, code: str, ext: str) -> list[str]:
    """Extract the granule file names for one `(code, ext)` from a listing.

    Reads the operational stream's Apache directory index and returns the
    matching granule names, de-duplicated and sorted (so the `latest` symlink
    and the other product's rows are dropped).

    Args:
        html: The raw directory-listing HTML.
        code: Product code in lower case (`"rw"` / `"yw"`).
        ext: File extension to keep — `"hdf5"` or `"bz2"`.

    Returns:
        list[str]: The matching granule names, ascending by timestamp.

    Examples:
        - Two rows, one kept:
            ```python
            >>> from earthlens.radklim._helpers import parse_listing
            >>> html = (
            ...     'raa01-yw_10000-2608081820-dwd---bin.hdf5 '
            ...     'raa01-yw_10000-2608081820-dwd---bin.bz2'
            ... )
            >>> parse_listing(html, "yw", "hdf5")
            ['raa01-yw_10000-2608081820-dwd---bin.hdf5']

            ```
    """
    names: set[str] = set()
    for match in _GRANULE_RE.finditer(html):
        if match.group("code") == code and match.group("ext") == ext:
            names.add(match.group(0))
    return sorted(names, key=timestamp_from_name)


def timestamp_from_name(name: str) -> dt.datetime:
    """Parse the naive-UTC scan time out of an operational granule name.

    Args:
        name: A granule file name carrying a `YYMMDDHHMM` stamp.

    Returns:
        datetime.datetime: The parsed timestamp (naive, UTC).

    Raises:
        ValueError: When `name` carries no recognisable timestamp.

    Examples:
        - The stamp is `YYMMDDHHMM`:
            ```python
            >>> from earthlens.radklim._helpers import timestamp_from_name
            >>> timestamp_from_name("raa01-yw_10000-2608081820-dwd---bin.hdf5")
            datetime.datetime(2026, 8, 8, 18, 20)

            ```
    """
    match = _GRANULE_RE.search(name)
    if match is None:
        raise ValueError(f"{name!r} carries no RADOLAN timestamp.")
    return dt.datetime.strptime(match.group("ts"), "%y%m%d%H%M")
