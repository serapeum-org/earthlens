"""Live end-to-end tests for the EUMETSAT Data Store backend.

Hits the real EUMETSAT Data Store via `eumdac`. Gated behind both the
`e2e` pytest marker and the OAuth2 env vars (`EUMETSAT_CONSUMER_KEY` /
`EUMETSAT_CONSUMER_SECRET`), so a default `pytest` invocation skips them.

Run with:

    EUMETSAT_CONSUMER_KEY=... EUMETSAT_CONSUMER_SECRET=... \\
    pixi run -e dev pytest -m "e2e and eumetsat" tests/eumetsat

The test targets the MSG cloud-mask collection: its products are small
(~0.5 MB) and reliably present (15-min cadence), so the fetch is quick.
Geostationary products cover the whole disk regardless of bbox, and the
collection has a 15-min cadence, so the test fetches a **single** product
(not the whole window) to keep the download bounded. A download that
returns HTTP 403 means the account has valid credentials but has not
accepted that collection's EUMETSAT licence — that is an account-side
configuration, not a code regression, so it is reported as a skip.
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

# The cloud mask has a short publication latency; probe a couple of days back
# so the window is comfortably populated regardless of the exact run time.
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
    """Authenticate, search, and fetch one small product from the live store."""

    def test_search_and_fetch_one_cloud_mask(self, tmp_path: Path):
        """Live auth + search returns products, and one fetches to disk.

        Fetches a single MSG cloud-mask product (not the whole window) so the
        download stays small. A 403 on the fetch means the account has not
        accepted the collection licence and is reported as a skip.
        """
        el = EarthLens(
            data_source="eumetsat",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"msg-cloud-mask": ["CLM"]},
            lat_lim=[0.0, 10.0],
            lon_lim=[0.0, 10.0],
            path=str(tmp_path),
        )
        backend = el.datasource
        products = backend._search()
        assert products, "live search returned no cloud-mask products for the window"

        try:
            paths = backend._fetch(products[:1])
        except Exception as exc:  # noqa: BLE001 - classify a licence 403 as a skip
            message = str(exc)
            if (
                "403" in message
                or "Unauthorised" in message
                or "Unauthorized" in message
            ):
                pytest.skip(
                    "credentials are valid but the account has not accepted the "
                    f"licence for this collection (HTTP 403): {message}"
                )
            raise

        assert paths, "fetch returned no paths"
        assert paths[0].exists() and paths[0].stat().st_size > 0
