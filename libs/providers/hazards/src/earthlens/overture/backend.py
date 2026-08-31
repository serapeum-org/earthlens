"""Backend that queries Overture Maps GeoParquet over the public S3 bucket.

`Overture(AbstractDataSource)` wraps the official `overturemaps` SDK,
which reads the Overture Maps Foundation 1.0 GeoParquet on the public,
anonymous `s3://overturemaps-us-west-2` bucket. A request is a **theme +
bbox** (+ optional release): the backend plans one fetch per requested
feature type (`_search`), pulls the bbox-pushed-down GeoParquet for each
(`_fetch`), surfaces a per-row `license_id`, and writes the result as a
pyramids `~pyramids.feature.collection.FeatureCollection`.

This is a `vector` backend: the on-the-wire result is a table of
features (footprints, POIs, road segments, admin boundaries), not a
gridded array, so `OUTPUT_KIND = "vector"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument.
Overture is a static per-release snapshot, so `start` / `end` are
accepted but ignored (there is no temporal axis to iterate); the SDK
auto-targets the newest release when `release` is `None`.

Overture needs **no credentials** — the bucket is public, so there is no
auth class. The only extra is the `overturemaps` SDK, imported lazily so
the package imports without `earthlens[overture]`; a missing SDK
surfaces as a friendly `ImportError` at construction time.

Theme/type selection follows the vector-backend reading of `variables`:
`variables` is a `dict[str, list[str]]` mapping a friendly theme name to
its requested feature types (`{"buildings": []}`,
`{"places": ["place"]}`). An empty type list resolves to the theme's
`default_type`. Because Overture's whole-Earth coverage makes an
unbounded bbox a footgun for the large themes (Buildings is 2.3 B rows),
the backend guards the bbox area for `buildings` / `transportation` /
`places` and rejects the whole-Earth default for them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.overture.catalog import Catalog, Theme
from earthlens.overture.releases import ReleaseLookupError, is_release_id

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection


FileFormat = Literal["geoparquet", "gpkg", "geojson"]

#: Output format -> (OGR driver or `"parquet"`, file extension). GeoParquet
#: is the default because it round-trips Overture's deeply-nested schema
#: (`names`, `categories`, `sources`, …) losslessly; GPKG / GeoJSON go
#: through pyogrio, which JSON-encodes the nested columns.
_FORMATS: dict[str, tuple[str, str]] = {
    "geoparquet": ("parquet", "parquet"),
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Whole-Earth bbox area in square degrees (360 x 180). A request whose
#: box covers (almost) the whole planet is rejected for the guarded
#: themes regardless of the per-theme area cap below.
_WHOLE_EARTH_DEG2 = 360.0 * 180.0

#: Per-theme bbox-area cap, in square degrees, for the themes whose global
#: row counts make an oversized box a footgun (`G5`). A request whose box
#: area exceeds the theme's cap is rejected with guidance to shrink it (or
#: pass `max_features=`). Themes absent from this map (e.g. `divisions`,
#: relatively few rows globally) are unguarded. `max_bbox_deg2=` overrides
#: every entry. One square degree is roughly 12 300 km^2 at the equator.
_DEFAULT_MAX_BBOX_DEG2: dict[str, float] = {
    "buildings": 0.5,
    "transportation": 0.5,
    "places": 9.0,
    "base": 0.5,
    "addresses": 0.5,
}


class Overture(AbstractDataSource):
    """Overture Maps backend (vector GeoParquet output, per-row licensing).

    Wraps the public Overture GeoParquet so a user can pull a theme +
    bbox window of features through the same `download()` shape every
    other earthlens backend uses. Each requested feature type is fetched
    with the SDK's bbox pushdown, tagged `EPSG:4326`, given a per-row
    `license_id` derived from its `sources` column, and written to one
    vector file under `path`. When any `ODbL-1.0` (OSM-derived) rows are
    present a `LicenseWarning` is emitted.

    The bucket needs no credentials.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of features, so
            the facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "overture features are vector, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned FeatureCollection (a GeoDataFrame) directly"

    #: Each Overture release is a snapshot with no time axis, so a missing `start` /
    #: `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        start: str | None = None,
        end: str | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        release: str | None = None,
        max_features: int | None = None,
        file_format: FileFormat = "geoparquet",
        max_bbox_deg2: float | None = None,
        stream: bool = False,
        where: str | None = None,
        columns: list[str] | None = None,
    ):
        """Initialise an Overture backend instance.

        Args:
            variables: Mapping of friendly theme name to its requested
                feature types — `{"buildings": []}` (primary type),
                `{"places": ["place"]}`, or several themes at once. For
                this backend `variables` selects *themes/types*, not data
                variables. An empty type list defaults to the theme's
                `default_type`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`. Required and drives the
                GeoParquet pushdown.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            start: Accepted for signature parity and recorded, but
                ignored — Overture is a static per-release snapshot with
                no temporal axis.
            end: Accepted and ignored (see `start`).
            temporal_resolution: Sentinel `"all"` — Overture is not
                chunked in time.
            path: Output directory for the written vector file(s).
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end` (only used when
                they are supplied, for record-keeping).
            release: Overture release id (`"2026-07-22.0"`). `None` (the
                default) targets the newest release Overture publishes —
                the SDK auto-targets it on the default fetch path, and the
                DuckDB path resolves it live (see `_resolve_release`). Pin
                it only for reproducibility, and expect a pinned id to stop
                resolving once Overture prunes that release from S3.
            max_features: Optional cap on the rows kept per fetched type;
                excess rows are dropped with a warning. `None` keeps all.
            file_format: Output vector format — `"geoparquet"` (default,
                lossless), `"gpkg"`, or `"geojson"`.
            max_bbox_deg2: Optional override of the per-theme bbox-area
                cap (square degrees) applied to the guarded themes. `None`
                uses the built-in per-theme defaults.
            stream: When `True`, read each type through the SDK's streaming
                `record_batch_reader` (lower peak memory) instead of
                materialising the whole bbox via `geodataframe`. Streaming
                is also used automatically whenever `max_features` is set,
                so the cap can stop the read early instead of fetching the
                full bbox and discarding rows.
            where: Optional raw SQL predicate pushed down to the GeoParquet
                via DuckDB (e.g. `"height > 10"`,
                `"categories.primary = 'restaurant'"`), so only matching
                rows leave S3. ANDed onto the bbox filter. Setting it routes
                the fetch through the DuckDB path (requires `duckdb`, pulled
                in by `earthlens[overture]`); takes precedence over `stream`.
            columns: Optional list of attribute columns to keep when using
                the DuckDB path (`id` / `sources` are always retained so
                identity and the per-row `license_id` survive). Ignored on
                the non-DuckDB paths.

        Raises:
            TypeError: If `variables` is not a mapping of theme -> types.
            ValueError: If `file_format` is not one of the supported
                formats, `variables` is empty, or `release` is not shaped
                like an Overture release id.
            ImportError: If the `overturemaps` SDK is not installed.
        """
        if not isinstance(variables, dict):
            raise TypeError(
                "Overture `variables` must be a mapping of theme -> types "
                "(e.g. {'buildings': []} or {'places': ['place']}), not a "
                f"{type(variables).__name__}. For this backend `variables` "
                "selects Overture themes and their feature types."
            )
        if not variables:
            raise ValueError(
                "Overture `variables` is empty; supply at least one theme, "
                "e.g. variables={'buildings': []}."
            )
        if file_format not in _FORMATS:
            raise ValueError(
                f"file_format must be one of {sorted(_FORMATS)}, got {file_format!r}."
            )
        if release is not None and not is_release_id(release):
            raise ValueError(
                f"release must be an Overture release id, got {release!r}. "
                "Ids are a release date plus an ordinal (yyyy-mm-dd.n); "
                "list the ones Overture publishes with "
                "earthlens.overture.releases.child_release_ids(). Leave it "
                "None to target whatever is published now."
            )
        self._release = release
        self._max_features = max_features
        self._file_format: FileFormat = file_format
        self._max_bbox_deg2 = max_bbox_deg2
        self._stream = stream
        self._where = where
        self._columns = columns
        self._catalog = Catalog()
        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )
        # Overture is a static per-release snapshot — pin the sentinel even
        # when the facade forwards its default `"daily"`, so the attribute
        # never misrepresents a temporal cadence the backend does not have.
        self.temporal_resolution = "all"

    def _initialize(self) -> None:
        """Verify the `overturemaps` SDK is importable; no auth, no client.

        Overture's bucket is public, so there is no client to build — but
        the SDK is an optional extra, so this raises a friendly
        `ImportError` early (at construction) when it is missing rather
        than deep inside `_fetch`.

        Returns:
            None: No per-instance client object.

        Raises:
            ImportError: If `overturemaps` is not installed.
        """
        _require_overturemaps()
        return None

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Record the (ignored) date window in a `TemporalExtent`.

        Overture is a static per-release snapshot, so there is no date
        loop: `start` / `end` are parsed only when supplied (for
        record-keeping) and otherwise left `None`. The resolution is the
        sentinel `"all"`.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Ignored beyond being recorded.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are
                `None` when the corresponding argument was `None`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def _resolve_plan(self) -> list[tuple[str, Theme, str]]:
        """Resolve `variables` into the concrete `(theme_name, Theme, type)` fetches.

        Each `variables` theme is looked up in the bundled catalog
        (raising with a did-you-mean hint on an unknown theme) and its
        requested type list expanded against the theme (raising on an
        unknown type, defaulting to the theme's primary type when empty).

        Returns:
            list[tuple[str, Theme, str]]: One `(theme_name, theme, type)`
                triple per type to fetch, in `variables` order.

        Raises:
            ValueError: If a theme name or a requested type is unknown.
        """
        plan: list[tuple[str, Theme, str]] = []
        for name, requested in cast("dict[str, list[str]]", self.vars).items():
            theme = self._catalog.get_theme(name)
            for overture_type in theme.resolve_types(requested):
                plan.append((name, theme, overture_type))
        return plan

    def _resolve_release(self) -> str:
        """Resolve a concrete release id for the DuckDB S3 path.

        Uses the explicit `release` if given, else asks the SDK which
        release Overture currently publishes, else falls back to the
        newest entry in the catalog's bundled index. The DuckDB path
        needs a concrete id (the `geodataframe` path can leave it `None`
        and let the SDK pick latest, but the S3 glob cannot).

        The live lookup comes first because Overture keeps only the
        newest release (or two) on `s3://overturemaps-us-west-2` and
        prunes the rest, so a bundled id goes stale within weeks. Globbing
        a pruned release matches no files and DuckDB fails the read with
        `No files found that match the pattern`. The bundled index stays
        as an offline fallback. The SDK caches the lookup per process,
        but only successful ones, so `_fetch` resolves once up front rather
        than per requested type.

        Only a lookup failure is absorbed, and only that:
        `earthlens.overture.releases.latest_release` raises the single
        typed `ReleaseLookupError` for an unreachable, undecodable, or
        nonsensical catalog, so a genuine code fault (a missing SDK
        constant, say) propagates instead of degrading to the stale
        bundled id — which would be issue #931 restored at `WARNING`
        level. That read is bounded by `STAC_TIMEOUT`; the SDK's own
        lookup is not, and an unbounded one would hang here rather than
        fall through to the index.

        A catalog that reports something which is not release-shaped
        counts as a failure too. Upstream's `latest` is one key in a
        document whose sibling release list already arrives as unparsed
        `https:` fragments, so an unchecked value would build a glob like
        `release/None/…` and fail with the very error this resolution
        order exists to prevent.

        Returns:
            str: A concrete release id (e.g. `"2026-07-22.0"`).

        Raises:
            RuntimeError: If the live lookup fails and the bundled index
                is empty, leaving no release to glob.

        See Also:
            earthlens.overture.catalog.Catalog.latest_release: The offline
                fallback this reads when the live lookup fails.
            earthlens.overture.query.build_query: Builds the S3 glob from
                the resolved release.
        """
        if self._release:
            return self._release
        from earthlens.overture.releases import latest_release

        try:
            return latest_release()
        except ReleaseLookupError as exc:
            cause, reason = exc, str(exc)
        indexed = self._catalog.latest_release()
        if not indexed:
            raise RuntimeError(
                "Could not resolve an Overture release for the DuckDB query "
                f"path: {reason}, and the bundled available_releases: index "
                "is empty. Pass an explicit release= — the ids Overture "
                "publishes are listed at https://stac.overturemaps.org."
            ) from cause
        logger.warning(
            f"Could not resolve the live Overture release ({reason}); falling "
            f"back to the bundled index entry {indexed!r}. Overture prunes old "
            "releases, so this id may no longer exist on S3."
        )
        return indexed

    def _guard_bbox(self, theme_names: list[str]) -> None:
        """Reject an oversized / whole-Earth bbox for the guarded themes.

        Buildings / Transportation / Places cover the whole planet at row
        counts (billions / millions) where an unbounded box is a footgun,
        so this raises a clear `ValueError` when the requested bbox area
        exceeds the per-theme cap (or covers the whole Earth). Other
        themes (e.g. `divisions`) are unguarded.

        Args:
            theme_names: The friendly theme names being requested.

        Raises:
            ValueError: If any guarded theme's bbox area exceeds its cap.
        """
        area = (self.space.east - self.space.west) * (
            self.space.north - self.space.south
        )
        for name in theme_names:
            cap = (
                self._max_bbox_deg2
                if self._max_bbox_deg2 is not None
                else _DEFAULT_MAX_BBOX_DEG2.get(name)
            )
            if cap is None:
                continue
            if area >= _WHOLE_EARTH_DEG2 or area > cap:
                raise ValueError(
                    f"The requested bbox covers {area:.2f} square degrees, "
                    f"which exceeds the {cap:.2f} square-degree cap for the "
                    f"{name!r} theme (its global row count makes an oversized "
                    "box a footgun). Shrink the bbox (lat_lim / lon_lim), pass "
                    "a larger max_bbox_deg2=, or set max_features= to cap the "
                    "result."
                )

    def _search(self) -> list[RemoteProduct]:
        """Plan one `RemoteProduct` per requested `(theme, type)`.

        Resolves `variables` against the catalog and enforces the
        bbox-size guard, then returns one product per type to fetch. No
        network call is made here — the actual GeoParquet pull happens in
        `_fetch`.

        Returns:
            list[RemoteProduct]: One product per `(theme, type)`; each
                `id` is `"<theme>/<type>"` and `metadata` carries the
                `Theme` and the `type` string.

        Raises:
            ValueError: If a theme/type is unknown, or the bbox is too
                large for a guarded theme.
        """
        plan = self._resolve_plan()
        self._guard_bbox(list(self.vars))
        return [
            RemoteProduct(
                id=f"{theme_name}/{overture_type}",
                metadata={
                    "theme_name": theme_name,
                    "theme": theme,
                    "type": overture_type,
                },
            )
            for theme_name, theme, overture_type in plan
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Pull each planned type's GeoParquet and write a FeatureCollection.

        For every product, reads the type's GeoParquet (bbox pushdown via
        PyArrow parquet statistics) — through the SDK's `geodataframe`, or,
        when `stream=True` / `max_features` is set, the streaming
        `record_batch_reader` (see `_read_geodataframe`) — tags the result
        `EPSG:4326`, adds the per-row `license_id` column, warns when
        ODbL-1.0 rows are present, and writes one vector file under `path`.
        The SDK's unit is the Overture *type* (passed positionally) — there
        is no `theme=` kwarg.

        A type that matches no features in the bbox is skipped (a warning
        is logged and no empty file is written), mirroring the shipped
        vector backends; its path is therefore absent from the result.

        Args:
            products: The products returned by `_search`.

        Returns:
            list[Path]: The written vector file paths, in product order;
                a product that matched no features contributes no path.
        """
        from earthlens.overture.collection import to_feature_collection

        bbox = (
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        )
        written: list[Path] = []
        # Resolved once, before the loop: a mid-loop recovery from a failed
        # lookup would otherwise read the first types from the bundled release
        # and the rest from the live one, so a single download() could mix two
        # snapshots. Offline callers also pay one connect attempt, not one per
        # requested type. Its name says what it is: the release the DuckDB
        # path will glob, present exactly when that path is taken, which is
        # the only path needing a concrete id.
        duckdb_release = (
            self._resolve_release() if (self._where or self._columns) else None
        )
        for product in products:
            theme_name = product.metadata["theme_name"]
            overture_type = product.metadata["type"]
            label = product.id
            if duckdb_release is not None:
                from earthlens.overture.query import query_overture

                logger.info(
                    f"Querying Overture {overture_type!r} (theme {theme_name!r}) "
                    f"via DuckDB for bbox {bbox} (release={duckdb_release}, "
                    f"where={self._where!r})"
                )
                gdf = query_overture(
                    theme_name,
                    overture_type,
                    duckdb_release,
                    bbox,
                    where=self._where,
                    columns=self._columns,
                    limit=self._max_features,
                )
            else:
                mode = (
                    "streaming" if (self._stream or self._max_features) else "in-memory"
                )
                logger.info(
                    f"Fetching Overture {overture_type!r} (theme {theme_name!r}) "
                    f"for bbox {bbox} (release={self._release or 'latest'}, {mode})"
                )
                gdf = _read_geodataframe(
                    overture_type,
                    bbox=bbox,
                    release=self._release,
                    max_features=self._max_features,
                    stream=self._stream,
                )
            collection = to_feature_collection(
                gdf, label=label, max_features=self._max_features
            )
            if len(collection) == 0:
                logger.warning(
                    f"{label}: no features matched the bbox; nothing written."
                )
                continue
            out_path = self._write(
                collection, theme_name, overture_type, duckdb_release
            )
            logger.info(f"{label}: wrote {len(collection)} feature(s) to {out_path}")
            written.append(out_path)
        return written

    def _write(
        self,
        collection: FeatureCollection,
        theme_name: str,
        overture_type: str,
        release: str | None = None,
    ) -> Path:
        """Write one type's FeatureCollection to a vector file under `root_dir`.

        The filename embeds the theme, type, and release
        (`overture_<theme>_<type>_<release>.<ext>`). Overture has no
        temporal axis — the release *is* the version — so naming it is
        what keeps successive downloads in distinct files across a
        monthly rollover.

        The DuckDB path knows the concrete release it globbed and passes
        it in. The default path cannot: it hands `release=None` to the SDK
        and never learns which snapshot answered, so an unpinned fetch
        there still writes `..._latest`, and a rollover overwrites the
        previous run. GeoParquet (the default) is written with
        `to_parquet` to preserve Overture's nested schema; GPKG / GeoJSON
        go through `to_file`.

        Args:
            collection: The features to write.
            theme_name: Friendly theme name (for the filename).
            overture_type: Overture feature type (for the filename).
            release: The release the rows were read from, when the caller
                resolved one. `None` falls back to the requested
                `release`, then to `latest`.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _FORMATS[self._file_format]
        stamp = release or self._release or "latest"
        stem = f"overture_{theme_name}_{overture_type}_{stamp}"
        out_path = self.root_dir / f"{stem}.{ext}"
        if driver == "parquet":
            collection.to_parquet(str(out_path))
        else:
            collection.to_file(str(out_path), driver=driver)
        return out_path

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the requested Overture themes and return the written paths.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends.

        Returns:
            list[Path]: The vector file(s) written under `path`, one per
                requested feature type.
        """
        return cast("list[Path]", self._api())


def _read_geodataframe(
    overture_type: str,
    bbox: tuple[float, float, float, float],
    release: str | None,
    max_features: int | None,
    stream: bool,
):
    """Read one Overture type into a `GeoDataFrame`, materialised or streamed.

    The default path is the SDK's `geodataframe` (materialises the whole
    bbox). When `stream` is `True`, or `max_features` is set, the streaming
    `record_batch_reader` is used instead so the read can stop early once
    `max_features` rows are collected rather than fetching the full bbox.

    Args:
        overture_type: The Overture feature type (e.g. `"building"`).
        bbox: `(west, south, east, north)` in degrees (WGS84).
        release: Overture release id, or `None` for the newest.
        max_features: Optional cap; when set, the streamed read stops once
            this many rows are collected and the frame is trimmed to it.
        stream: Force the streaming reader even when `max_features` is
            `None`.

    Returns:
        geopandas.GeoDataFrame: The fetched features (CRS as the SDK
            returns it — the caller tags `EPSG:4326`).
    """
    from overturemaps.core import geodataframe, record_batch_reader

    if not stream and max_features is None:
        return geodataframe(overture_type, bbox=bbox, release=release)
    reader = record_batch_reader(overture_type, bbox=bbox, release=release)
    return _stream_to_geodataframe(reader, max_features)


def _stream_to_geodataframe(reader, max_features: int | None):
    """Assemble a `GeoDataFrame` from a PyArrow `RecordBatchReader`, stopping early.

    Iterates the reader's batches, accumulating until `max_features` rows
    are reached (when set), then builds the frame from the collected
    batches. This is the streaming counterpart to `GeoDataFrame.from_arrow`
    — it avoids reading the whole bbox when a cap is in force.

    Args:
        reader: The SDK's `record_batch_reader` result, or `None` (an empty
            match).
        max_features: Optional row cap; the result is trimmed to it.

    Returns:
        geopandas.GeoDataFrame: The collected rows (empty when the reader is
            `None` or yields no batches).
    """
    import geopandas as gpd
    import pyarrow as pa

    if reader is None:
        return gpd.GeoDataFrame()
    batches = []
    rows = 0
    for batch in reader:
        batches.append(batch)
        rows += batch.num_rows
        if max_features is not None and rows >= max_features:
            break
    if not batches:
        return gpd.GeoDataFrame()
    table = pa.Table.from_batches(batches, schema=reader.schema)
    gdf = gpd.GeoDataFrame.from_arrow(table)
    if max_features is not None and len(gdf) > max_features:
        gdf = gdf.head(max_features)
    return gdf


def _require_overturemaps() -> None:
    """Import-guard the optional `overturemaps` SDK with a friendly error.

    Raises:
        ImportError: If `overturemaps` is not installed, naming the
            `earthlens[overture]` extra to install.
    """
    try:
        import overturemaps  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The Overture backend requires the `overturemaps` SDK. Install "
            "it with `pip install earthlens[overture]`."
        ) from exc
