"""Live end-to-end tests for the Overture Maps backend.

Hits the real, public, anonymous Overture GeoParquet on
`s3://overturemaps-us-west-2`, so these tests are gated only behind the
`e2e` pytest marker plus network availability — no credentials are
needed. A default `pytest` invocation skips them.

Run with:

    uv run --locked pytest -m "e2e and overture" -v
"""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import pytest

from earthlens.earthlens import EarthLens
from earthlens.overture import LicenseWarning

#: A tiny bbox over a dense Manhattan block (Times Square), small enough to
#: fetch in seconds and reliably non-empty for both places and buildings.
_LAT_LIM = [40.757, 40.759]
_LON_LIM = [-73.987, -73.984]


@pytest.mark.e2e
@pytest.mark.overture
class TestOvertureLiveFetch:
    """Live Overture fetches (public bucket — no credentials needed)."""

    def test_places_block_has_license_id(self, tmp_path: Path):
        """A tiny `places` fetch lands a non-empty GeoParquet with `license_id`."""
        paths = EarthLens(
            data_source="overture",
            variables={"places": []},
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one written file, got {paths}"
        gdf = gpd.read_parquet(paths[0])
        assert len(gdf) > 0, "the Times-Square block should contain places"
        assert "license_id" in gdf.columns, "per-row license_id must be surfaced"
        assert gdf.crs.to_epsg() == 4326, "output must be tagged EPSG:4326"

    def test_buildings_block_warns_on_odbl(self, tmp_path: Path):
        """A tiny `buildings` fetch is OSM-derived and warns about ODbL rows."""
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            paths = EarthLens(
                data_source="overture",
                variables={"buildings": []},
                lat_lim=_LAT_LIM,
                lon_lim=_LON_LIM,
                path=str(tmp_path),
                file_format="gpkg",
            ).download(progress_bar=False)

        assert paths[0].suffix == ".gpkg", "buildings should write a GeoPackage"
        gdf = gpd.read_file(paths[0])
        assert "license_id" in gdf.columns
        odbl = (gdf["license_id"] == "ODbL-1.0").sum()
        if odbl:
            assert [w for w in record if issubclass(w.category, LicenseWarning)], (
                "ODbL rows present but no LicenseWarning emitted"
            )

    def test_places_geojson_nested_roundtrip(self, tmp_path: Path):
        """A `places` GeoJSON write round-trips Overture's nested struct columns."""
        paths = EarthLens(
            data_source="overture",
            variables={"places": []},
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            file_format="geojson",
        ).download(progress_bar=False)

        assert paths[0].suffix == ".geojson", "places should write GeoJSON"
        gdf = gpd.read_file(paths[0])
        assert len(gdf) > 0, "the block should contain places"
        assert "license_id" in gdf.columns, "license_id must survive the GeoJSON write"

    def test_streaming_with_max_features_caps_live(self, tmp_path: Path):
        """Streaming + `max_features` reads via record_batch_reader and caps rows live."""
        paths = EarthLens(
            data_source="overture",
            variables={"places": []},
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            stream=True,
            max_features=10,
        ).download(progress_bar=False)

        gdf = gpd.read_parquet(paths[0])
        assert 0 < len(gdf) <= 10, "streaming cap should bound the row count"
        assert "license_id" in gdf.columns
        assert gdf.crs.to_epsg() == 4326

    def test_duckdb_where_pushdown_live(self, tmp_path: Path):
        """A `where=` predicate is pushed to S3 via DuckDB and only matches return."""
        paths = EarthLens(
            data_source="overture",
            variables={"places": []},
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            where="confidence > 0.95",
            columns=["names", "confidence"],
        ).download(progress_bar=False)

        gdf = gpd.read_parquet(paths[0])
        assert len(gdf) > 0, "the block should have high-confidence places"
        assert (gdf["confidence"] > 0.95).all(), "the predicate must be pushed down"
        assert "license_id" in gdf.columns, "per-row licensing survives the projection"
        assert gdf.crs.to_epsg() == 4326

    def test_release_helpers_live(self):
        """The SDK's release helpers resolve a real, recent release id."""
        from overturemaps.core import get_latest_release

        release = get_latest_release()
        assert release and release[:2] == "20", f"unexpected release {release!r}"

    def test_duckdb_release_is_resolved_live(self, tmp_path: Path):
        """The DuckDB path globs the published release, not the bundled index."""
        from overturemaps.core import get_latest_release

        backend = EarthLens(
            data_source="overture",
            variables={"places": []},
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            where="confidence > 0.95",
        ).datasource

        assert backend._resolve_release() == get_latest_release(), (
            "an unpinned DuckDB fetch must target the release Overture "
            "publishes; the bundled index goes stale as releases are pruned"
        )
