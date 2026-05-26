"""Live end-to-end tests for the EUMETSAT Data Store backend.

Hits the real EUMETSAT Data Store via `eumdac`. Gated behind both the
`e2e` pytest marker and the OAuth2 env vars (`EUMETSAT_CONSUMER_KEY` /
`EUMETSAT_CONSUMER_SECRET`), so a default `pytest` invocation skips them.

Run with:

    EUMETSAT_CONSUMER_KEY=... EUMETSAT_CONSUMER_SECRET=... \\
    pixi run -e dev pytest -m "e2e and eumetsat" tests/eumetsat
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

_HAVE_CREDS = bool(
    os.environ.get("EUMETSAT_CONSUMER_KEY")
    and os.environ.get("EUMETSAT_CONSUMER_SECRET")
)

# SEVIRI L1.5 has a short publication latency; probe a couple of days back so
# the window is comfortably populated regardless of the exact run time.
_PROBE_DATE = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime(
    "%Y-%m-%d"
)


@pytest.mark.e2e
@pytest.mark.eumetsat
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET to run live EUMETSAT e2e tests",
)
class TestEumetsatLiveFetch:
    """Single small product fetch against a public EUMETSAT collection."""

    def test_hrseviri_one_day_small_box(self, tmp_path: Path):
        """MSG SEVIRI — small bbox x short window -> at least one product on disk."""
        el = EarthLens(
            data_source="eumetsat",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"msg-hrseviri": ["HRSEVIRI"]},
            lat_lim=[0.0, 5.0],
            lon_lim=[0.0, 5.0],
            path=str(tmp_path),
        )
        paths = el.download(progress_bar=False)
        assert paths, f"no products written into {tmp_path!r}"
        assert all(Path(p).exists() for p in paths)
