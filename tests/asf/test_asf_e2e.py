"""Live end-to-end tests for the ASF InSAR backend.

Hits the real ASF / Earthdata Login services. Anonymous search runs
without credentials; the stack call also runs anonymously; the
download stage requires an EDL bearer token or username/password
(the same credential ladder `earthlens.earthdata` uses).

Gated behind the `e2e` + `asf` pytest markers and the EDL env vars
(`EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`);
the search/stack tests run if `asf_search` is installed; the
download test additionally needs the EDL creds.

Run with:

    EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... \\
    pixi run -e dev pytest -m "e2e and asf" tests/asf
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

asf_search = pytest.importorskip("asf_search")
from earthlens.asf import ASF  # noqa: E402

_HAVE_CREDS = bool(
    os.environ.get("EARTHDATA_TOKEN")
    or (
        os.environ.get("EARTHDATA_USERNAME")
        and os.environ.get("EARTHDATA_PASSWORD")
    )
)


# A known-good Sentinel-1 SLC granule with abundant interferometric
# pairs (Iceland, central rift zone, ascending). Used as the stack
# reference and as the search-window check.
_REFERENCE_SLC = "S1A_IW_SLC__1SDV_20240601T072115_20240601T072143_054132_06960B_6FE8"


@pytest.mark.e2e
@pytest.mark.asf
class TestSearchAnonymous:
    """Anonymous SAR catalog search — no creds required."""

    def test_geo_search_returns_some_results(self, tmp_path: Path) -> None:
        """A small bbox × ~10-day window returns at least one S1 SLC."""
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
        start = end - dt.timedelta(days=10)
        backend = ASF(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            variables=["sentinel-1-slc"],
            # Iceland, central rift zone — Sentinel-1 ascends regularly.
            lat_lim=[63.8, 64.4],
            lon_lim=[-21.7, -21.0],
            path=tmp_path,
            max_results=5,
        )
        products = backend._search()
        # ASF data availability is upstream-controlled — a quiet window
        # in the rift zone is a legitimate weekly-cron outcome and not a
        # backend regression, so skip rather than fail when the catalogue
        # returns nothing.
        if not products:
            pytest.skip(
                "ASF returned zero products for the probe window; "
                "this is upstream availability, not a backend issue"
            )
        for remote in products:
            assert remote.metadata["fileName"].endswith(".zip")


@pytest.mark.e2e
@pytest.mark.asf
class TestStackAnonymous:
    """Anonymous baseline stack from a known reference — no creds required."""

    def test_stack_from_known_reference_returns_baseline_bearing_products(
        self, tmp_path: Path
    ) -> None:
        """Build a tight baseline stack from a real reference granule."""
        backend = ASF(
            start="2024-01-01",
            end="2024-12-31",
            variables=["sentinel-1-slc"],
            reference=_REFERENCE_SLC,
            perpendicular_baseline=(-100.0, 100.0),
            temporal_baseline=(0, 60),
            path=tmp_path,
        )
        try:
            products = backend._search()
        except ValueError as exc:
            # ASF occasionally retires named granules; skip rather than
            # turn a missing fixture into a weekly cron failure.
            pytest.skip(f"reference granule no longer available: {exc}")
        # The reference itself is in the stack (perp/temp = 0/0).
        assert products
        for remote in products:
            perp = remote.metadata["perpendicularBaseline"]
            temp = remote.metadata["temporalBaseline"]
            # Client-side post-filter (apply_baseline_windows) keeps
            # the returned acquisitions inside the bounds.
            assert perp is None or -100.0 <= perp <= 100.0
            assert temp is None or 0 <= temp <= 60


@pytest.mark.e2e
@pytest.mark.asf
@pytest.mark.skipif(
    not _HAVE_CREDS,
    reason="set EARTHDATA_TOKEN or EARTHDATA_USERNAME / EARTHDATA_PASSWORD "
    "to run authed ASF download",
)
class TestDownloadAuthed:
    """Authed single-file download — requires EDL credentials."""

    def test_download_one_product_to_disk(self, tmp_path: Path) -> None:
        """Pull a single small product and assert the file lands on disk.

        Note: even an S1 SLC is hundreds of MB; this exercise is opt-in
        via the e2e marker and the EDL creds gate, but a tighter target
        keeps the runtime sane.
        """
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)
        start = end - dt.timedelta(days=3)
        backend = ASF(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            variables=["sentinel-1-slc"],
            lat_lim=[63.9, 64.1],
            lon_lim=[-21.4, -21.2],
            path=tmp_path,
            max_results=1,
        )
        paths = backend.download()
        assert paths, "no products returned"
        for path in paths:
            assert path.exists(), f"expected file on disk: {path}"
            assert path.stat().st_size > 0
