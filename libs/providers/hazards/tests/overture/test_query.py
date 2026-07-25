"""Unit tests for `earthlens.overture.query` SQL construction (no network)."""

from __future__ import annotations

import pytest

from earthlens.overture.query import _dataset_path, _projection, build_query


@pytest.mark.overture
class TestDatasetPath:
    """`_dataset_path` S3 glob construction."""

    def test_path_shape(self):
        """The path embeds the bucket, release, theme, and type partitions."""
        path = _dataset_path("places", "place", "2026-05-20.0")
        assert path == (
            "s3://overturemaps-us-west-2/release/2026-05-20.0/theme=places/type=place/*"
        )


@pytest.mark.overture
class TestProjection:
    """`_projection` SELECT-list construction."""

    def test_no_columns_selects_star_with_wkb_geometry(self):
        """No columns -> all columns, geometry re-encoded to WKB."""
        proj = _projection(None)
        assert proj == "* EXCLUDE geometry, ST_AsWKB(geometry) AS geometry"

    def test_columns_force_id_and_sources(self):
        """Explicit columns always retain `id` and `sources` for identity/licence."""
        proj = _projection(["names", "confidence"])
        for column in ("names", "confidence", "id", "sources"):
            assert f'"{column}"' in proj, column
        assert "ST_AsWKB(geometry) AS geometry" in proj
        assert "* EXCLUDE" not in proj

    def test_columns_drop_geometry_and_dedupe(self):
        """A user-supplied `geometry`/duplicate column is dropped/de-duped."""
        proj = _projection(["id", "geometry", "id"])
        assert proj.count('"id"') == 1
        assert '"geometry"' not in proj


@pytest.mark.overture
class TestBuildQuery:
    """`build_query` full statement assembly."""

    def test_bbox_overlap_predicate(self):
        """The bbox is an overlap test against Overture's per-row bbox struct."""
        sql = build_query("places", "place", "2026-05-20.0", (-74.0, 40.0, -73.0, 41.0))
        assert "bbox.xmin <= -73.0" in sql
        assert "bbox.xmax >= -74.0" in sql
        assert "bbox.ymin <= 41.0" in sql
        assert "bbox.ymax >= 40.0" in sql
        assert "theme=places/type=place" in sql

    def test_where_is_anded(self):
        """A `where` predicate is ANDed onto the bbox filter, parenthesised."""
        sql = build_query(
            "buildings", "building", "r", (0.0, 0.0, 1.0, 1.0), where="height > 10"
        )
        assert "(height > 10)" in sql
        assert ") AND (" in sql

    def test_no_where_has_no_extra_and(self):
        """Without `where`, only the bbox predicate is present."""
        sql = build_query("places", "place", "r", (0.0, 0.0, 1.0, 1.0))
        assert ") AND (" not in sql

    def test_limit_appended(self):
        """`limit` becomes a SQL LIMIT clause."""
        sql = build_query("places", "place", "r", (0.0, 0.0, 1.0, 1.0), limit=50)
        assert sql.rstrip().endswith("LIMIT 50")

    def test_no_limit_has_no_limit_clause(self):
        """Without `limit`, no LIMIT clause is emitted."""
        sql = build_query("places", "place", "r", (0.0, 0.0, 1.0, 1.0))
        assert "LIMIT" not in sql


class _FakeDuckDBConnection:
    """Minimal `duckdb.connect()` stand-in returning a canned frame."""

    def __init__(self, frame):
        self._frame = frame
        self.last_sql: str | None = None

    def execute(self, sql: str):
        self.last_sql = sql
        return self

    def to_arrow_table(self):
        import pyarrow as pa

        return pa.Table.from_pandas(self._frame, preserve_index=False)

    def close(self):
        pass


def _wkb_frame(rows: int):
    """Build a pandas frame mirroring a DuckDB result (WKB geometry column)."""
    import pandas as pd
    from shapely import Point, to_wkb

    return pd.DataFrame(
        {
            "id": [f"f{i}" for i in range(rows)],
            "sources": [[{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}]] * rows,
            "geometry": [to_wkb(Point(float(i), float(i))) for i in range(rows)],
        }
    )


@pytest.mark.overture
class TestQueryOverture:
    """`query_overture` execution + WKB decode (DuckDB connection faked)."""

    def test_decodes_wkb_to_geodataframe(self, monkeypatch):
        """A populated result becomes an EPSG:4326 GeoDataFrame with geometry."""
        import duckdb

        from earthlens.overture.query import query_overture

        monkeypatch.setattr(
            duckdb, "connect", lambda *a, **k: _FakeDuckDBConnection(_wkb_frame(3))
        )
        gdf = query_overture("places", "place", "2026-05-20.0", (0.0, 0.0, 1.0, 1.0))
        assert len(gdf) == 3
        assert gdf.crs.to_epsg() == 4326
        assert gdf.geometry.iloc[0].geom_type == "Point"
        assert "sources" in gdf.columns

    def test_empty_result_returns_empty(self, monkeypatch):
        """A zero-row result yields an empty GeoDataFrame."""
        import duckdb

        from earthlens.overture.query import query_overture

        monkeypatch.setattr(
            duckdb, "connect", lambda *a, **k: _FakeDuckDBConnection(_wkb_frame(0))
        )
        gdf = query_overture("places", "place", "2026-05-20.0", (0.0, 0.0, 1.0, 1.0))
        assert len(gdf) == 0
