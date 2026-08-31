"""Server-side SQL pushdown for Overture via DuckDB.

The default fetch path (`earthlens.overture.backend._read_geodataframe`)
pulls every feature inside the bbox through the `overturemaps` SDK and
filters client-side. For an **attribute** filter — "only buildings taller
than 10 m", "only restaurants", "only high-confidence places" — that wastes
bandwidth: the whole bbox transfers before the predicate is applied.

This module pushes the predicate down to the GeoParquet on
`s3://overturemaps-us-west-2` with DuckDB (`httpfs` + `spatial` extensions),
so only the matching rows leave S3. The bbox is expressed against
Overture's per-row `bbox` struct (a fast, statistics-friendly overlap
test) and the caller's `where` SQL is ANDed on top. The bucket is public,
so an empty-credential S3 secret forces **anonymous / unsigned** access
regardless of any AWS credentials in the environment.

DuckDB is an optional dependency (`pip install earthlens[overture]` pulls
it in); it is imported lazily so the package still imports without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import geopandas as gpd

#: Public Overture S3 bucket (anonymous, us-west-2).
_BUCKET = "overturemaps-us-west-2"
_S3_REGION = "us-west-2"

#: WGS84 — the CRS every Overture geometry is in.
_CRS = "EPSG:4326"


def _dataset_path(theme: str, overture_type: str, release: str) -> str:
    """Return the S3 glob for one theme/type partition of a release.

    Args:
        theme: Friendly theme name (the Overture `theme=` partition).
        overture_type: Overture feature type (the `type=` partition).
        release: Concrete release id (`"2026-07-22.0"`).

    Returns:
        str: An `s3://…/*` glob over the partition's parquet files.
    """
    return f"s3://{_BUCKET}/release/{release}/theme={theme}/type={overture_type}/*"


def _projection(columns: list[str] | None) -> str:
    """Build the SELECT projection, always emitting WKB `geometry`.

    Geometry is re-encoded to WKB (`ST_AsWKB`) so it can be rebuilt as a
    GeoSeries client-side. When `columns` is given, `id` and `sources` are
    forced in so feature identity and the per-row `license_id` survive.

    Args:
        columns: Attribute columns to keep, or `None` for all columns.

    Returns:
        str: The SELECT list (without the `SELECT` keyword).
    """
    if not columns:
        return "* EXCLUDE geometry, ST_AsWKB(geometry) AS geometry"
    kept: list[str] = []
    for column in [*columns, "id", "sources"]:
        if column != "geometry" and column not in kept:
            kept.append(column)
    quoted = ", ".join(f'"{c}"' for c in kept)
    return f"{quoted}, ST_AsWKB(geometry) AS geometry"


def build_query(
    theme: str,
    overture_type: str,
    release: str,
    bbox: tuple[float, float, float, float],
    where: str | None = None,
    columns: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Compose the DuckDB SQL for one theme/type fetch (no execution).

    The bbox is applied as an overlap test against Overture's per-row
    `bbox` struct — a feature is kept when its bbox intersects the query
    box — and the caller's `where` predicate is ANDed on. Factored out
    (pure, no network) so the generated SQL can be unit-tested.

    Args:
        theme: Friendly theme name.
        overture_type: Overture feature type.
        release: Concrete release id.
        bbox: `(west, south, east, north)` in degrees (WGS84).
        where: Optional raw SQL predicate ANDed onto the bbox filter
            (e.g. `"confidence > 0.9 AND height > 10"`). The caller owns
            this SQL.
        columns: Optional attribute columns to keep (`id` / `sources`
            are always retained).
        limit: Optional row cap, applied as SQL `LIMIT`.

    Returns:
        str: The full `SELECT … FROM read_parquet(…) WHERE …` statement.
    """
    west, south, east, north = bbox
    path = _dataset_path(theme, overture_type, release)
    predicate = (
        f"bbox.xmin <= {east} AND bbox.xmax >= {west} "
        f"AND bbox.ymin <= {north} AND bbox.ymax >= {south}"
    )
    if where:
        predicate = f"({predicate}) AND ({where})"
    sql = (
        f"SELECT {_projection(columns)} "  # nosec B608 - `where=` is caller-owned SQL by design; runs in the caller's local DuckDB over public read-only S3, no privilege boundary
        f"FROM read_parquet('{path}', hive_partitioning=1) "
        f"WHERE {predicate}"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def query_overture(
    theme: str,
    overture_type: str,
    release: str,
    bbox: tuple[float, float, float, float],
    where: str | None = None,
    columns: list[str] | None = None,
    limit: int | None = None,
) -> gpd.GeoDataFrame:
    """Run a bbox + attribute query against Overture S3 GeoParquet via DuckDB.

    Pushes both the bbox and the optional `where` predicate down to the
    parquet scan so only matching rows transfer. Reads the public bucket
    anonymously (an empty-credential S3 secret forces unsigned access even
    when AWS credentials are present in the environment).

    Args:
        theme: Friendly theme name (the `theme=` partition).
        overture_type: Overture feature type (the `type=` partition).
        release: Concrete release id (must not be `None` — the S3 path
            needs it).
        bbox: `(west, south, east, north)` in degrees (WGS84).
        where: Optional raw SQL predicate ANDed onto the bbox filter.
        columns: Optional attribute columns to keep.
        limit: Optional row cap.

    Returns:
        geopandas.GeoDataFrame: The matching features, geometry rebuilt
            from WKB and tagged `EPSG:4326`. Empty (no geometry) when
            nothing matched.

    Raises:
        ImportError: If `duckdb` is not installed.
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Overture attribute pushdown (where= / columns=) requires DuckDB. "
            "Install it with `pip install earthlens[overture]`."
        ) from exc
    import geopandas as gpd

    sql = build_query(theme, overture_type, release, bbox, where, columns, limit)
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
        # Empty-credential secret -> anonymous/unsigned read of the public
        # bucket, overriding any AWS credentials in the environment.
        con.execute(
            "CREATE SECRET overture_anon "
            f"(TYPE s3, PROVIDER config, KEY_ID '', SECRET '', REGION '{_S3_REGION}');"
        )
        frame = con.execute(sql).to_arrow_table().to_pandas()
    finally:
        con.close()

    if len(frame) == 0:
        return gpd.GeoDataFrame()
    geometry = gpd.GeoSeries.from_wkb(frame.pop("geometry"), crs=_CRS)
    return gpd.GeoDataFrame(frame, geometry=geometry, crs=_CRS)
