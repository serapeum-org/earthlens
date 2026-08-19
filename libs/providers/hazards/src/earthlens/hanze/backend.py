"""Backend that fetches HANZE historical European flood events and impacts.

`HANZE(AbstractDataSource)` downloads the HANZE (Historical Analysis of Natural
Hazards in Europe) database of observed European flood events and their impacts
(Paprotny et al.) from its pinned static Zenodo release, filters it, and returns
the event / impact records as a :class:`pandas.DataFrame`. It is the observed
hazard -> loss record — real floods with fatalities, persons affected, area
flooded and economic losses — so a modelled event set can be validated against
the *observed* loss distribution. Companion to the global `emdat` backend.

Three design points carry this backend:

* **Per-instance `OUTPUT_KIND`.** The default is `tabular`, returning a
  :class:`pandas.DataFrame` of events + impacts. Passing `with_geometry=True`
  makes the instance `vector`: it additionally downloads the NUTS-3 region
  boundary shapefile and returns a pyramids
  :class:`~pyramids.feature.collection.FeatureCollection` of the affected
  regions (the `emdat` / `eumetsat` per-instance pattern). The facade reads the
  instance attribute to know the return shape and to gate `aggregate=`.
* **Direct file download, not range-read.** HANZE ships small individual Zenodo
  objects (a 618 KB events CSV, a 2.4 MB region zip), so each is fetched whole
  with :class:`~earthlens.base.http.HttpClient` and cached under `path` — none
  of caravan's multi-GB range-read machinery applies.
* **No new dependency.** `HttpClient` + pandas + `base/archive` + pyramids are
  all core, and the Zenodo record is public (CC-BY-4.0), so there is no auth and
  no `[hanze]` extra.

These are event records, not gridded rasters, so `aggregate=` is refused and
nothing here imports a gridded-array library (no `xarray`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.archive import extract_members
from earthlens.base.http import HttpClient
from earthlens.hanze import geometry as geometry_module
from earthlens.hanze.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: Global sentinel bounds — a request without an explicit bbox covers Europe-wide.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


def _as_list(value: str | list[str] | None) -> list[str]:
    """Return a selector argument as a list of strings.

    Args:
        value: A single value, a list of values, or `None`.

    Returns:
        list[str]: `[]` for `None`, `[value]` for a bare string, else the list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _normalize_country(country: str | list[str] | None) -> set[str]:
    """Validate and upper-case the requested ISO2 country codes.

    Args:
        country: One ISO2 code, a list of them, or `None` to keep every country.

    Returns:
        set[str]: The upper-cased codes; empty when `None`.

    Raises:
        ValueError: If a value is not two ASCII letters — an unrecognised code
            would otherwise filter every row away and look like an empty result.
    """
    codes: set[str] = set()
    for raw in _as_list(country):
        code = raw.strip().upper()
        if not (len(code) == 2 and code.isalpha() and code.isascii()):
            raise ValueError(
                f"country= must be 2-letter ISO2 code(s) (e.g. 'DE', 'NL'); got "
                f"{raw!r}. HANZE keys events by ISO2 country code."
            )
        codes.add(code)
    return codes


def _normalize_region(region: str | list[str] | None) -> set[str]:
    """Validate and upper-case the requested NUTS-3 region codes.

    Args:
        region: One NUTS-3 code, a list of them, or `None` to keep every region.

    Returns:
        set[str]: The upper-cased codes; empty when `None`.

    Raises:
        ValueError: If a value is not a 5-character NUTS-3 code (a 2-letter
            country prefix plus three alphanumerics). Like `country=`, a
            malformed code would otherwise filter every row away and look like an
            empty result rather than a mistake.
    """
    codes: set[str] = set()
    for raw in _as_list(region):
        code = raw.strip().upper()
        well_formed = (
            len(code) == 5 and code.isascii() and code.isalnum() and code[:2].isalpha()
        )
        if not well_formed:
            raise ValueError(
                f"region= must be 5-character NUTS-3 code(s) (e.g. 'DE300', "
                f"'NL414'); got {raw!r}. A malformed code matches nothing and "
                "looks like an empty result."
            )
        codes.add(code)
    return codes


