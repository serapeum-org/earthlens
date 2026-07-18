"""Live end-to-end tests for the JAXA backend.

Three tracks:

* **`jaxa-earth` (authless)** — pulls a tiny AW3D30 tile around Mt. Fuji
  through the official `jaxa.earth` API and writes a real COG via
  pyramids. Skipped when the SDK is not installed.
* **`gportal` (credentialed)** — issues a 1-product search against a
  GCOM-C/SGLI dataset and, when credentials are available, SFTP-downloads
  it. Skipped without `$GPORTAL_USERNAME` + `$GPORTAL_PASSWORD`.
* **`ptree` (credentialed)** — downloads one recent 10-minute Himawari-9
  AHI B13 IR observation (10 full-disk segments, ~10 MB total) from
  `ftp.ptree.jaxa.jp` via stdlib `ftplib`. Selected under
  `pytest -m "jaxa and ptree and e2e"`; the test itself runtime-skips
  when `$JAXA_PTREE_USERNAME` + `$JAXA_PTREE_PASSWORD` are unset.

Tests are marked `e2e` and skipped by default (the suite runs with
`-m "not e2e"`); they only run under `pytest -m "jaxa and e2e"`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from earthlens.jaxa import JAXA

pytestmark = [pytest.mark.jaxa, pytest.mark.e2e]


@pytest.mark.slow
def test_jaxa_earth_authless_fetch_lives(tmp_path: Path) -> None:
    """Authless AW3D30 tile pull writes a non-empty COG over EPSG:4326."""
    pytest.importorskip("jaxa.earth")
    from pyramids.dataset import Dataset

    lens = JAXA(
        variables=["aw3d30"],
        start="2000-01-01",
        end="2030-12-31",
        lat_lim=[35.35, 35.40],
        lon_lim=[138.70, 138.78],
        resolution=1000.0,
        path=tmp_path,
    )
    written = lens.download()
    assert len(written) == 1
    assert written[0].exists()
    assert written[0].stat().st_size > 0
    cog = Dataset.read_file(str(written[0]))
    assert cog.epsg == 4326
    geo = cog.geotransform
    assert geo[0] == pytest.approx(138.70, abs=0.005)
    assert geo[3] == pytest.approx(35.40, abs=0.005)


@pytest.mark.slow
@pytest.mark.skipif(
    not (os.environ.get("GPORTAL_USERNAME") and os.environ.get("GPORTAL_PASSWORD")),
    reason="needs $GPORTAL_USERNAME + $GPORTAL_PASSWORD",
)
def test_gportal_search_lives() -> None:
    """A live G-Portal search over a 1-day window matches a small product set.

    Search is **anonymous** even with credentials available (the SDK
    only needs creds for `download`). The test verifies search reaches
    the live catalog; the JAXA backend's auth threading keeps credentials
    off the SDK's module-level globals, so this helper does not need to
    set or restore them.
    """
    pytest.importorskip("gportal")
    import gportal

    search = gportal.search(
        dataset_ids=["10003001"],
        start_time="2024-01-01",
        end_time="2024-01-02",
        count=3,
    )
    assert (search.matched() or 0) >= 1


@pytest.mark.slow
@pytest.mark.skipif(
    not (os.environ.get("GPORTAL_USERNAME") and os.environ.get("GPORTAL_PASSWORD")),
    reason="needs $GPORTAL_USERNAME + $GPORTAL_PASSWORD",
)
def test_gportal_sftp_download_lives(tmp_path: Path) -> None:
    """A credentialed JAXA request actually pulls files over SFTP.

    Drives the full backend chain through the gportal protocol: resolves
    `sgli-l3-nwlr`, authenticates against `$GPORTAL_USERNAME` /
    `$GPORTAL_PASSWORD`, runs `gportal.search`, then `gportal.download`
    over SFTP into `tmp_path`. Asserts at least one non-empty HDF5
    product lands on disk so a regression that breaks the credentialed
    path surfaces here (the other e2e test only exercises the anonymous
    search half). The 1-day window over a large bbox returns ~6 daily
    L3 mosaics totalling ~10 MB.
    """
    pytest.importorskip("gportal")
    from earthlens.core import EarthLens
    lens = EarthLens(
        data_source="jaxa",
        variables=["sgli-l3-nwlr"],
        start="2024-01-01",
        end="2024-01-02",
        lat_lim=[0.0, 30.0],
        lon_lim=[120.0, 150.0],
        path=tmp_path,
    )
    written = lens.download()
    assert len(written) >= 1
    assert all(p.exists() for p in written)
    assert all(p.stat().st_size > 0 for p in written)
    assert all(p.suffix == ".h5" for p in written)


@pytest.mark.slow
@pytest.mark.ptree
def test_ptree_ftp_download_lives(tmp_path: Path) -> None:
    """A live P-Tree fetch downloads all 10 HSD segments for one B13 slot.

    Drives the full backend chain through the `ptree` protocol: resolves
    `himawari-ahi-fldk`, authenticates against `$JAXA_PTREE_USERNAME` /
    `$JAXA_PTREE_PASSWORD`, connects over plain FTP to
    `ftp.ptree.jaxa.jp`, and downloads all 10 full-disk segments of the
    Himawari-9 AHI **B13** IR band for a recent 10-minute slot into
    `tmp_path`. B13 is R20 (2 km), so 10 segments run ~10 MB total —
    small enough for CI. The test asserts every filename matches the
    A1-pinned HSD pattern and lands non-empty on disk. Runtime-skips
    when the credentials are absent (the `ptree` marker still controls
    selection).
    """
    if not (
        os.environ.get("JAXA_PTREE_USERNAME")
        and os.environ.get("JAXA_PTREE_PASSWORD")
    ):
        pytest.skip("needs $JAXA_PTREE_USERNAME + $JAXA_PTREE_PASSWORD")
    import datetime as dt
    import re

    from earthlens.core import EarthLens
    # 3 days back at 12:00 UTC: safely inside the 30-day retention
    # boundary in either direction and well past upload latency (near-
    # real-time slots can lag by 10-15 min). Yesterday 00:00 UTC was
    # fragile at the retention edge on late-in-the-day CI slots.
    reference = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    slot = reference.replace(hour=12, minute=0, second=0, microsecond=0)
    stamp = slot.strftime("%Y-%m-%d %H:%M")

    lens = EarthLens(
        data_source="jaxa",
        variables=["himawari-ahi-fldk"],
        bands=["B13"],
        start=stamp,
        end=stamp,
        fmt="%Y-%m-%d %H:%M",
        lat_lim=[-60.0, 60.0],
        lon_lim=[80.0, 180.0],
        temporal_resolution="hourly",
        path=tmp_path,
    )
    written = lens.download()
    assert len(written) == 10, f"expected 10 segments, got {len(written)}"
    assert all(p.exists() for p in written)
    assert all(p.stat().st_size > 0 for p in written)
    pattern = re.compile(
        r"^HS_H\d\d_\d{8}_\d{4}_B13_FLDK_R20_S\d{2}10\.DAT\.bz2$"
    )
    for path in written:
        assert pattern.match(path.name), f"unexpected filename: {path.name}"
    segments = sorted(int(p.name.split("_S")[-1][:2]) for p in written)
    assert segments == list(range(1, 11)), (
        f"expected segments 1..10, got {segments}"
    )
