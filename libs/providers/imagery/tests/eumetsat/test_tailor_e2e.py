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

#: How long to wait for the native customisation before giving up and skipping.
#: Well under the e2e lane's wall-clock cap, which the GeoTIFF roundtrip test in
#: this file already spends most of -- a second unbounded 30-minute poll would
#: take the whole lane down with it rather than fail this one test.
_NATIVE_POLL_TIMEOUT_S = 480.0

#: Substrings marking a Data Store download refusal (account not authorised).
_NOT_AUTHORISED = (
    "403",
    "Unauthorised",
    "Unauthorized",
    "error downloading from the Data Store",
)


@pytest.mark.e2e
@pytest.mark.eumetsat
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET to run live EUMETSAT e2e tests",
)
class TestEumetsatDataTailorLive:
    """Submit one live customisation and read the customised GeoTIFF back."""

    def test_tailor_one_customisation_roundtrip(self, tmp_path: Path):
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
        before = len(list(backend._auth.datatailor().customisations))

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

        after = len(list(backend._auth.datatailor().customisations))
        assert after <= before, "customisation was not deleted after streaming"

    def test_tailor_native_format_needs_no_projection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A `crs=None` MSG SEVIRI request comes back as native `.nat`.

        Native output cannot be reprojected, so the chain must carry no
        projection at all. Skips (not fails) when the account is not
        download-authorised for the collection, and when Data Tailor is still
        queueing the order at `_NATIVE_POLL_TIMEOUT_S`: the whole lane has to
        finish inside its wall-clock cap, and the roundtrip test above already
        spends a large part of it.
        """
        monkeypatch.setattr(
            "earthlens.eumetsat.backend.TAILOR_POLL_TIMEOUT_S",
            _NATIVE_POLL_TIMEOUT_S,
        )
        el = EarthLens(
            data_source="eumetsat",
            start=_PROBE_DATE,
            end=_PROBE_DATE,
            variables={"msg-hrseviri": ["HRSEVIRI"]},
            lat_lim=[40.0, 55.0],
            lon_lim=[-5.0, 15.0],
            path=str(tmp_path),
        )

        try:
            paths = el.download(
                progress_bar=False,
                tailor=TailorConfig(
                    format="msgnative", crs=None, bbox=(-5.0, 40.0, 15.0, 55.0)
                ),
            )
        except TimeoutError as exc:
            pytest.skip(
                "Data Tailor was still queueing the native order after "
                f"{_NATIVE_POLL_TIMEOUT_S:.0f}s: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - classify a download 403 as a skip
            if any(marker in str(exc) for marker in _NOT_AUTHORISED):
                pytest.skip(
                    "credentials are valid but the account is not authorised to "
                    f"download this collection (accept the licence): {exc}"
                )
            raise

        assert paths, "native tailor download returned no paths"
        assert paths[0].exists(), f"{paths[0]} was not written"
        assert paths[0].stat().st_size > 0, f"{paths[0]} is empty"
        assert paths[0].suffix == ".nat", f"expected a native .nat, got {paths[0].name}"
