"""Tests for the GHSL catalog-tooling handlers (`earthlens.ghsl.cli`).

Moved out of core's CLI test suite when the GHSL tile-regen and live-validate
handlers moved into this distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.ghsl.cli as ghsl_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.validate import validate_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the ghsl backend."""
    return next(b for b in list_backends() if b.provider == "ghsl")


class TestTileRegen:
    """Tests for tile_regen (GIS tile-grid regeneration)."""

    def test_writes_tile_geojson(self, tmp_path, monkeypatch):
        """The tile frame is written to TILE_SCHEMA_PATH as GeoJSON."""
        import geopandas as gpd
        from shapely.geometry import box

        import earthlens.ghsl._helpers as ghsl_helpers

        frame = gpd.GeoDataFrame(
            {
                "tile_id": ["R1_C1"],
                "left": [0],
                "top": [1],
                "right": [1],
                "bottom": [0],
                "geometry": [box(0, 0, 1, 1)],
            },
            crs="ESRI:54009",
        )
        monkeypatch.setattr(ghsl_cli, "_tile_frame", lambda: frame)
        dest = tmp_path / "tile_schema.geojson"
        monkeypatch.setattr(ghsl_helpers, "TILE_SCHEMA_PATH", dest)
        path, count = ghsl_cli.tile_regen()
        assert count == 1 and dest.exists(), "tile geojson written"
        assert path.endswith("tile_schema.geojson"), "wrote the bundled tile index"


class TestLiveValidator:
    """Tests for the live whole-globe artefact HEAD check."""

    def test_flags_non_200(self, monkeypatch):
        """A GHSL artefact that does not HEAD 200 is flagged live."""
        monkeypatch.setattr(ghsl_cli, "http_head", lambda url: 404)
        result = validate_one(_info(), live=True)
        assert result.status == "ok" and result.issues, "404 -> issue"

    def test_clean_at_200(self, monkeypatch):
        """All artefacts HEADing 200 clear the ghsl live check."""
        monkeypatch.setattr(ghsl_cli, "http_head", lambda url: 200)
        result = validate_one(_info(), live=True)
        assert result.issues == [], "all 200 -> clean"

    def test_reports_url_error(self, monkeypatch):
        """A ghsl_url failure is reported as an issue rather than raised."""
        import earthlens.ghsl._helpers as helpers

        def boom(*a, **kw):
            raise RuntimeError("bad url")

        monkeypatch.setattr(helpers, "ghsl_url", boom)
        result = validate_one(_info(), live=True)
        assert result.status == "ok", "errors captured, not raised"
