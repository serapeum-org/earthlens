"""The real CMEMS exchange, replayed offline from two recordings.

The sibling fake-based tests cover the error branches a recording cannot
reach. This one pins the shape of what CMEMS actually sends back, so a change
in that shape fails here rather than in the weekly live lane.

CMEMS is the only recorded backend where earthlens speaks no HTTP itself:
`copernicusmarine.subset()` does, over two transports, and they need two
different replay mechanisms.

* The OpenID auth legs and the catalog JSON go through `requests`. vcrpy
  replays those from the cassette.
* The data is ARCO zarr read through the toolbox's own boto3 client. vcrpy
  *records* those reads but cannot replay them — on playback botocore opens a
  connection vcrpy never sees, so the request reaches CloudFerro for real and
  the test passes only with a live socket. That is precisely the failure this
  tier exists to catch, so the S3 half is served by the same in-process S3
  the six bucket backends use, seeded from what the recording captured.

Between them nothing touches the network, which `--disable-socket` enforces.

A regional 2-D field, not the global 4-D one the e2e test uses: zarr reads
whole chunks, so a chunk is the smallest thing that can cross the wire, and
GLORYS12's `thetao` chunk is 4 MB on its own against 147 KB here. `_my_`
(multi-year reprocessed), not NRT, so the chosen day is closed and the
recording can be reproduced.
"""

from __future__ import annotations

import os

import pytest

from earthlens.core import EarthLens

#: Baltic/North Sea reprocessed L4 SST: a fixed, long-closed day.
_DATASET = "cmems_obs-sst_bal_phy-temp_my_l4_P1D-m"
_DATE = "2020-06-01"


@pytest.mark.vcr
def test_a_real_request_parses(tmp_path, monkeypatch, seed_cmems_s3):
    """A recorded L4 SST subset replays into a readable NetCDF."""
    # A placeholder credential when none is exported, because the backend
    # refuses to build a request without one and the real values are scrubbed
    # from the cassette. CMEMS authenticates in a POST body and a header,
    # neither of which the replay matches on, so any non-empty value replays.
    # A real credential passes through untouched, which is what makes
    # re-recording possible; the unit lane exports no secrets.
    for name in (
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
    ):
        monkeypatch.setenv(name, os.environ.get(name) or "placeholder")

    paths = EarthLens(
        data_source="cmems",
        start=_DATE,
        end=_DATE,
        variables={_DATASET: ["analysed_sst"]},
        lat_lim=[55.0, 55.1],
        lon_lim=[15.0, 15.1],
        temporal_resolution="daily",
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert paths, "a file-writing backend returns the paths it wrote"
    assert paths[0].suffix == ".nc", f"expected a NetCDF, got {paths}"
    assert paths[0].exists(), "the written path exists"
