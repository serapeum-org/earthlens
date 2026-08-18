"""Live end-to-end tests for the GDACS multi-hazard alert backend.

Hits the real GDACS SEARCH feed, which is public, so these tests are
gated only behind the `e2e` pytest marker plus network availability — no
credentials are needed. A default `pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m e2e tests/gdacs
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import NoReturn

import pytest
import requests

from earthlens.earthlens import EarthLens
from earthlens.gdacs import GDACS, GdacsUnavailableError
from earthlens.gdacs.events import ATTRIBUTE_COLUMNS
from earthlens.testing import skip_live_unavailable

# A recent ~30-day window: GDACS is a live alert feed, so very old
# windows can be sparse. Earthquakes are the most frequent hazard, so a
# month of global EQ alerts is reliably non-empty.
_TODAY = dt.date.today()
_RECENT_START = (_TODAY - dt.timedelta(days=30)).strftime("%Y-%m-%d")
_TODAY_STR = _TODAY.strftime("%Y-%m-%d")


def _skip_on_upstream(exc: Exception) -> NoReturn:
    """Skip (not fail) when GDACS SEARCH was unavailable, else re-raise.

    GDACS SEARCH answers a well-formed query with a spurious `400`, or times
    out, under load (issue #929). The backend retries those and, when they
    persist, raises the typed `GdacsUnavailableError`, which skips the lane
    rather than reddening it — the backend's retries already gave the service
    several chances, so a survivor is a real outage, not a regression (the query
    composition is asserted offline by the unit tests). The skip goes through
    `skip_live_unavailable` so it carries the shared availability-skip prefix and
    the repo-wide masked-lane guard still counts it (a wholly-skipped GDACS lane
    never reports green). Anything else re-raises and fails.
    """
    # `GdacsUnavailableError` is the live path — `_fetch` already wraps a SEARCH
    # ConnectionError/Timeout into it. The bare transport arms are belt-and-braces
    # for a transport error raised outside the SEARCH call (e.g. while writing).
    if isinstance(
        exc, (GdacsUnavailableError, requests.ConnectionError, requests.Timeout)
    ):
        skip_live_unavailable(f"GDACS SEARCH unavailable: {exc}")
    raise exc


@pytest.mark.e2e
@pytest.mark.gdacs
class TestGdacsLiveQuery:
    """Live GDACS SEARCH queries (public — no credentials needed)."""

    def test_recent_earthquakes(self, tmp_path: Path):
        """A recent global EQ window returns a schema-correct FeatureCollection."""
        try:
            fc = EarthLens(
                variables=["EQ"],
                data_source="gdacs",
                start=_RECENT_START,
                end=_TODAY_STR,
                lat_lim=[-90.0, 90.0],
                lon_lim=[-180.0, 180.0],
                path=str(tmp_path),
            ).download(progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - upstream -> skip, else re-raise
            _skip_on_upstream(exc)

        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert fc.crs.to_epsg() == 4326
        if len(fc):
            assert fc["hazard_type"].isin(["EQ"]).all(), "only EQ alerts requested"
            assert list(tmp_path.glob("gdacs_alerts_*.gpkg")), "GeoPackage written"

    def test_single_request(self, tmp_path: Path):
        """A multi-hazard download issues one combined query (no per-hazard fan-out)."""
        from unittest import mock

        import earthlens.gdacs.backend as backend_module

        try:
            with mock.patch.object(
                backend_module.requests, "get", wraps=backend_module.requests.get
            ) as spy:
                GDACS(
                    start=_RECENT_START,
                    end=_TODAY_STR,
                    variables=["EQ", "TC", "FL", "VO", "WF", "DR"],
                    lat_lim=[-90.0, 90.0],
                    lon_lim=[-180.0, 180.0],
                    path=str(tmp_path),
                ).download(progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - upstream -> skip, else re-raise
            _skip_on_upstream(exc)
        # Every call carries all six hazard types in one `eventlist`, so any
        # repeat is a retry of the same combined query, not a per-hazard split.
        # (A single transient burp now retries rather than fans out, so counting
        # raw calls would false-fail; assert the query shape instead.)
        assert spy.call_count >= 1, "expected at least one SEARCH request"
        eventlists = {call.kwargs["params"]["eventlist"] for call in spy.call_args_list}
        assert eventlists == {"EQ,TC,FL,VO,WF,DR"}, (
            "expected one combined query for all hazard types (no per-hazard "
            f"fan-out); saw eventlist params {eventlists}"
        )


class TestSkipOnUpstream:
    """The `_skip_on_upstream` triage runs offline (no e2e marker, no network)."""

    def test_skips_on_typed_unavailable(self):
        """A GdacsUnavailableError becomes a prefixed skip, not a failure."""
        exc = GdacsUnavailableError("down", status_code=503)
        with pytest.raises(pytest.skip.Exception) as excinfo:
            _skip_on_upstream(exc)
        assert "GDACS SEARCH unavailable" in str(excinfo.value)

    def test_skips_on_transport_error(self):
        """A bare transport error also skips (belt-and-braces arm)."""
        exc = requests.ConnectionError("dropped")
        with pytest.raises(pytest.skip.Exception):
            _skip_on_upstream(exc)

    def test_reraises_other_errors(self):
        """A non-availability error re-raises unchanged so the test still fails."""
        exc = ValueError("real bug")
        with pytest.raises(ValueError, match="real bug"):
            _skip_on_upstream(exc)
