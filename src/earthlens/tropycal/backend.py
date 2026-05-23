"""Backend that fetches tropical-cyclone best tracks via tropycal.

`TropicalCyclone(AbstractDataSource)` wraps `tropycal.tracks.TrackDataset`
so a user can pull a space/time window of cyclone best tracks — from
IBTrACS (global) or HURDAT2 (North Atlantic / East Pacific reanalysis) —
through the same `download()` shape every other earthlens backend uses.
Each basin code in `variables` becomes one `TrackDataset` load; the
storms in the requested seasons are mapped to a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` by
:mod:`earthlens.tropycal.events`.

This is a `vector` backend: the result is a table of track features
(per-fix `Point`s by default, or one `LineString` per storm with
`geometry="track"`), not a gridded array, so `OUTPUT_KIND = "vector"` and
the :class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=`
argument. `download()` returns the in-memory FeatureCollection (the union
across requested basins) and writes one vector file per basin to `path`.

tropycal needs **no credentials** — it fetches best-track files from
NCEI / NHC over HTTPS itself — so there is no `auth.py`. It is an
optional dependency (the `[tropycal]` extra), imported lazily inside
:meth:`_get_track_dataset` so the package imports without it.

Basin selection follows the vector-backend reading of `variables`: it is
a `list[str]` of basin codes (`["north_atlantic"]`,
`["north_atlantic", "east_pacific"]`), **not** data-variable names. The
data source (`"ibtracs"` / `"hurdat"`) and geometry mode (`"point"` /
`"track"`) arrive as explicit constructor kwargs. The temporal window is
filtered client-side at the fix level (`G4`), so `temporal_resolution`
carries the sentinel `"all"`.
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
from earthlens.tropycal import events
from earthlens.tropycal.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

FileFormat = Literal["gpkg", "geojson"]
Geometry = Literal["point", "track"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Default basin when `variables` is empty.
_DEFAULT_BASINS = ["north_atlantic"]

#: The two tropycal data sources (there is no `jtwc` source in 1.4).
_SOURCES = ("ibtracs", "hurdat")


class TropicalCyclone(AbstractDataSource):
    """Tropical-cyclone best-track backend (vector track-feature output).

    Wraps `tropycal.tracks.TrackDataset` so a user can request a
    space/time window of cyclone best tracks through the same
    `download()` shape every other earthlens backend uses. Each basin
    code in `variables` becomes one (slow, cached) `TrackDataset` load;
    the storms in the requested seasons are filtered to the window + bbox
    and mapped to a :class:`~pyramids.feature.collection.FeatureCollection`,
    and the basins' results are unioned into the single FeatureCollection
    `download()` returns.

    Best-track only — tropycal's realtime / operational / forecast
    products are out of scope. No credentials are needed.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of track
            features, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "all",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        source: str = "ibtracs",
        geometry: Geometry = "point",
        min_category: int | None = None,
        storm_type: str | None = None,
        file_format: FileFormat = "gpkg",
    ):
        """Initialise a Tropical-cyclone backend instance.

        Args:
            start: Inclusive start of the track window, as a string
                parsed with `fmt`.
            end: Inclusive end of the track window.
            variables: List of basin codes to query
                (`["north_atlantic"]`, `["north_atlantic",
                "east_pacific"]`). For this backend `variables` names the
                *basins*, not data variables (see the package docstring).
                An empty list defaults to `["north_atlantic"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: tropycal filters by a date window, not a
                daily/monthly cadence, so this is the sentinel `"all"`,
                not a pandas frequency alias.
            path: Output directory for the per-basin vector files.
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            source: tropycal data source — `"ibtracs"` (default, global
                coverage) or `"hurdat"` (North Atlantic / East Pacific
                reanalysis only). Note this overrides tropycal's own
                default of `"hurdat"`.
            geometry: Output geometry — `"point"` (default, one feature
                per 6-hourly fix) or `"track"` (one `LineString` per
                storm with summary attributes).
            min_category: Optional Saffir-Simpson floor; fixes below this
                category are dropped. `None` keeps every fix. In
                `geometry="track"` mode the filter applies at the fix
                level *before* the LineString is built, so a storm that
                only briefly reaches the floor yields a track drawn from
                just its qualifying fixes (a clipped / shortened path),
                not its whole track.
            storm_type: Optional tropycal storm-type filter (`"HU"`,
                `"TS"`, …); keeps only fixes of that type. `None` keeps
                every type. Same fix-level, track-clipping behaviour as
                `min_category` in `geometry="track"` mode.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.

        Raises:
            ValueError: If `file_format` is not `"gpkg"`/`"geojson"`, if
                `source` is not `"ibtracs"`/`"hurdat"`, or if `geometry`
                is not `"point"`/`"track"`.
            TypeError: If `variables` is a mapping rather than a list of
                basin codes.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got "
                f"{file_format!r}."
            )
        if source not in _SOURCES:
            raise ValueError(
                f"source must be one of {list(_SOURCES)}, got {source!r}. "
                "tropycal 1.4 has no 'jtwc' source."
            )
        if geometry not in ("point", "track"):
            raise ValueError(
                f"geometry must be 'point' or 'track', got {geometry!r}."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "TropicalCyclone `variables` must be a list of basin codes "
                "(e.g. ['north_atlantic', 'east_pacific']), not a mapping. "
                "For this backend `variables` selects basins, not data "
                "variables; source/geometry/filters are explicit keyword "
                "arguments."
            )
        self._source = source
        self._geometry: Geometry = geometry
        self._min_category = min_category
        self._storm_type = storm_type
        self._file_format: FileFormat = file_format
        self._catalog = Catalog()
        # Per-process memo of loaded TrackDatasets, keyed (basin, source),
        # so a multi-basin/multi-year request loads each basin once (G3).
        self._track_datasets: dict[tuple[str, str], object] = {}
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_BASINS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """No auth, no client — tropycal fetches public best-track files.

        Returns:
            None: No per-instance client object.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        The bbox is applied client-side at the fix level in
        :meth:`_query_one`, so it passes through unchanged.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        tropycal is queried per season (calendar year) and filtered at the
        fix level, so there is no per-date loop. The resolution is kept as
        the sentinel `"all"` and `dates` collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Ignored beyond being recorded as the
                resolution label.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _search(self) -> list[RemoteProduct]:
        """One :class:`RemoteProduct` per requested basin.

        Resolves each basin code in `self.vars` against the bundled
        catalog (raising with a did-you-mean hint on an unknown code) and
        validates that the requested `source` actually serves the basin
        (`hurdat` only serves the North Atlantic / East Pacific). No
        network call is made here.

        Returns:
            list[RemoteProduct]: One product per basin, in request order;
                `id` is the basin code and `metadata` carries `source`.

        Raises:
            ValueError: If a code in `self.vars` is not a registered
                basin, or if the `(basin, source)` pair is invalid.
        """
        products: list[RemoteProduct] = []
        for basin in self.vars:
            sources = self._catalog.sources_for(basin)
            if self._source not in sources:
                raise ValueError(
                    f"source {self._source!r} does not serve basin "
                    f"{basin!r}; {basin!r} is served by {sources}. "
                    "Pass a supported source= for this basin."
                )
            products.append(
                RemoteProduct(id=basin, metadata={"source": self._source})
            )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Load each basin's TrackDataset and map it to a FeatureCollection.

        Widens the inherited `-> list[Path]` contract: a vector backend
        returns in-memory :class:`FeatureCollection`s, not file paths. One
        collection per product, in request order.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[FeatureCollection]: One collection per basin, same order.
        """
        return [self._query_one(product) for product in products]

    def _query_one(self, product: RemoteProduct) -> FeatureCollection:
        """Load one basin's TrackDataset and map its storms to features.

        Iterates the requested seasons (`start.year .. end.year`), pulls
        each storm's `to_dataframe(attrs_as_columns=True)`, applies the
        optional `storm_type` / `min_category` fix filters, and hands the
        per-storm frames to :func:`earthlens.tropycal.events.frame_to_fc`
        for the window + bbox filter and geometry mapping.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`;
                `product.id` is the basin code.

        Returns:
            FeatureCollection: The basin's matched track features (empty
                on no match).
        """
        basin = product.id
        source = product.metadata["source"]
        track_dataset = self._get_track_dataset(basin, source)

        frames: list[pd.DataFrame] = []
        for year in range(self.time.start_date.year, self.time.end_date.year + 1):
            for storm_id in self._season_storm_ids(track_dataset, year):
                frame = self._storm_frame(track_dataset, storm_id)
                if frame is not None and len(frame):
                    frames.append(self._apply_fix_filters(frame))

        return events.frame_to_fc(
            frames,
            geometry=self._geometry,
            window=(self.time.start_date, self.time.end_date),
            bbox=(
                self.space.south,
                self.space.north,
                self.space.west,
                self.space.east,
            ),
            source=source,
        )

    def _get_track_dataset(self, basin: str, source: str) -> object:
        """Return the memoised `TrackDataset` for `(basin, source)`.

        Builds it on first use (a slow call — tropycal downloads and
        parses the whole basin best-track file) and caches it on the
        instance so a multi-year / multi-storm request reuses one load
        (`G3`). The `tropycal` import lives here so the package imports
        without the `[tropycal]` extra.

        Args:
            basin: The basin code.
            source: The data source (`"ibtracs"`/`"hurdat"`).

        Returns:
            The `tropycal.tracks.TrackDataset` instance.

        Raises:
            ImportError: If the `[tropycal]` extra is not installed.
        """
        key = (basin, source)
        cached = self._track_datasets.get(key)
        if cached is not None:
            return cached
        try:
            import tropycal.tracks as tracks
        except ImportError as exc:
            raise ImportError(
                "The Tropycal backend needs the `tropycal` package. Install "
                "it with `pip install earthlens[tropycal]`."
            ) from exc
        logger.info(
            f"Loading tropycal TrackDataset(basin={basin!r}, source={source!r}); "
            "the first load downloads + parses the whole basin best-track file "
            "and can be slow."
        )
        track_dataset = tracks.TrackDataset(basin=basin, source=source)
        self._track_datasets[key] = track_dataset
        return track_dataset

    @staticmethod
    def _season_storm_ids(track_dataset: object, year: int) -> list[str]:
        """Return the storm ids in one season, or `[]` when none.

        A season with no storms (or a year tropycal cannot serve) is
        logged and skipped rather than aborting the whole request.

        Args:
            track_dataset: A loaded `TrackDataset`.
            year: Calendar year of the season.

        Returns:
            list[str]: The season's storm ids.
        """
        try:
            season = track_dataset.get_season(year)
            ids = season.summary().get("id") or []
            return list(ids)
        except Exception as exc:  # noqa: BLE001 - skip an unservable season
            logger.warning(f"tropycal season {year} skipped: {type(exc).__name__}: {exc}")
            return []

    @staticmethod
    def _storm_frame(track_dataset: object, storm_id: str) -> pd.DataFrame | None:
        """Return one storm's per-fix DataFrame, or `None` on failure.

        Args:
            track_dataset: A loaded `TrackDataset`.
            storm_id: A storm id from the season summary.

        Returns:
            The storm's `to_dataframe(attrs_as_columns=True)` output, or
            `None` when the storm cannot be read.
        """
        try:
            storm = track_dataset.get_storm(storm_id)
            return storm.to_dataframe(attrs_as_columns=True)
        except Exception as exc:  # noqa: BLE001 - skip an unreadable storm
            logger.warning(
                f"tropycal storm {storm_id!r} skipped: {type(exc).__name__}: {exc}"
            )
            return None

    def _apply_fix_filters(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Apply the optional `storm_type` / `min_category` fix filters.

        Args:
            frame: One storm's per-fix DataFrame.

        Returns:
            The frame with non-matching fixes removed (a copy; the input
            is not mutated). Returns the frame unchanged when no filter
            is set.
        """
        if self._storm_type is None and self._min_category is None:
            return frame
        filtered = frame
        if self._storm_type is not None and "type" in filtered.columns:
            filtered = filtered[filtered["type"] == self._storm_type]
        if self._min_category is not None and "vmax" in filtered.columns:
            categories = filtered["vmax"].map(events.saffir_simpson_category)
            filtered = filtered[categories >= self._min_category]
        return filtered

    def _api(self) -> list[FeatureCollection]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> FeatureCollection:
        """Load every requested basin and return the unioned tracks.

        Each basin in `self.vars` is loaded once; its matched features are
        written to one vector file under `path` (named after the basin),
        and the per-basin results are concatenated into the single
        :class:`FeatureCollection` returned. An all-empty result returns a
        schema-correct empty FeatureCollection and writes nothing.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; tropycal has no progress bar, so this is a
                no-op.
            aggregate: Must be `None`. Tracks are vector, not gridded, so
                there is no meaningful aggregation. The facade already
                rejects a non-`None` `aggregate=` for a `vector` backend;
                this is the belt-and-suspenders guard for direct backend
                callers.

        Returns:
            FeatureCollection: The row-wise union of every requested
                basin's track features, CRS `EPSG:4326`. Empty
                (schema-only) when nothing matched.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
            ImportError: If the `[tropycal]` extra is not installed.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "TropicalCyclone.download(aggregate=...) is not supported: "
                "cyclone tracks are vector features, not gridded rasters, so "
                "there is no meaningful gridded reduction. Call download() "
                "without aggregate= and post-process the returned "
                "FeatureCollection (a GeoDataFrame) directly."
            )

        products = self._search()
        collections = self._fetch(products) if products else []

        written: list[Path] = []
        for product, collection in zip(products, collections):
            if len(collection):
                written.append(self._write(product.id, collection))

        combined = events.concat_fcs(collections, self._geometry)
        if written:
            logger.info(
                f"Tropycal download summary: {len(combined)} feature(s) across "
                f"{len(written)} file(s) written to {self.root_dir}"
            )
        else:
            logger.warning(
                "Tropycal download summary: no tracks matched the request, "
                "nothing written"
            )
        return combined

    def _write(self, basin: str, collection: FeatureCollection) -> Path:
        """Write one basin's features to a vector file under `root_dir`.

        Args:
            basin: The basin code, used as the filename stem.
            collection: The basin's track features.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        out_path = self.root_dir / f"tropycal_{basin}_{self._geometry}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path