class HANZE(AbstractDataSource):
    """HANZE historical-flood-impacts backend (per-instance output kind).

    Downloads the HANZE events / impacts table from its pinned Zenodo release,
    filters it by country / region / flood type / date window, and returns a
    :class:`pandas.DataFrame`. With `with_geometry=True` it instead returns a
    :class:`~pyramids.feature.collection.FeatureCollection` of the affected
    NUTS-3 regions.

    The record is public (CC-BY-4.0); no credentials are needed.

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` — `"tabular"` by
            default, `"vector"` when `with_geometry=True`. The facade reads it
            to know the return shape and rejects `aggregate=` for both.
        REQUIRES_TIME_WINDOW: `False` — a request without a window returns every
            year the record covers.

    Examples:
        - Pull DE + NL flood events, or the affected-region geometry, through the
          facade (both fetch from Zenodo, so this is illustrative, not a
          doctest):

            ```python
            from earthlens.core import EarthLens

            events = EarthLens(
                "hanze", start="1950", end="2020", country=["DE", "NL"]
            ).download()  # a pandas.DataFrame of events + impacts

            regions = EarthLens(
                "hanze", start="1990", end="2020", country="DE", with_geometry=True
            ).download()  # a FeatureCollection of the affected NUTS-3 regions
            ```
    """

    OUTPUT_KIND: OutputKind = "tabular"

    REQUIRES_TIME_WINDOW = False

    AGGREGATE_REFUSAL_REASON = (
        "HANZE serves observed flood event / impact records (and, with "
        "with_geometry, their NUTS-3 region polygons), not gridded rasters, so "
        "there is no meaningful gridded reduction. Call download() without "
        "aggregate= and post-process the returned DataFrame / FeatureCollection "
        "directly"
    )

    #: Whether the transport should draw a progress bar, set from
    #: `download(progress_bar=...)` so the flag reaches the fetch.
    _progress: bool = True

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        country: str | list[str] | None = None,
        region: str | list[str] | None = None,
        flood_type: str | list[str] | None = None,
        with_geometry: bool = False,
        timeout: float = 120.0,
    ):
        """Initialise a HANZE backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`. Only
                its year is significant — HANZE indexes events by year. `None`
                means "from the beginning of the record".
            end: Inclusive end of the optional window; `None` means "to the end
                of the record".
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in degrees. A
                non-global box selects the affected regions (and, on the tabular
                path, the events touching them) that intersect it, which loads
                the region geometry.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in degrees.
            temporal_resolution: HANZE issues one query over the whole window, so
                this is the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the cached source files and the written
                table / vector file. Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            country: One ISO2 country code or a list of them (`"DE"`,
                `["DE", "NL"]`). `None` keeps every country.
            region: One NUTS-3 code or a list of them (`"DE300"`), matched
                against each event's affected-region list. `None` keeps every
                region.
            flood_type: One flood type or a list of them — any of `"River"`,
                `"Flash"`, `"Coastal"`, `"River/Coastal"`. `None` keeps every
                type.
            with_geometry: When `True`, additionally download the NUTS-3 region
                shapefile and return a `FeatureCollection` of the affected
                regions instead of the events `DataFrame` (sets
                `OUTPUT_KIND="vector"` for this instance).
            timeout: Per-request timeout in seconds for the Zenodo downloads.

        Raises:
            ValueError: If a `country` value is not a 2-letter ISO2 code, a
                `region` value is not a 5-character NUTS-3 code, or a
                `flood_type` value is not a registered HANZE flood type.
        """
        self._catalog = Catalog()
        # Resolve the always-loaded record / geometry blocks into non-optional
        # attributes once, so the rest of the backend (and the type checker) can
        # read them without re-guarding the catalog's `... | None` fields.
        if self._catalog.record is None or self._catalog.geometry is None:
            raise ValueError(
                "the HANZE catalog failed to load its 'record:'/'geometry:' "
                "block; the bundled hanze_data_catalog.yaml is malformed."
            )
        self._record = self._catalog.record
        self._geo = self._catalog.geometry
        self._country = _normalize_country(country)
        self._region = _normalize_region(region)
        # Validate flood types against the catalog (did-you-mean hint on a typo).
        self._flood_types = [
            self._resolve_flood_type(name) for name in _as_list(flood_type)
        ]
        self._with_geometry = with_geometry
        self._timeout = timeout
        self._http: HttpClient | None = None
        self._regions_fc: FeatureCollection | None = None

        self.OUTPUT_KIND = "vector" if with_geometry else "tabular"

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            # HANZE is facet-only: it is a single product selected by
            # country=/region=/flood_type=, so it declares no `variables`
            # parameter (the facade neither requires nor forwards one). The base
            # class still wants the argument, so an empty list is passed here.
            variables=[],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    def _resolve_flood_type(self, name: str) -> str:
        """Resolve one requested flood type against the catalog vocabulary.

        Args:
            name: A flood-type string in any casing (`"river"`).

        Returns:
            str: The canonical HANZE flood type (`"River"`).

        Raises:
            ValueError: If `name` is not a registered flood type.
        """
        # Case-insensitive match against the catalog vocabulary, so "river"
        # resolves to "River" rather than failing the did-you-mean.
        wanted = name.strip().lower()
        for canonical in self._catalog.flood_types():
            if canonical.lower() == wanted:
                return canonical
        # Fall through to the catalog's did-you-mean error.
        self._catalog.get_flood_type(name.strip())
        raise AssertionError("unreachable")  # pragma: no cover

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Capture the requested bounds as a :class:`SpatialExtent`.

        Args:
            lat_lim: `[min_lat, max_lat]`.
            lon_lim: `[min_lon, max_lon]`.

        Returns:
            SpatialExtent: The requested extent.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the optional `[start, end]` window into a :class:`TemporalExtent`.

        HANZE covers a whole record and is indexed by event year, so `None`
        bounds are legal and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string bound; a non-matching
                string falls back to an ISO-8601 parse.

        Returns:
            TemporalExtent: Frozen model with the parsed (or `None`) endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        dates = (
            pd.DatetimeIndex([start_dt, end_dt])
            if start_dt is not None and end_dt is not None
            else pd.DatetimeIndex([])
        )
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=dates,
        )

    @property
    def _year_range(self) -> tuple[int | None, int | None]:
        """Return the requested window as inclusive year bounds.

        Returns:
            tuple[int | None, int | None]: `(first_year, last_year)`, either
                `None` when that end of the window was not given.
        """
        start = self.time.start_date
        end = self.time.end_date
        return (
            start.year if start is not None else None,
            end.year if end is not None else None,
        )

    @property
    def _bbox(self) -> tuple[float, float, float, float] | None:
        """Return the request bbox, or `None` for a Europe-wide request.

        Returns:
            tuple[float, float, float, float] | None:
                `(min_lon, min_lat, max_lon, max_lat)`, or `None` when the
                request covers the whole globe (so no region is dropped).
        """
        space = self.space
        whole_globe = (
            space.latitude_min <= _GLOBAL_LAT[0]
            and space.latitude_max >= _GLOBAL_LAT[1]
            and space.longitude_min <= _GLOBAL_LON[0]
            and space.longitude_max >= _GLOBAL_LON[1]
        )
        if whole_globe:
            return None
        return (
            space.longitude_min,
            space.latitude_min,
            space.longitude_max,
            space.latitude_max,
        )

    def _client(self) -> HttpClient:
        """Return this instance's pooled client, building it on first use.

        Zenodo is a single origin; a dropped connection there is a normal event,
        so connection and timeout errors are retried too (matching `emdat`).

        Returns:
            HttpClient: The same instance on every later call.
        """
        if self._http is None:
            self._http = HttpClient(
                timeout=self._timeout,
                retry_on_exceptions=(requests.ConnectionError, requests.Timeout),
            )
        return self._http

    def _download_file(
        self, key: str, *, expect_magic: bytes | tuple[bytes, ...] | None = None
    ) -> Path:
        """Download one catalog file into `root_dir`, reusing a cached copy.

        Args:
            key: A logical file key (`"events"`, `"regions"`, or `"region_names"`).
            expect_magic: Optional leading-byte guard (one prefix or a tuple of
                acceptable prefixes) rejecting an error page served with a 200
                status.

        Returns:
            Path: The local file in `root_dir`.
        """
        record = self._record.record
        entry = self._catalog.file(key)
        local = self.root_dir / entry.name
        if not local.exists():
            logger.info(f"HANZE: downloading {entry.name} (record {record}).")
            self._client().download(
                entry.content_url(record),
                local,
                expect_magic=expect_magic,
                progress=self._progress,
            )
        return local

    def _load_events(self) -> pd.DataFrame:
        """Download and parse the HANZE events / impacts CSV.

        The download is guarded by the events header's leading bytes, so an HTML
        error page served with a `200` status (a proxy / CDN hiccup) is rejected
        at the download site rather than cached under the CSV name and failing
        confusingly at `read_csv` on every later call — the same guard the region
        zip (`PK`) and the sibling `emdat` xlsx (`PK`) use. The live file is
        published with a UTF-8 BOM, so both the BOM-prefixed and the plain header
        are accepted; `utf-8-sig` then strips the BOM so the `ID` column name is
        clean.

        Returns:
            pandas.DataFrame: The full events table, HANZE's documented headers.
        """
        local = self._download_file("events", expect_magic=(b"\xef\xbb\xbfID,", b"ID,"))
        return pd.read_csv(local, encoding="utf-8-sig")

    def _load_regions(self) -> FeatureCollection:
        """Download, extract and read the NUTS-3 region shapefile (cached).

        Returns:
            FeatureCollection: The region polygons in the shapefile's stored CRS
                (`EPSG:3035`).
        """
        if self._regions_fc is not None:
            return self._regions_fc
        from pyramids.feature.collection import FeatureCollection

        archive = self._download_file("regions", expect_magic=b"PK")
        stem = self._geo.member_stem
        members = extract_members(
            archive,
            self.root_dir / "hanze_regions",
            include=(".shp", ".shx", ".dbf", ".prj", ".cpg"),
        )
        shp = next(
            (m for m in members if m.stem == stem and m.suffix.lower() == ".shp"),
            None,
        )
        if shp is None:
            raise ValueError(
                f"the HANZE region archive {archive.name} has no "
                f"{stem}.shp member (found {[m.name for m in members]})."
            )
        self._regions_fc = FeatureCollection.read_file(str(shp))
        return self._regions_fc

    def _bbox_region_codes(self) -> set[str] | None:
        """Return the NUTS-3 codes whose region intersects the request bbox.

        Loads the region geometry (in `EPSG:3035`), reprojects to WGS84, and
        selects the polygons intersecting the bbox. `None` when the request is
        Europe-wide (no bbox restriction).

        Returns:
            set[str] | None: The in-bbox NUTS-3 codes, or `None` for a whole-globe
                request.
        """
        bbox = self._bbox
        if bbox is None:
            return None
        regions = self._load_regions().to_crs(geometry_module.OUTPUT_CRS)
        join_field = self._geo.join_field
        min_lon, min_lat, max_lon, max_lat = bbox
        within = regions.cx[min_lon:max_lon, min_lat:max_lat]
        # Upper-cased here (like `self._region`) so `_row_matches_codes` compares
        # already-normalised sets rather than re-casing them per event row.
        return set(within[join_field].astype(str).str.upper())

    def _filter_events(self, events: pd.DataFrame) -> pd.DataFrame:
        """Apply the request's country / region / type / date / bbox filters.

        Args:
            events: The full events table.

        Returns:
            pandas.DataFrame: The matching rows, index reset.
        """
        columns = self._catalog.columns
        mask = pd.Series(True, index=events.index)

        if self._country:
            mask &= (
                events[columns["country_code"]]
                .astype(str)
                .str.upper()
                .isin(self._country)
            )
        if self._flood_types:
            mask &= events[columns["type"]].isin(self._flood_types)

        first_year, last_year = self._year_range
        if first_year is not None:
            mask &= events[columns["year"]] >= first_year
        if last_year is not None:
            mask &= events[columns["year"]] <= last_year

        # An explicit `region=` restriction only counts when non-empty; the
        # bbox-derived set counts whenever a bbox is set, even if it resolves to
        # no regions (a bbox over open water legitimately drops every event).
        code_filters: list[set[str]] = []
        if self._region:
            code_filters.append(self._region)
        bbox_codes = self._bbox_region_codes()
        if bbox_codes is not None:
            code_filters.append(bbox_codes)
        if code_filters:
            mask &= events[columns["regions_nuts3"]].apply(
                lambda cell: self._row_matches_codes(cell, code_filters)
            )

        return events[mask].reset_index(drop=True)

    @staticmethod
    def _row_matches_codes(cell: object, code_filters: list[set[str]]) -> bool:
        """Whether an event's affected-region list satisfies every code filter.

        Args:
            cell: One `Regions affected (NUTS 3)` cell.
            code_filters: One set per active restriction (explicit `region=`, and
                the bbox-derived codes); the event must intersect **each**.

        Returns:
            bool: `True` when the event's codes intersect every filter set.
        """
        # The filter sets are already upper-cased at construction (`self._region`)
        # and in `_bbox_region_codes`, so only the row's codes need normalising.
        row_codes = {code.upper() for code in geometry_module.split_nuts3(cell)}
        return all(bool(row_codes & codes) for codes in code_filters)

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product to fetch (the HANZE events table).

        Returns:
            list[RemoteProduct]: A single product carrying the record id.
        """
        return [
            RemoteProduct(
                id="hanze:events",
                metadata={"record": self._record.record},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Download, filter, and shape the one product to the instance's kind.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[Any]: One element — a filtered :class:`pandas.DataFrame`
                (tabular), or a
                :class:`~pyramids.feature.collection.FeatureCollection` of the
                affected regions (vector).
        """
        events = self._filter_events(self._load_events())
        if self._with_geometry:
            return [self._build_region_collection(events)]
        return [events]

    def _build_region_collection(self, events: pd.DataFrame) -> FeatureCollection:
        """Join the filtered events to their affected NUTS-3 region polygons.

        When the request carries a bbox, the joined regions are additionally
        restricted to it by bounding-box intersection (`GeoDataFrame.cx`): an
        event that touched an in-bbox region also lists regions outside the box,
        and returning those would put polygons well outside a spatial query on
        the map. A region whose extent intersects the box is kept **whole** (it
        is selected, not geometrically trimmed), so the vector answer stays
        "affected regions within the box", matching the tabular bbox path.

        Args:
            events: The filtered events table.

        Returns:
            FeatureCollection: One polygon per affected region (restricted to the
                regions intersecting the bbox when one is set), CRS `EPSG:4326`.
        """
        regions = self._load_regions()
        geometry = self._geo
        collection = geometry_module.join_events_to_regions(
            events,
            regions,
            regions_column=self._catalog.columns["regions_nuts3"],
            join_field=geometry.join_field,
            name_field=geometry.name_field,
        )
        bbox = self._bbox
        if bbox is None or not len(collection):
            return collection
        from pyramids.feature.collection import FeatureCollection

        min_lon, min_lat, max_lon, max_lat = bbox
        within = collection.cx[min_lon:max_lon, min_lat:max_lat]
        return FeatureCollection(within.reset_index(drop=True))

    def _api(self) -> list[Any]:
        """Compose :meth:`_search` and :meth:`_fetch`.

        Returns:
            list[Any]: The fetched result (one element).
        """
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True) -> pd.DataFrame | FeatureCollection:
        """Fetch HANZE and return the per-instance shape.

        Runs the download + filter, writes the result to `path` (a CSV for the
        tabular default, a GeoPackage for `with_geometry`), and returns it.

        Args:
            progress_bar: Whether to draw a download progress bar. Passed through
                to the transport, so `False` really does silence it.

        Returns:
            A :class:`pandas.DataFrame` of events + impacts (the default), or a
            :class:`~pyramids.feature.collection.FeatureCollection` of the
            affected NUTS-3 regions (`with_geometry=True`). Both are also written
            under `root_dir`.

        Raises:
            requests.HTTPError: If a Zenodo download returns a non-2xx status.
            ValueError: If a download's body fails its content guard (an HTML
                error page served with a 200 status), or `with_geometry=True` and
                the region archive has no `<member_stem>.shp` member.
        """
        self._progress = progress_bar
        results = self._api()
        # `_search` always yields one product, so `_fetch` returns a single
        # element (a 0-row DataFrame / empty FC is still one element); the
        # `_empty_result()` fallback is a defensive guard for a future `_search`
        # that could return nothing, not a path this backend reaches today.
        result = results[0] if results else self._empty_result()
        self._log_citation()

        if self.OUTPUT_KIND == "vector":
            out_path = self.root_dir / (self._result_stem("hanze_regions") + ".gpkg")
            # Written unconditionally — an empty result still writes a schema-only
            # GeoPackage, so the vector path matches the tabular one (which always
            # writes a header-only CSV) and a caller globbing `path` finds a file.
            result.to_file(str(out_path), driver="GPKG")
            logger.info(
                f"HANZE: {len(result)} affected region(s) written to {out_path}."
            )
            return result

        out_path = self.root_dir / (self._result_stem("hanze_events") + ".csv")
        result.to_csv(out_path, index=False)
        logger.info(f"HANZE: {len(result)} event(s) written to {out_path}.")
        return result

    def _empty_result(self) -> pd.DataFrame | FeatureCollection:
        """Return the empty result matching the instance's output kind."""
        if self._with_geometry:
            return geometry_module.empty_region_fc()
        return pd.DataFrame()

    def _result_stem(self, base: str) -> str:
        """Compose an output file stem that encodes the request's filters.

        A plain `base` for an unfiltered request; otherwise `base-<digest>` so
        two differently-filtered queries into one `path=` do not overwrite each
        other. The digest is order-insensitive in the multi-value filters.

        Args:
            base: The stem prefix (`"hanze_events"` / `"hanze_regions"`).

        Returns:
            str: `base`, or `base-<8-hex-digest>` when any filter is active.
        """
        first_year, last_year = self._year_range
        applied = (
            bool(self._country),
            bool(self._region),
            bool(self._flood_types),
            first_year is not None,
            last_year is not None,
            self._bbox is not None,
        )
        if not any(applied):
            return base
        request = (
            tuple(sorted(self._country)),
            tuple(sorted(self._region)),
            tuple(sorted(self._flood_types)),
            self._year_range,
            self._bbox,
        )
        digest = hashlib.sha1(
            repr(request).encode(), usedforsecurity=False
        ).hexdigest()[:8]
        return f"{base}-{digest}"

    def _log_citation(self) -> None:
        """Log the CC-BY attribution once (info, not a warning)."""
        record = self._record
        if record.attribution:
            logger.info(f"HANZE source citation: {record.attribution}")
