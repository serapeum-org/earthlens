"""Live end-to-end tests for the FLODIS observed-flood impacts backend.

Hits the real Zenodo record `8123096`, which is public (CC-BY-4.0), so these
tests are gated only behind the `e2e` pytest marker plus network availability —
no credentials are needed. A default `pytest` invocation skips them.

Run with:

    pytest -m "e2e and flodis" libs/providers/hazards/tests/flodis
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.core import EarthLens


@pytest.mark.e2e
@pytest.mark.flodis
class TestFlodisLiveDownload:
    """Live downloads from the pinned FLODIS Zenodo record (public)."""

    def test_damages_dataframe(self, tmp_path: Path) -> None:
        """A MOZ window returns a real damages DataFrame keyed on disasterno."""
        damages = EarthLens(
            "flodis",
            dataset="damages",
            country="MOZ",
            start="2000",
            end="2018",
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(damages, pd.DataFrame)
        assert len(damages) > 0, "expected some MOZ flood-damage events in 2000-2018"
        for column in ("ISO3", "year", "disasterno", "total_deaths", "GFD_matches"):
            assert column in damages.columns, f"missing FLODIS column {column!r}"
        assert not any(col.startswith("Unnamed") for col in damages.columns)
        assert set(damages["ISO3"].unique()) == {"MOZ"}
        assert damages["year"].between(2000, 2018).all()
        assert list(tmp_path.glob("flodis_damages-*.csv")), "damages CSV written"

    def test_displacement_dataframe(self, tmp_path: Path) -> None:
        """The displacement table returns a real DataFrame keyed on GADM codes."""
        displacement = EarthLens(
            "flodis",
            dataset="displacement",
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(displacement, pd.DataFrame)
        assert len(displacement) > 0, "expected IDMC displacement events"
        for column in ("ISO3", "year", "displacements", "GID_1", "GID_2"):
            assert column in displacement.columns, f"missing FLODIS column {column!r}"
        assert (tmp_path / "flodis_displacement.csv").exists(), "displacement CSV written"
