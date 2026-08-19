"""Live end-to-end test for the IUCN Red List backend.

Hits the real IUCN Red List v4 API, which requires a Bearer token, so it is
gated behind the `e2e` marker and a skip on a missing `IUCN_TOKEN`. A
default `pytest` run skips it. Request a token at
`https://api.iucnredlist.org/users/sign_up`.

Run with:

    uv run --active pytest -m "e2e and iucn" libs/providers/land/tests/iucn
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from earthlens.earthlens import EarthLens

_HAVE_TOKEN = bool(os.environ.get("IUCN_TOKEN"))


@pytest.mark.e2e
@pytest.mark.iucn
@pytest.mark.skipif(not _HAVE_TOKEN, reason="set IUCN_TOKEN to run live IUCN e2e")
class TestIucnLiveQuery:
    """Live IUCN Red List v4 queries (require an IUCN_TOKEN)."""

    def test_species_assessment(self, tmp_path: Path):
        """A well-known species returns a non-empty assessment DataFrame."""
        frame = EarthLens(
            data_source="iucn",
            start="2024-01-01",
            end="2024-12-31",
            variables=["species:Panthera leo"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(frame, pd.DataFrame)
        assert not frame.empty, "expected at least one lion assessment"
        assert frame["scientific_name"].iloc[0] == "Panthera leo"
