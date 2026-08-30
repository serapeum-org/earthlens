"""Live end-to-end tests for the NASA FIRMS active-fire backend.

Hits the real FIRMS area CSV API, so these tests are gated behind the
`e2e` pytest marker *and* the presence of a `FIRMS_MAP_KEY` environment
variable. A default `pytest` invocation skips them; a run without the
key skips cleanly rather than failing.

The query is kept to a single sensor over a single ≤10-day chunk
(≈1 FIRMS transaction, well under the quota). It asserts the schema and
plausible value ranges rather than a non-empty result: a recent window
over any one box can legitimately contain zero detections, so a
non-empty assertion would be seasonally flaky.

Run with:

    pixi run -e dev pytest -m e2e tests/firms
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.firms.events import ATTRIBUTE_COLUMNS

_HAS_KEY = bool(os.environ.get("FIRMS_MAP_KEY"))

# A recent 5-day window; FIRMS NRT retains only ~2 months, so the window
# must be recent. A wide tropical box maximises the chance of fire
# activity year-round, but the test does not require a non-empty result.
_TODAY = dt.date.today()
_START = (_TODAY - dt.timedelta(days=5)).strftime("%Y-%m-%d")
_END = _TODAY.strftime("%Y-%m-%d")

#: Lowest FRP (MW) accepted as retrieval noise rather than a parsing error.
#: Fire Radiative Power is a radiance *difference* against a fitted background,
#: so a marginal low-confidence detection can come back slightly negative — NASA
#: emits those. Measured: a 5-day equatorial-Africa window of 115,553 detections
#: carried exactly one, `-0.26` MW on a `confidence='l'` pixel. A column shift or
#: unit error, which is what this check exists to catch, shows up as wild values,
#: not as one small negative.
_FRP_NOISE_FLOOR = -1.0

#: Largest share of negative FRP tolerated before the frame is suspect. The
#: single measured negative is 9e-6 of the frame; a misparse would be orders of
#: magnitude above this.
_FRP_MAX_NEGATIVE_SHARE = 0.01


@pytest.mark.e2e
@pytest.mark.firms
@pytest.mark.skipif(not _HAS_KEY, reason="FIRMS_MAP_KEY not set")
class TestFirmsLiveQuery:
    """Live FIRMS area-CSV queries (needs a free MAP_KEY)."""

    def test_recent_window_returns_schema(self, tmp_path: Path):
        """A recent single-sensor query returns a schema-correct FeatureCollection."""
        fc = EarthLens(
            variables=["VIIRS_SNPP_NRT"],
            data_source="firms",
            start=_START,
            end=_END,
            lat_lim=[-10.0, 10.0],
            lon_lim=[10.0, 35.0],
            path=str(tmp_path),
        ).download(progress_bar=False)

        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert fc.crs.to_epsg() == 4326
        if len(fc):
            frp = fc["frp"].dropna()
            worst = float(frp.min()) if len(frp) else 0.0
            assert worst >= _FRP_NOISE_FLOOR, (
                f"FRP {worst} is below the retrieval noise floor "
                f"({_FRP_NOISE_FLOOR}) — suspect a column shift or unit error"
            )
            share = float((frp < 0).mean()) if len(frp) else 0.0
            assert share < _FRP_MAX_NEGATIVE_SHARE, (
                f"{share:.2%} of FRP values are negative — retrieval noise is "
                "rare, so this many suggests the frame is misparsed"
            )
            assert fc["latitude"].between(-10.0, 10.0).all(), "lat within bbox"
            assert fc["longitude"].between(10.0, 35.0).all(), "lon within bbox"
            assert list(tmp_path.glob("firms_*.gpkg")), "GeoPackage written"
