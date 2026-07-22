"""Facade-level tests for routing `data_source="overture"`."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.earthlens import EarthLens

from earthlens.overture import Overture


@pytest.mark.overture
class TestOvertureFacade:
    """The EarthLens facade routes and gates the Overture backend."""

    def test_key_registered(self):
        """`"overture"` is a registered data-source key."""
        assert "overture" in EarthLens.DataSources

    def test_key_resolves_to_overture_class(self):
        """The key resolves to `earthlens.overture.Overture`."""
        assert EarthLens.DataSources["overture"] is Overture

    def test_facade_routes_to_overture(self, tmp_path: Path):
        """Constructing with the key binds an Overture backend."""
        facade = EarthLens(
            data_source="overture",
            variables={"divisions": ["division_area"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[-74.0, -73.0],
            path=str(tmp_path),
        )
        assert isinstance(facade.datasource, Overture)

    def test_facade_rejects_aggregate_for_vector(self, tmp_path: Path):
        """A non-None aggregate is rejected before the backend runs."""
        facade = EarthLens(
            data_source="overture",
            variables={"divisions": ["division_area"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[-74.0, -73.0],
            path=str(tmp_path),
        )
        with pytest.raises(NotImplementedError, match=r"OUTPUT_KIND='vector'"):
            facade.download(aggregate=object())

    def test_facade_forwards_backend_kwargs(self, tmp_path: Path):
        """Extra kwargs (release, file_format) reach the backend constructor."""
        facade = EarthLens(
            data_source="overture",
            variables={"divisions": ["division_area"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[-74.0, -73.0],
            path=str(tmp_path),
            release="2026-05-20.0",
            file_format="gpkg",
        )
        assert facade.datasource._release == "2026-05-20.0"
        assert facade.datasource._file_format == "gpkg"
