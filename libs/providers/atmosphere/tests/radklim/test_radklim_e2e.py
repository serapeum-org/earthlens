"""Live end-to-end test for the RADKLIM / RADOLAN backend.

Hits the real, public, anonymous DWD Open Data HTTPS endpoints — no
credentials — so it is gated only on the `e2e` marker and network
reachability. A default `pytest` run skips it.

Run with:

    pytest -m "e2e and radklim" libs/providers/atmosphere/tests/radklim
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.base import HttpClient
from earthlens.radklim import RADKLIM
from earthlens.radklim._helpers import reproc_archive_url

pytestmark = [pytest.mark.e2e, pytest.mark.radklim]

_GERMANY_LAT = [47.0, 55.0]
_GERMANY_LON = [6.0, 15.0]


def _network_available() -> bool:
    """Return whether the DWD operational radar tree is reachable."""
    try:
        HttpClient(timeout=30).get(
            "https://opendata.dwd.de/weather/radar/radolan/rw/", raise_for_status=False
        )
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _network_available(), reason="opendata.dwd.de unreachable")
def test_operational_hdf5_download(tmp_path):
    """A recent radolan-rw window downloads at least one readable HDF5 granule."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    backend = RADKLIM(
        start=(now - dt.timedelta(minutes=95)).strftime("%Y-%m-%dT%H:%M"),
        end=now.strftime("%Y-%m-%dT%H:%M"),
        variables={"radolan-rw": []},
        lat_lim=list(_GERMANY_LAT),
        lon_lim=list(_GERMANY_LON),
        fmt="%Y-%m-%dT%H:%M",
        path=str(tmp_path),
    )
    paths = backend.download(progress_bar=False)
    assert paths, "expected at least one operational granule in the last ~90 min"
    first = Path(paths[0])
    assert first.suffix == ".hdf5", first
    assert first.read_bytes()[:4] == b"\x89HDF", "not a valid HDF5 granule"


@pytest.mark.skipif(not _network_available(), reason="opendata.dwd.de unreachable")
def test_reproc_archive_is_reachable():
    """The RADKLIM reproc yearly archive is live and gzip (a 1-byte range, not a full pull)."""
    url = reproc_archive_url("hourly", "2017_002", "RW", 2024)
    response = HttpClient(timeout=60).get(
        url, headers={"Range": "bytes=0-1", "Accept-Encoding": "identity"}
    )
    assert response.status_code in (200, 206), f"{url} -> {response.status_code}"
    assert response.content[:2] == b"\x1f\x8b", "reproc archive is not gzip"
