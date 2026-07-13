"""Live end-to-end test for the OpenStreetMap `pbf` protocol.

Downloads the real (small, ~8.8 MB) Geofabrik **Malta** extract over anonymous
HTTPS and reads its building footprints with `pyrosm` — no credentials. Gated
behind the `e2e` + `osm_pbf` markers plus the `osm-pbf` extra (`pyrosm` /
`osmium`): a default `pytest` run skips it, a missing SDK skips the module, and
a transport failure skips rather than fails. The extract is cached under the
test's `tmp_path` so the run is self-contained.

Run with:

    pixi run -e dev pytest -m "osm_pbf and e2e" tests/osm
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

pytest.importorskip("pyrosm", reason="the pbf e2e needs the osm-pbf extra (pyrosm)")
pytest.importorskip("osmium", reason="the pbf e2e needs the osm-pbf extra (osmium)")

from earthlens.earthlens import EarthLens  # noqa: E402
from earthlens.osm._pbf import download_extract, read_pbf  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.osm_pbf]

# A dense bbox over Valletta / Sliema — stable, plentiful building coverage, so
# the clip path returns features without reading the whole island.
_LAT_LIM = [35.88, 35.94]
_LON_LIM = [14.48, 14.54]


def _skip_on_network(exc: Exception) -> None:
    """Skip (not fail) when the failure is a transport problem, else re-raise."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        pytest.skip(f"Geofabrik unreachable: {exc}")
    raise exc


class TestPbfLive:
    """A live Geofabrik download + pyrosm read of the Malta extract."""

    def test_malta_buildings_returns_features(self, tmp_path: Path):
        """pbf:buildings over the Malta extract returns >=1 polygon, EPSG:4326."""
        try:
            fc = EarthLens(
                data_source="osm",
                variables=["pbf:buildings"],
                region="malta",
                lat_lim=_LAT_LIM,
                lon_lim=_LON_LIM,
                path=str(tmp_path),
                cache_dir=str(tmp_path / "geofabrik"),
            ).download(progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - transport -> skip, else re-raise
            _skip_on_network(exc)
        assert len(fc) >= 1, "expected at least one building footprint"
        assert fc.crs.to_epsg() == 4326
        # the identity column is normalised to `osm_id` (from pyrosm's `id`).
        assert {"osm_id", "osm_type"} <= set(fc.columns)
        assert "building" in fc.columns


class TestPbfPyosmiumLive:
    """A live Geofabrik download read through the streaming pyosmium engine."""

    def test_malta_area_and_line_stream(self, tmp_path: Path):
        """pyosmium reads the real Malta extract for an area + a line layer."""
        try:
            path = download_extract(
                "europe/malta", tmp_path / "geofabrik", progress=False
            )
        except Exception as exc:  # noqa: BLE001 - transport -> skip, else re-raise
            _skip_on_network(exc)
        bbox = (_LON_LIM[0], _LAT_LIM[0], _LON_LIM[1], _LAT_LIM[1])
        buildings = read_pbf(
            path, pyrosm_method="get_buildings", bbox=bbox, engine="pyosmium"
        )
        roads = read_pbf(
            path,
            pyrosm_method="get_network",
            network_type="driving",
            bbox=bbox,
            engine="pyosmium",
        )
        assert len(buildings) >= 1
        assert set(buildings.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
        assert len(roads) >= 1
        assert set(roads.geometry.geom_type) == {"LineString"}
