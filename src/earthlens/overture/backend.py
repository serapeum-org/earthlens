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

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.overture.catalog import Catalog, Theme

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

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

    def __init__(
        self,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        start: str | None = None,
        end: str | None = None,
        temporal_resolution: str = "all",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        release: str | None = None,
        max_features: int | None = None,
        file_format: FileFormat = "geoparquet",
        max_bbox_deg2: float | None = None,
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
            release: Overture release id (`"2026-05-20.0"`). `None` (the
                default) lets the SDK auto-target the newest release.
            max_features: Optional cap on the rows kept per fetched type;
                excess rows are dropped with a warning. `None` keeps all.
            file_format: Output vector format — `"geoparquet"` (default,
                lossless), `"gpkg"`, or `"geojson"`.
            max_bbox_deg2: Optional override of the per-theme bbox-area
                cap (square degrees) applied to the guarded themes. `None`
                uses the built-in per-theme defaults.

        Raises:
            TypeError: If `variables` is not a mapping of theme -> types.
            ValueError: If `file_format` is not one of the supported
                formats, or `variables` is empty.
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
                f"file_format must be one of {sorted(_FORMATS)}, got "
                f"{file_format!r}."
            )
        self._release = release
        self._max_features = max_features
        self._file_format: FileFormat = file_format
        self._max_bbox_deg2 = max_bbox_deg2
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
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

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a `SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` / `end` when set.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are
                `None` when the corresponding argument was `None`.
        """
        start_dt = dt.datetime.strptime(start, fmt) if start else None
        end_dt = dt.datetime.strptime(end, fmt) if end else None
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
        for name, requested in self.vars.items():
            theme = self._catalog.get_theme(name)
            for overture_type in theme.resolve_types(requested):
                plan.append((name, theme, overture_type))
        return plan

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

        For every product, calls the `overturemaps` SDK's
        `geodataframe(<type>, bbox=, release=)` (bbox pushdown via PyArrow
        parquet statistics), tags the result `EPSG:4326`, adds the per-row
        `license_id` column, warns when ODbL-1.0 rows are present, and
        writes one vector file under `path`. The SDK's unit is the Overture
        *type* (passed positionally) — there is no `theme=` kwarg.

        A type that matches no features in the bbox is skipped (a warning
        is logged and no empty file is written), mirroring the shipped
        vector backends; its path is therefore absent from the result.

        Args:
            products: The products returned by `_search`.

        Returns:
            list[Path]: The written vector file paths, in product order;
                a product that matched no features contributes no path.
        """
        from overturemaps.core import geodataframe

        from earthlens.overture.collection import to_feature_collection

        bbox = (
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        )
        written: list[Path] = []
        for product in products:
            theme_name = product.metadata["theme_name"]
            overture_type = product.metadata["type"]
            label = product.id
            logger.info(
                f"Fetching Overture {overture_type!r} (theme {theme_name!r}) "
                f"for bbox {bbox} (release={self._release or 'latest'})"
            )
            gdf = geodataframe(overture_type, bbox=bbox, release=self._release)
            collection = to_feature_collection(
                gdf, label=label, max_features=self._max_features
            )
            if len(collection) == 0:
                logger.warning(
                    f"{label}: no features matched the bbox; nothing written."
                )
                continue
            out_path = self._write(collection, theme_name, overture_type)
            logger.info(f"{label}: wrote {len(collection)} feature(s) to {out_path}")
            written.append(out_path)
        return written

    def _write(
        self,
        collection: FeatureCollection,
        theme_name: str,
        overture_type: str,
    ) -> Path:
        """Write one type's FeatureCollection to a vector file under `root_dir`.

        The filename embeds the theme, type, and release
        (`overture_<theme>_<type>_<release>.<ext>`) so successive
        downloads land in distinct files. GeoParquet (the default) is
        written with `to_parquet` to preserve Overture's nested schema;
        GPKG / GeoJSON go through `to_file`.

        Args:
            collection: The features to write.
            theme_name: Friendly theme name (for the filename).
            overture_type: Overture feature type (for the filename).

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _FORMATS[self._file_format]
        stem = f"overture_{theme_name}_{overture_type}_{self._release or 'latest'}"
        out_path = self.root_dir / f"{stem}.{ext}"
        if driver == "parquet":
            collection.to_parquet(str(out_path))
        else:
            collection.to_file(str(out_path), driver=driver)
        return out_path

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the requested Overture themes and return the written paths.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends.
            aggregate: Must be `None`. Overture is vector, not gridded, so
                there is no meaningful aggregation. The facade already
                rejects a non-`None` `aggregate=` for a `vector` backend;
                this is the belt-and-suspenders guard for direct callers.

        Returns:
            list[Path]: The vector file(s) written under `path`, one per
                requested feature type.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "Overture.download(aggregate=...) is not supported: Overture "
                "features are vector, not gridded rasters, so there is no "
                "meaningful gridded reduction. Call download() without "
                "aggregate= and post-process the returned FeatureCollection "
                "(a GeoDataFrame) directly."
            )
        return self._api()


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
