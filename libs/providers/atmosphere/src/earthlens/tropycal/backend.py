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
from typing import TYPE_CHECKING, Any, Literal, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.tropycal import events
from earthlens.tropycal.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection


FileFormat = Literal["gpkg", "geojson"]
Geometry = Literal["point", "track"]
Product = Literal["besttrack", "recon", "ships", "realtime"]
ReconProduct = Literal["hdobs", "dropsondes", "vdms"]

#: Products: `besttrack` (default) is basin+window keyed (vector); `recon`
#: is storm-keyed aircraft observations (vector); `ships` is storm +
#: forecast-cycle keyed SHIPS guidance (tabular); `realtime` is live active
#: storms (vector, no date window).
_PRODUCTS = ("besttrack", "recon", "ships", "realtime")
_RECON_PRODUCTS = ("hdobs", "dropsondes", "vdms")

#: Products that are storm-keyed (variables are storm ids, not basins).
#: `realtime` is excluded: its `variables` are optional (empty = all active).
_STORM_KEYED = ("recon", "ships")

#: A window wide enough to admit every realtime fix (realtime has no
#: `[start, end]`; only the bbox filters).
_OPEN_WINDOW = (dt.datetime(1800, 1, 1), dt.datetime(2200, 1, 1))

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

    Four products, selected with `product=`: `besttrack` (the default),
    `recon` aircraft observations, `ships` forecast guidance, and
    `realtime` active storms. No credentials are needed.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of track
            features, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "tropycal products are vector features or tabular guidance, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned GeoDataFrame / DataFrame directly"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        source: str = "ibtracs",
        geometry: Geometry = "point",
        min_category: int | None = None,
        storm_type: str | None = None,
        file_format: FileFormat = "gpkg",
        product: Product = "besttrack",
        recon_product: ReconProduct = "hdobs",
        basin: str = "north_atlantic",
        ships_time: str | None = None,
        realtime_jtwc: bool = False,
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
            product: Which tropycal product to fetch. `"besttrack"`
                (default) is basin + date-window keyed and `variables` is a
                list of basin codes. `"recon"` is storm-keyed (aircraft
                reconnaissance observations for named storms): `variables`
                is then a list of storm identifiers and `basin` / `source`
                say where to resolve them.
            recon_product: For `product="recon"`, which recon sub-product
                to map — `"hdobs"` (default, high-density flight-level
                observations), `"dropsondes"`, or `"vdms"`.
            basin: For storm-keyed products (`product="recon"` / `"ships"`),
                the basin whose `TrackDataset` is loaded to resolve the storm
                identifiers in `variables`. Ignored for `"besttrack"` (where
                `variables` *are* the basins) and `"realtime"`.
            ships_time: For `product="ships"`, the SHIPS forecast-init cycle
                as a datetime string (e.g. `"2022-09-27 00:00"`); SHIPS
                guidance is issued per cycle, so this is required for ships
                and ignored otherwise.
            realtime_jtwc: For `product="realtime"`, whether to source active
                storms from JTWC (`True`) instead of NHC (`False`, default).
                Ignored for the other products.

        Raises:
            ValueError: If `file_format`, `source`, `geometry`, `product`, or
                `recon_product` is not a recognised value; if a storm-keyed
                product is given an empty `variables`; or if `product="ships"`
                is given no `ships_time`.
            TypeError: If `variables` is a mapping rather than a list.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        if source not in _SOURCES:
            raise ValueError(
                f"source must be one of {list(_SOURCES)}, got {source!r}. "
                "tropycal 1.4 has no 'jtwc' source."
            )
        if geometry not in ("point", "track"):
            raise ValueError(f"geometry must be 'point' or 'track', got {geometry!r}.")
        if product not in _PRODUCTS:
            raise ValueError(
                f"product must be one of {list(_PRODUCTS)}, got {product!r}."
            )
        if recon_product not in _RECON_PRODUCTS:
            raise ValueError(
                f"recon_product must be one of {list(_RECON_PRODUCTS)}, got "
                f"{recon_product!r}."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "TropicalCyclone `variables` must be a list (basin codes for "
                "product='besttrack', storm identifiers for product='recon'), "
                "not a mapping."
            )
        if product in _STORM_KEYED and not list(variables):
            raise ValueError(
                f"product={product!r} is storm-keyed: `variables` must list "
                "at least one storm identifier (e.g. ['AL092022'])."
            )
        if product == "ships" and not ships_time:
            raise ValueError(
                "product='ships' needs ships_time=<forecast-init datetime> "
                "(e.g. '2022-09-27 00:00'); SHIPS guidance is per cycle."
            )
        self._source = source
        self._geometry: Geometry = geometry
        self._min_category = min_category
        self._storm_type = storm_type
        self._file_format: FileFormat = file_format
        self._product: Product = product
        self._recon_product: ReconProduct = recon_product
        self._basin = basin
        self._ships_time = ships_time
        self._realtime_jtwc = realtime_jtwc
        # ships is tabular (a forecast-guidance table); the others are
        # vector. The facade reads this instance attribute to decide whether
        # `aggregate=` is allowed (it is not, for either kind).
        self.OUTPUT_KIND = "tabular" if product == "ships" else "vector"
        self._catalog = Catalog()
        # Per-process memo of loaded TrackDatasets, keyed (basin, source),
        # so a multi-basin/multi-year request loads each basin once (G3).
        self._track_datasets: dict[tuple[str, str], object] = {}
        # besttrack: empty `variables` defaults to North Atlantic. recon:
        # `variables` are storm ids (already validated non-empty above).
        resolved_vars = (
            list(variables) or list(_DEFAULT_BASINS)
            if product == "besttrack"
            else list(variables)
        )
        super().__init__(
            start=start,
            end=end,
            variables=resolved_vars,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

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
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="all")

    def _search(self) -> list[RemoteProduct]:
        """One :class:`RemoteProduct` per requested unit.

        For `product="besttrack"` that is one product per basin (each
        basin/source pair validated against the catalog). For
        `product="recon"` it is one product per storm identifier (resolved
        against `basin`/`source` at fetch time). No network call is made
        here.

        Returns:
            list[RemoteProduct]: One product per basin (besttrack) or per
                storm id (recon), in request order.

        Raises:
            ValueError: If a besttrack basin is unknown or its
                `(basin, source)` pair is invalid.
        """
        if self._product in _STORM_KEYED:
            return [
                RemoteProduct(
                    id=str(storm_id),
                    metadata={
                        "basin": self._basin,
                        "source": self._source,
                        "recon_product": self._recon_product,
                    },
                )
                for storm_id in self.vars
            ]
        products: list[RemoteProduct] = []
        for basin in self.vars:
            sources = self._catalog.sources_for(basin)
            if self._source not in sources:
                raise ValueError(
                    f"source {self._source!r} does not serve basin "
                    f"{basin!r}; {basin!r} is served by {sources}. "
                    "Pass a supported source= for this basin."
                )
            products.append(RemoteProduct(id=basin, metadata={"source": self._source}))
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
                Truncated when `limit=` was passed to `download`.
        """
        # Lazy so a `limit=` stops the work: a basin past the cap never has its
        # TrackDataset loaded, rather than being loaded and then trimmed away.
        return self._take_limited(
            (self._query_one(product) for product in products),
            limit=self._limit,
        )

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
        if self._product == "recon":
            return self._query_recon(product)

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

    def _query_recon(self, product: RemoteProduct) -> FeatureCollection:
        """Fetch one storm's recon observations and map them to points.

        Resolves the storm via the basin `TrackDataset`, loads the chosen
        recon sub-product (`hdobs` / `dropsondes` / `vdms`) for it, and maps
        the observation DataFrame to a window+bbox-filtered point
        FeatureCollection via
        :func:`earthlens.tropycal.events.recon_to_fc`.

        Args:
            product: A :class:`RemoteProduct` from :meth:`_search`;
                `product.id` is the storm identifier.

        Returns:
            FeatureCollection: The storm's recon observation points (empty
                on no match / no recon data).
        """
        basin = product.metadata["basin"]
        source = product.metadata["source"]
        recon_product = product.metadata["recon_product"]
        track_dataset = self._get_track_dataset(basin, source)

        storm = self._get_storm(track_dataset, product.id)
        frame = self._recon_frame(storm, recon_product) if storm is not None else None
        return events.recon_to_fc(
            frame,
            storm_id=product.id,
            recon_product=recon_product,
            window=(self.time.start_date, self.time.end_date),
            bbox=(
                self.space.south,
                self.space.north,
                self.space.west,
                self.space.east,
            ),
            source=source,
        )

    @staticmethod
    def _get_storm(track_dataset: Any, storm_id: str) -> object | None:
        """Resolve a storm by id, or `None` (logged) when it cannot be read."""
        try:
            return cast("object | None", track_dataset.get_storm(storm_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"tropycal storm {storm_id!r} not resolved: {type(exc).__name__}: {exc}"
            )
            return None

    def _recon_frame(self, storm: object, recon_product: str):
        """Return a storm's recon observation DataFrame, or `None` on failure.

        Lazy-imports `tropycal.recon` and reads the chosen sub-product's
        `.data` frame. A storm with no recon data (most storms) yields
        `None`, which maps to an empty collection.
        """
        try:
            import tropycal.recon as recon
        except ImportError as exc:
            raise ImportError(
                "The Tropycal recon product needs the `tropycal` package. "
                "Install it with `pip install earthlens[tropycal]`."
            ) from exc
        builders = {
            "hdobs": recon.hdobs,
            "dropsondes": recon.dropsondes,
            "vdms": recon.vdms,
        }
        try:
            obj = builders[recon_product](storm)
            return getattr(obj, "data", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"tropycal recon {recon_product} unavailable for storm: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

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
    def _season_storm_ids(track_dataset: Any, year: int) -> list[str]:
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
        except Exception as exc:  # noqa: BLE001
            # Intentionally broad (matches the FDSN/CMEMS "one bad item does
            # not kill the batch" policy): a single unservable season is
            # logged with its exception type and skipped so a multi-year
            # request still returns the seasons that loaded. The type+message
            # are logged so a genuine bug is visible rather than silent.
            logger.warning(
                f"tropycal season {year} skipped: {type(exc).__name__}: {exc}"
            )
            return []

    @staticmethod
    def _storm_frame(track_dataset: Any, storm_id: str) -> pd.DataFrame | None:
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
            return cast(
                "pd.DataFrame | None", storm.to_dataframe(attrs_as_columns=True)
            )
        except Exception as exc:  # noqa: BLE001
            # Intentionally broad (batch resilience, as above): one unreadable
            # storm is logged with its exception type and skipped rather than
            # aborting the whole basin.
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

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> FeatureCollection | pd.DataFrame:
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
            limit: Cap on the total features (or SHIPS rows) returned, across
                every requested basin / storm. Applied as each one is loaded,
                so a basin or storm **past** the cap is never pulled. Within a
                single item it can only trim: a basin's `TrackDataset` is
                parsed whole before any of its storms can be filtered, so a
                one-basin request (the default) pays the full load whatever the
                cap. `None` (the default) loads everything.

        Returns:
            FeatureCollection: The row-wise union of every requested
                basin's track features, CRS `EPSG:4326`. Empty
                (schema-only) when nothing matched.

        Raises:
            ImportError: If the `[tropycal]` extra is not installed.
        """
        self._limit = self.check_limit(limit)
        if self._product == "ships":
            return self._download_ships()
        if self._product == "realtime":
            return self._download_realtime()

        products = self._search()
        collections = self._fetch(products) if products else []

        written: list[Path] = []
        for product, collection in zip(products, collections):
            if len(collection):
                written.append(self._write(product.id, collection))

        if self._product == "recon":
            combined = events.concat_recon_fcs(collections)
        else:
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

    def _download_realtime(self) -> FeatureCollection:
        """Fetch live active storms and map their current tracks to features.

        `product="realtime"` has no date window: it pulls whatever storms are
        active *now* from `tropycal.realtime.Realtime`. `variables` (if given)
        selects active storm ids; empty means every active storm. Each
        `RealtimeStorm` is `Storm`-shaped, so its `to_dataframe` maps through
        the same point/track mapper as best tracks (with an open time window
        — only the bbox filters). An empty result (no active storms, or none
        in the bbox) returns a schema-correct empty FeatureCollection.

        Returns:
            FeatureCollection: Current-track features for the active storms,
                CRS `EPSG:4326`.
        """
        realtime = self._get_realtime()
        active = list(realtime.list_active_storms())
        requested = set(self.vars)
        ids = [s for s in active if not requested or s in requested]
        if not ids:
            logger.warning(
                "Tropycal realtime: no matching active storms right now, "
                "nothing written"
            )
            return events.empty_fc(self._geometry)

        bbox = (self.space.south, self.space.north, self.space.west, self.space.east)
        # Lazy so a `limit=` stops the work: a storm past the cap is never
        # pulled. Writing happens *after* the cap is applied, matching the
        # best-track path — writing inside the generator would put the
        # untrimmed fragment on disk while returning the trimmed one, so for
        # the storm the cap lands inside the file and the return value would
        # disagree.
        collections = self._take_limited(
            (self._realtime_one(realtime, storm_id, bbox) for storm_id in ids),
            limit=self._limit,
        )
        written = [
            self._write(storm_id, collection)
            for storm_id, collection in zip(ids, collections)
            if len(collection)
        ]

        combined = events.concat_fcs(collections, self._geometry)
        logger.info(
            f"Tropycal realtime: {len(combined)} feature(s) across "
            f"{len(written)} file(s) written to {self.root_dir}"
        )
        return combined

    def _get_realtime(self) -> Any:
        """Build a `tropycal.realtime.Realtime` (lazy import; one live fetch)."""
        try:
            import tropycal.realtime as realtime
        except ImportError as exc:
            raise ImportError(
                "The Tropycal realtime product needs the `tropycal` package. "
                "Install it with `pip install earthlens[tropycal]`."
            ) from exc
        logger.info("Loading tropycal realtime active-storm data (live fetch).")
        return realtime.Realtime(jtwc=self._realtime_jtwc)

    @staticmethod
    def _realtime_storm_frame(realtime: Any, storm_id: str) -> pd.DataFrame | None:
        """Return one active storm's current-track frame, or `None` on failure."""
        try:
            storm = realtime.get_storm(storm_id)
            return cast(
                "pd.DataFrame | None", storm.to_dataframe(attrs_as_columns=True)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"tropycal realtime storm {storm_id!r} skipped: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def _realtime_one(
        self,
        realtime: Any,
        storm_id: str,
        bbox: tuple[float, float, float, float],
    ) -> FeatureCollection:
        """Pull one active storm's current track.

        Fetch only — the caller writes what survives the cap, so the file on
        disk always matches the returned features.

        Args:
            realtime: The `tropycal.realtime.Realtime` object to read from.
            storm_id: The active storm's identifier.
            bbox: The request's `(south, north, west, east)` filter.

        Returns:
            FeatureCollection: The storm's current-track features (empty when
                it fell outside the bbox).
        """
        frame = self._realtime_storm_frame(realtime, storm_id)
        return events.frame_to_fc(
            [frame] if frame is not None else [],
            geometry=self._geometry,
            window=_OPEN_WINDOW,
            bbox=bbox,
            source="realtime",
        )

    def _download_ships(self) -> pd.DataFrame:
        """Fetch SHIPS guidance for each storm and return one tabular frame.

        SHIPS is `product="ships"` — a tabular (not geographic) forecast
        guidance table per `(storm, forecast-init cycle)`. Each requested
        storm's `Ships.to_dataframe()` is prefixed with `storm_id` /
        `forecast_init` columns and the per-storm tables are concatenated.
        A storm with no SHIPS guidance for the cycle is skipped (logged).
        Each storm's table is also written to a CSV under `path`.

        Returns:
            pd.DataFrame: The concatenated SHIPS guidance (empty with
                `storm_id`/`forecast_init`/`fhr` columns when nothing matched).
        """
        init = pd.to_datetime(self._ships_time).to_pydatetime()
        products = self._search()
        # Lazy so a `limit=` stops the work: a storm past the cap never has its
        # guidance pulled. Storms with no guidance are dropped before the cap
        # counts them, so `limit` bounds returned rows only.
        frames = self._take_limited(
            (
                frame
                for frame in (self._ships_one(product, init) for product in products)
                if frame is not None
            ),
            limit=self._limit,
        )
        # Written after the cap, so a storm the cap lands inside has the same
        # rows on disk as in the returned frame. Each frame carries its own
        # `storm_id` (stamped in `_ships_one`), so the surviving frames can be
        # written without re-pairing them with `products` — which the
        # `None`-dropping filter above has already unaligned.
        written = [
            self._write_table(str(frame["storm_id"].iloc[0]), init, frame)
            for frame in frames
        ]
        if not frames:
            logger.warning(
                "Tropycal ships: no SHIPS guidance matched the request, nothing written"
            )
            return pd.DataFrame(columns=["storm_id", "forecast_init", "fhr"])
        combined = pd.concat(frames, ignore_index=True)
        logger.info(
            f"Tropycal ships: {len(combined)} rows across {len(written)} "
            f"file(s) written to {self.root_dir}"
        )
        return combined

    def _ships_one(
        self, product: RemoteProduct, init: dt.datetime
    ) -> pd.DataFrame | None:
        """Pull one storm's SHIPS table for `init`.

        Fetch only — the caller writes what survives the cap, so the file on
        disk always matches the returned rows.

        Args:
            product: One product from `_search`; `product.id` is the storm id.
            init: The forecast initialisation time to request.

        Returns:
            pd.DataFrame | None: The storm's guidance rows, stamped with
                `storm_id` / `forecast_init`, or `None` when the storm has no
                guidance for this cycle.
        """
        track_dataset = self._get_track_dataset(
            product.metadata["basin"], product.metadata["source"]
        )
        storm = self._get_storm(track_dataset, product.id)
        df = self._ships_frame(storm, init) if storm is not None else None
        if df is None or not len(df):
            return None
        df = df.copy()
        df.insert(0, "forecast_init", pd.Timestamp(init))
        df.insert(0, "storm_id", product.id)
        return df

    @staticmethod
    def _ships_frame(storm: Any, init: dt.datetime) -> pd.DataFrame | None:
        """Return a storm's SHIPS table for `init`, or `None` (logged) if absent."""
        try:
            return cast("pd.DataFrame | None", storm.get_ships(init).to_dataframe())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"tropycal SHIPS unavailable for the requested storm/cycle: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def _write_table(self, storm_id: str, init: dt.datetime, df: pd.DataFrame) -> Path:
        """Write one storm's SHIPS table to a CSV under `root_dir`."""
        stem = f"tropycal_ships_{storm_id}_{init:%Y%m%dT%H}"
        out_path = self.root_dir / f"{stem}.csv"
        df.to_csv(out_path, index=False)
        return out_path

    def _write(self, unit: str, collection: FeatureCollection) -> Path:
        """Write one unit's features to a vector file under `root_dir`.

        Args:
            unit: The basin code (besttrack) or storm id (recon), used as
                the filename stem.
            collection: The unit's features.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        label = self._recon_product if self._product == "recon" else self._geometry
        out_path = self.root_dir / f"tropycal_{self._product}_{unit}_{label}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path


#: Convenience alias matching the package / library name (the other backend
#: classes are named after their provider / library). `TropicalCyclone` stays
#: the canonical, descriptive class name.
Tropycal = TropicalCyclone
