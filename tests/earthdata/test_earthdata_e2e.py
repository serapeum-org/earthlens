"""Live end-to-end tests for the NASA Earthdata backend.

Hits real Earthdata Login (EDL) + CMR + a DAAC. Gated behind both the
`e2e` pytest marker and the EDL env vars (`EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`), so a default `pytest` invocation skips them.

Run with:

    EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... \\
    pixi run -e dev pytest -m "e2e and earthdata" tests/earthdata
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

_HAVE_CREDS = bool(
    os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD")
)

# GPM IMERG late half-hourly has a publication latency of roughly half a
# day; probe ~10 days back so the requested window is comfortably
# populated regardless of the exact run time.
_PROBE_DATE = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).strftime(
    "%Y-%m-%d"
)


@pytest.mark.e2e
@pytest.mark.earthdata
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EARTHDATA_USERNAME / EARTHDATA_PASSWORD to run live Earthdata e2e tests",
)
class TestEarthdataLiveFetch:
    """Single tiny granule fetch against a public Earthdata collection."""

    def test_imerg_one_day_small_box(self, tmp_path: Path):
        """GPM IMERG — small bbox × 1 day → at least one granule on disk."""
        el = EarthLens(
            data_source="earthdata",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"GPM_3IMERGHHL_07": ["precipitation"]},
            lat_lim=[0.0, 2.0],
            lon_lim=[0.0, 2.0],
            temporal_resolution="daily",
            path=str(tmp_path),
            direct_s3="never",
        )
        paths = el.download(progress_bar=False)
        assert paths, f"no granules written into {tmp_path!r}: {list(tmp_path.iterdir())!r}"
        assert all(Path(p).exists() for p in paths), (
            f"download() returned non-existent paths: {paths!r}"
        )
