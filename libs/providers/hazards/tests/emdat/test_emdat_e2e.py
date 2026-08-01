"""Live end-to-end tests for the EM-DAT disaster backend.

Two very different services sit behind one backend, and these tests treat
them that way:

* `emdat:events` reads the EM-DAT Archive from the UCLouvain Dataverse. It is
  **anonymous**, so this half needs nothing but network.
* `gdis:points` reads a granule from NASA Earthdata Cloud, so it needs an
  Earthdata Login *and* an account that has accepted the SEDAC data-use
  agreement. Those tests skip themselves when no credential resolves, rather
  than failing on a machine that simply has none.

`gdis:polygons` is deliberately **not** exercised here — it is a 2.2 GB
download, which does not belong in a routine live run. Its parsing is covered
by the unit tests against a synthetic GeoPackage.

Run with:

    pytest -m "e2e and emdat" libs/providers/hazards/tests/emdat
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pandas as pd
import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.earthlens import EarthLens

#: A window that both sources cover: GDIS stops at 2018, so anything later
#: would make the two halves incomparable.
_START = "1990-01-01"
_END = "2018-12-31"

#: Bangladesh — reliably flood-prone, so neither source comes back empty.
_LAT = [20.5, 26.7]
_LON = [88.0, 92.7]


def _netrc_has_edl() -> bool:
    """Report whether `~/.netrc` actually holds an Earthdata entry.

    Merely having a `.netrc` is not enough — plenty of machines have one for
    unrelated hosts, and treating that as a credential would turn a skip into
    a confusing failure.
    """
    netrc = Path.home() / ".netrc"
    if not netrc.exists():
        return False
    return "urs.earthdata.nasa.gov" in netrc.read_text(
        encoding="utf-8", errors="ignore"
    )


_HAS_EDL = bool(
    os.getenv("EARTHDATA_TOKEN")
    or (os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"))
    or _netrc_has_edl()
)

_needs_edl = pytest.mark.skipif(
    not _HAS_EDL,
    reason="no Earthdata Login credential resolved (EARTHDATA_TOKEN, "
    "EARTHDATA_USERNAME/PASSWORD, or ~/.netrc)",
)


@pytest.mark.e2e
@pytest.mark.emdat
class TestEventsLive:
    """The anonymous Dataverse archive — no credentials needed."""

    def test_fetches_the_archive(self, tmp_path: Path) -> None:
        """A filtered request returns real EM-DAT event rows."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            events = EarthLens(
                "emdat",
                variables=["emdat:events"],
                start=_START,
                end=_END,
                hazard="flood",
                country="BGD",
                path=str(tmp_path),
            ).download()
        assert isinstance(events, pd.DataFrame)
        assert not events.empty

    def test_schema_is_the_documented_public_table(self, tmp_path: Path) -> None:
        """The archive still ships the columns the backend keys on."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            events = EarthLens(
                "emdat",
                variables=["emdat:events"],
                start="2000-01-01",
                end="2005-12-31",
                hazard="flood",
                country="BGD",
                path=str(tmp_path),
            ).download()
        assert {
            "DisNo.",
            "Disaster Type",
            "ISO",
            "Start Year",
            "Total Deaths",
            "Total Affected",
        } <= set(events.columns)

    def test_filters_are_honoured(self, tmp_path: Path) -> None:
        """The returned rows really are the requested hazard, country and years."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            events = EarthLens(
                "emdat",
                variables=["emdat:events"],
                start="2000-01-01",
                end="2010-12-31",
                hazard="flood",
                country="BGD",
                path=str(tmp_path),
            ).download()
        assert set(events["Disaster Type"].str.lower()) == {"flood"}
        assert set(events["ISO"]) == {"BGD"}
        assert events["Start Year"].between(2000, 2010).all()

    def test_license_warning_is_raised(self, tmp_path: Path) -> None:
        """The restricted-use archive warns on a real download."""
        with pytest.warns(LicenseWarning):
            EarthLens(
                "emdat",
                variables=["emdat:events"],
                start="2010-01-01",
                end="2010-12-31",
                hazard="flood",
                country="BGD",
                path=str(tmp_path),
            ).download()


@pytest.mark.e2e
@pytest.mark.emdat
@_needs_edl
class TestGdisPointsLive:
    """The GDIS centroid granule — needs an Earthdata Login."""

    def test_fetches_flood_locations(self, tmp_path: Path) -> None:
        """A regional flood request returns real point features."""
        locations = EarthLens(
            "emdat",
            variables=["gdis:points"],
            start=_START,
            end=_END,
            hazard="flood",
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
        ).download()
        assert len(locations) > 0
        assert set(locations.geometry.geom_type) == {"Point"}
        assert locations.crs == "EPSG:4326"

    def test_filters_are_honoured(self, tmp_path: Path) -> None:
        """The features really are floods inside the requested window."""
        locations = EarthLens(
            "emdat",
            variables=["gdis:points"],
            start=_START,
            end=_END,
            hazard="flood",
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
        ).download()
        assert set(locations["disastertype"].str.strip()) == {"flood"}
        assert locations["year"].between(1990, 2018).all()

    def test_joins_to_the_event_table(self, tmp_path: Path) -> None:
        """GDIS `disasterno` matches EM-DAT `DisNo.` with the ISO3 suffix dropped."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LicenseWarning)
            events = EarthLens(
                "emdat",
                variables=["emdat:events"],
                start=_START,
                end=_END,
                hazard="flood",
                country="BGD",
                path=str(tmp_path),
            ).download()
        locations = EarthLens(
            "emdat",
            variables=["gdis:points"],
            start=_START,
            end=_END,
            hazard="flood",
            lat_lim=_LAT,
            lon_lim=_LON,
            path=str(tmp_path),
        ).download()
        keys = set(events["DisNo."].str.rsplit("-", n=1).str[0])
        assert keys & set(locations["disasterno"])
