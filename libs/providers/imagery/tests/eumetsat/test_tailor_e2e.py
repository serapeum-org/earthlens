"""Live end-to-end test for the EUMETSAT Data Tailor branch.

Hits the real EUMETSAT Data Tailor (EPCS) via `eumdac`. Gated behind both
the `e2e` pytest marker and the OAuth2 env vars (`EUMETSAT_CONSUMER_KEY` /
`EUMETSAT_CONSUMER_SECRET`), so a default `pytest` invocation skips it.

Run with:

    EUMETSAT_CONSUMER_KEY=... EUMETSAT_CONSUMER_SECRET=... \\
    pixi run -e dev pytest -m "e2e and eumetsat" tests/eumetsat

The test submits ONE small `tailor=` customisation of a Sentinel-3 OLCI L1
EFR product (a tailorable, open-licence Copernicus collection) cropped to a
tiny ROI and reformatted to GeoTIFF, then checks the customised raster opens
with pyramids and that no customisation is left behind (quota hygiene). A
Data Store download `403` inside Data Tailor means the account has valid
credentials but is not authorised to download that collection (accept the
licence in the EUMETSAT portal) — an account-side configuration, not a code
regression, so it is reported as a skip.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.eumetsat import TailorConfig

_HAVE_CREDS = bool(
    os.environ.get("EUMETSAT_CONSUMER_KEY")
    and os.environ.get("EUMETSAT_CONSUMER_SECRET")
)

# OLCI L1 EFR has a short latency; probe a couple of days back so the window
# is comfortably populated regardless of the exact run time.
_PROBE_DATE = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime(
    "%Y-%m-%d"
)

#: Substrings marking a Data Store download refusal (account not authorised).
_NOT_AUTHORISED = (
    "403",
    "Unauthorised",
    "Unauthorized",
    "error downloading from the Data Store",
)


class _RecordSubmitted:
    """Wrap `_submit_customisation` to record the handle it returns.

    The quota-hygiene check needs to know which customisation *this* test
    created. Counting the account's customisations cannot tell us: the
    EUMETSAT account is shared, so a concurrent CI job submitting its own
    customisation moves the total independently of our cleanup.

    Args:
        original: The unbound `_submit_customisation` being wrapped.
        seen: List the submitted handle's id is appended to.
    """

    def __init__(self, original, seen: list[str]) -> None:
        self._original = original
        self._seen = seen

    def __call__(self, datatailor, product, chain):
        """Submit as usual, recording the resulting customisation's id."""
        cust = self._original(datatailor, product, chain)
        self._seen.append(str(cust))
        return cust


@pytest.mark.e2e
@pytest.mark.eumetsat
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET to run live EUMETSAT e2e tests",
)
class TestEumetsatDataTailorLive:
    """Submit one live customisation and read the customised GeoTIFF back."""

    def test_tailor_one_customisation_roundtrip(self, tmp_path: Path, monkeypatch):
        """A live `tailor=` request customises to GeoTIFF, opens, and cleans up.

        Skips (not fails) when the account is not download-authorised, so the
        gate is green on a valid-but-unlicensed key.
        """
        el = EarthLens(
            data_source="eumetsat",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"s3-olci-l1-efr": ["OLL1EFR"]},
            lat_lim=[50.0, 52.0],
            lon_lim=[-1.0, 1.0],
            path=str(tmp_path),
        )
        backend = el.datasource
        backend._auth.configure()
        # Record the customisation this test submits. A before/after *count* of
        # the account's customisations is not a cleanup signal: the EUMETSAT
        # account is shared, so a concurrent CI job's customisation moves the
        # total on its own. That raced - a run once failed `assert 4 <= 2`, a
        # delta of +2 from a test that submits exactly one, with no delete
        # failure logged.
        submitted: list[str] = []
        monkeypatch.setattr(
            type(backend),
            "_submit_customisation",
            _RecordSubmitted(type(backend)._submit_customisation, submitted),
        )

        try:
            paths = el.download(
                progress_bar=False,
                tailor=TailorConfig(
                    format="geotiff", crs="geographic", bbox=(-1.0, 50.0, 1.0, 52.0)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - classify a download 403 as a skip
            if any(marker in str(exc) for marker in _NOT_AUTHORISED):
                pytest.skip(
                    "credentials are valid but the account is not authorised to "
                    f"download this collection (accept the licence): {exc}"
                )
            raise

        assert paths, "tailor download returned no paths"
        assert paths[0].exists() and paths[0].stat().st_size > 0

        from pyramids.dataset import Dataset

        raster = Dataset.read_file(str(paths[0]))
        assert raster.rows > 0 and raster.columns > 0

        assert submitted, "the test never submitted a customisation to check"
        remaining = {str(c) for c in backend._auth.datatailor().customisations}
        leaked = remaining.intersection(submitted)
        assert not leaked, (
            f"customisation(s) {sorted(leaked)} survived streaming - "
            "_tailor_one's finally-block delete did not free the quota"
        )
