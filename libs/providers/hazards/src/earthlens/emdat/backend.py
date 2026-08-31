"""Backend that fetches EM-DAT disaster events and their geocoded locations.

`EMDAT(AbstractDataSource)` serves one request from whichever of two routes the
requested dataset names, and returns the shape that dataset declares. A request
is one dataset id (`variables=["emdat:events"]`) plus optional hazard, country,
date-window and bbox filters.

Three design points carry this backend:

* **Per-instance `OUTPUT_KIND`.** The resolved dataset's `output_kind` is copied
  onto `self.OUTPUT_KIND` in `__init__`: `emdat:events` is `tabular` and returns
  a :class:`pandas.DataFrame`, both `gdis:*` rows are `vector` and return a
  pyramids :class:`~pyramids.feature.collection.FeatureCollection`. The
  :class:`earthlens.earthlens.EarthLens` facade reads the instance attribute to
  know the return shape.
* **Conditional auth.** `emdat:events` comes from the UCLouvain Dataverse and
  is anonymous. The `gdis:*` sources moved into NASA Earthdata Cloud when their
  old SEDAC host went away, so they build an :class:`EmdatAuth` and need an
  Earthdata Login.
* **Size asymmetry.** `gdis:points` is a 1 MB CSV; `gdis:polygons` is a 2.2 GB
  GeoPackage. The polygons are never fetched implicitly — the request warns
  before starting that download.

These are event records and pre-geocoded locations, not gridded rasters, so
`aggregate=` is refused and nothing here imports a gridded-array library.
"""

from __future__ import annotations

import hashlib
import warnings
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import requests
from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    AbstractDataSource,
    HttpClient,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.biodiversity import LicenseWarning
from earthlens.emdat import _helpers
from earthlens.emdat.auth import EmdatAuth, EmdatCredentials
from earthlens.emdat.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: Global sentinel bounds — a request without an explicit bbox covers the world.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]

#: A download above this many megabytes gets an explicit warning before it
#: starts. Only `gdis:polygons` (2.2 GB) crosses it.
LARGE_DOWNLOAD_MB: float = 100.0


def _resolve_dataset_id(variables: list[str] | dict[str, Any] | None) -> str:
    """Return the single dataset id named by a `variables=` argument.

    Args:
        variables: The constructor's `variables=` argument.

    Returns:
        str: The one requested dataset id, with a repeat of the same id
            collapsed.

    Raises:
        TypeError: If `variables` is a mapping, which is another backend's
            shape.
        ValueError: If it does not name exactly one dataset.
    """
    if isinstance(variables, dict):
        raise TypeError(
            "EMDAT `variables` must be a one-element list naming the "
            "dataset id (e.g. ['emdat:events']), not a mapping."
        )
    ids = list(dict.fromkeys(variables)) if variables else []
    if len(ids) != 1:
        raise ValueError(
            "EMDAT needs exactly one dataset id in variables= (OUTPUT_KIND "
            f"is per instance); got {ids!r}. Available: "
            f"{Catalog().available()}."
        )
    return ids[0]


def _validated_iso3(country: str | None) -> str | None:
    """Return `country` unchanged once it looks like an ISO3 code.

    Args:
        country: The requested ISO3 code, or `None` to keep every country.

    Returns:
        str | None: The argument as given, so the caller's spelling is what
            reaches the filter.

    Raises:
        ValueError: If `country` is not three ASCII letters.
    """
    if country is None:
        return None
    # `isascii()` as well as `isalpha()`: the latter alone accepts letters
    # like 'Ð', which no ISO3 code contains.
    code = country.strip()
    if not (len(code) == 3 and code.isalpha() and code.isascii()):
        raise ValueError(
            f"country= must be a 3-letter ISO3 code (e.g. 'BGD'); got "
            f"{country!r}. An unrecognised code would otherwise filter every "
            "row away and look like an empty result."
        )
    return country


class EMDAT(AbstractDataSource):
    """EM-DAT disaster event / location backend (per-instance output kind).

    Resolves one dataset id to its catalog row, fetches it from that row's
    provider, and returns a :class:`pandas.DataFrame` (`tabular`) or a
    :class:`~pyramids.feature.collection.FeatureCollection` (`vector`) per the
    row's `output_kind`.

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the resolved
            dataset's `output_kind`. The facade reads it to know the return
            shape.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "EM-DAT serves disaster event records and pre-geocoded locations, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate="

    #: Both routes serve a whole archive, so a request without a window is
    #: legal and simply returns every year the dataset covers.
    REQUIRES_TIME_WINDOW = False

    #: Whether the transport should draw a progress bar. Set from
    #: `download(progress_bar=...)` so the flag actually reaches the fetch
    #: rather than being accepted and dropped.
    _progress: bool = True

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "annual",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        hazard: str | list[str] | None = None,
        country: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ):
        """Initialise an EM-DAT backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`.
                Only its year is significant — EM-DAT and GDIS are both indexed
                by event year. `None` means "from the beginning of the record".
            end: Inclusive end of the optional window; `None` means "to the end
                of the record".
            variables: A one-element list naming the dataset id
                (`["emdat:events"]`). Exactly one dataset is resolved per
                instance, because `OUTPUT_KIND` is per instance.
            lat_lim: `[min_lat, max_lat]` filter. Applied to the event
                coordinates for `emdat:events` / `gdis:points`, and pushed down
                as a spatial filter for `gdis:polygons`.
            lon_lim: `[min_lon, max_lon]` filter.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory; fetched source files and any written table
                land here.
            fmt: `strptime` format for `start` / `end`.
            hazard: One disaster type or a list of them (`"flood"`,
                `["flood", "storm"]`). Matched case- and whitespace
                -insensitively. `None` keeps every type.
            country: ISO3 country code to keep (`"BGD"`), matched
                case-insensitively. `None` keeps every country. A value that
                is not three letters is rejected rather than silently
                matching nothing.
            username: Earthdata Login username for the `gdis:*` sources. Falls
                back to `EARTHDATA_USERNAME`.
            password: Earthdata Login password. Falls back to
                `EARTHDATA_PASSWORD`.
            token: An Earthdata Login bearer token, preferred over a username
                and password. Falls back to `EARTHDATA_TOKEN`.

        Raises:
            TypeError: If `variables` is a mapping (pass a list of one id).
            ValueError: If `variables` is not exactly one dataset id, if a
                requested `hazard` is not a disaster type of the resolved
                dataset (the two sources have different vocabularies), or if
                `country` is not a 3-letter ISO3 code.
        """
        dataset_id = _resolve_dataset_id(variables)
        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(dataset_id)
        self._country = _validated_iso3(country)

        requested = [hazard] if isinstance(hazard, str) else list(hazard or [])
        self._hazards = [
            self._catalog.normalize_hazard(name, self._dataset) for name in requested
        ]

        # The events route is anonymous; only GDIS rides Earthdata Login.
        self._auth: EmdatAuth | None = None
        if self._dataset.provider == "earthdata":
            self._auth = EmdatAuth(
                EmdatCredentials(
                    username=username,
                    password=SecretStr(password) if password is not None else None,
                    token=SecretStr(token) if token is not None else None,
                )
            )

        # The per-instance output shape comes from the resolved dataset.
        self.OUTPUT_KIND = self._dataset.output_kind

        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=[dataset_id],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

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

        Both routes are indexed by event year and cover a whole archive, so
        `None` bounds are legal and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string bound; a
                non-matching string falls back to an ISO-8601 parse.

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
            tuple[int | None, int | None]: `(first_year, last_year)`, either of
                which is `None` when that end of the window was not given.
        """
        start = self.time.start_date
        end = self.time.end_date
        return (
            start.year if start is not None else None,
            end.year if end is not None else None,
        )

    @property
    def _bbox(self) -> tuple[float, float, float, float] | None:
        """Return the request bbox, or `None` for a world-wide request.

        A whole-globe request yields `None` rather than global bounds, so it
        never drops rows that merely lack coordinates.

        Returns:
            tuple[float, float, float, float] | None:
                `(min_lon, min_lat, max_lon, max_lat)`, or `None` when the
                request covers the whole globe.
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

        `HttpClient` retries retryable *statuses* out of the box but not
        transport failures, and the UCLouvain Dataverse is a single university
        host with no CDN in front of it — a dropped connection there is a
        normal event, not a signal to give up. Connection and timeout errors
        are therefore retried too, matching the `climate_indices` and
        `worldpop` backends, which fetch from comparable single-host origins.

        Returns:
            HttpClient: The same instance on every later call, so the pooled
                connection is reused rather than rebuilt per request.
        """
        if self._http is None:
            self._http = HttpClient(
                retry_on_exceptions=(requests.ConnectionError, requests.Timeout),
            )
        return self._http

    def authenticate(self) -> EMDAT:
        """Configure Earthdata Login when the resolved dataset needs it.

        Returns:
            EMDAT: This instance, so the call can be chained.

        Raises:
            AuthenticationError: When the `gdis:*` credentials do not resolve.
        """
        if self._auth is not None:
            self._auth.configure()
        return self

    def _search(self) -> list[RemoteProduct]:
        """Pin the one product to fetch (the resolved dataset).

        Returns:
            list[RemoteProduct]: A single product carrying the dataset id.
        """
        return [
            RemoteProduct(
                id=self._dataset.id,
                metadata={"provider": self._dataset.provider},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Fetch each product from its provider and parse it.

        Args:
            products: The list from :meth:`_search` (one product).

        Returns:
            list[Any]: One element per product — a :class:`pandas.DataFrame` for a
                `tabular` dataset, a
                :class:`~pyramids.feature.collection.FeatureCollection` for a
                `vector` one.
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct) -> Any:
        """Fetch and parse one product per its provider.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            A `DataFrame` (tabular) or `FeatureCollection` (vector).
        """
        if product.metadata.get("provider") == "dataverse":
            return self._fetch_events()
        return self._fetch_gdis()

    def _api(self) -> list[Any]:
        """Compose :meth:`_search` and :meth:`_fetch`.

        Returns:
            list[Any]: The fetched results.
        """
        return self._api_via_search_fetch()

    def _fetch_events(self) -> pd.DataFrame:
        """Fetch and filter the EM-DAT archive from the UCLouvain Dataverse.

        Resolves the archive file by name pattern against the dataset's latest
        published version (its name carries a release-date prefix that changes
        every version), downloads it into `root_dir`, reads the table and
        applies the request's filters.

        Returns:
            pandas.DataFrame: The matching event rows.

        Raises:
            ValueError: If the archive file cannot be resolved.
            requests.HTTPError: If the Dataverse API returns a non-2xx status.
        """
        dataset = self._dataset
        http = self._client()
        base = cast("str", dataset.dataverse_base)
        listing = _helpers.dataverse_file_listing(http, base, cast("str", dataset.doi))
        file_id, filename = _helpers.pick_dataverse_file(listing, dataset)

        # Delivered into the caller's own output directory rather than a hidden
        # cache, and reused from there on a later call. The EM-DAT terms forbid
        # redistributing the database, so every copy earthlens creates lives in
        # the caller's own directory and none is published anywhere.
        #
        # `filename` comes from a remote listing, so only its basename is used —
        # a name containing path separators would otherwise let the server pick
        # where on the caller's disk the download lands.
        local = self.root_dir / Path(filename).name
        if not local.exists():
            logger.info(f"EMDAT: downloading {filename} from {base}.")
            # `expect_magic` rejects an HTML error page served with a 200, which
            # would otherwise be cached under an .xlsx name and fail confusingly
            # at parse time on every later call. xlsx is a zip container.
            http.download(
                _helpers.dataverse_download_url(base, file_id),
                local,
                expect_magic=b"PK",
                progress=self._progress,
            )

        frame = pd.read_excel(local, sheet_name=cast("str", dataset.sheet))
        rows = _helpers.filter_frame(
            frame,
            dataset,
            hazards=self._hazards,
            country=self._country,
            year_range=self._year_range,
            bbox=self._bbox,
        )
        logger.info(
            f"EMDAT {dataset.id}: {len(rows)} of {len(frame)} event(s) matched."
        )
        return rows

    def _fetch_gdis(self) -> FeatureCollection:
        """Fetch and filter a GDIS distribution from NASA Earthdata Cloud.

        Returns:
            FeatureCollection: The matching disaster locations — points for
                `gdis:points`, admin-unit polygons for `gdis:polygons`.
        """
        granule = self._download_granule()
        member = _helpers.extract_member(
            granule, cast("str", self._dataset.member), self.root_dir
        )
        if self._dataset.format == "csv":
            return self._read_gdis_csv(member)
        return self._read_gdis_gpkg(member)

    def _warn_if_large(self) -> None:
        """Warn before starting a download big enough to be a surprise."""
        size = self._dataset.download_mb
        if size is not None and size >= LARGE_DOWNLOAD_MB:
            logger.warning(
                f"EMDAT {self._dataset.id}: this granule is about "
                f"{size / 1024:.1f} GB compressed and expands to several GB on "
                "disk. Use variables=['gdis:points'] (about 1 MB) unless real "
                "polygon footprints are needed."
            )

    def _download_granule(self) -> Path:
        """Authenticate, locate the granule on CMR, and download it.

        Anything a previous run left in `root_dir` is reused, so a repeated
        request costs nothing.

        Returns:
            Path: The granule archive in `root_dir`, or the member a previous
                run already unpacked from it.

        Raises:
            ImportError: If `earthaccess` is not installed.
            ValueError: If the named granule is not in the CMR collection.
        """
        dataset = self._dataset
        # The already-unpacked member is checked first: when it is present the
        # granule is not needed at all, so a truncated archive next to it is
        # nothing to act on. earthaccess also does not guarantee the catalogued
        # file name, which is the other reason not to lead with the archive.
        extracted = self.root_dir / Path(cast("str", dataset.member)).name
        if extracted.exists():
            return extracted

        local = self.root_dir / Path(cast("str", dataset.granule)).name
        if local.exists():
            # earthaccess offers no atomicity guarantee, unlike HttpClient's
            # atomic download, so an interrupted 2.2 GB fetch can leave a
            # truncated zip here. Reusing it would fail forever with a bare
            # BadZipFile naming neither the file nor the remedy.
            if zipfile.is_zipfile(local):
                return local
            logger.warning(
                f"EMDAT {dataset.id}: {local.name} is not a readable zip "
                "(a previous download was probably interrupted); re-fetching it."
            )
            local.unlink()

        self._warn_if_large()
        self.authenticate()

        import earthaccess

        granule = cast("str", dataset.granule)
        results = earthaccess.search_data(short_name=dataset.short_name, count=-1)
        wanted = [item for item in results if granule in str(item.data_links())]
        if not wanted:
            available = [str(item.data_links()) for item in results]
            raise ValueError(
                f"granule {dataset.granule!r} is not in CMR collection "
                f"{dataset.short_name!r}. Available: {available}. The collection "
                "may have been re-issued; update `granule:` in the EM-DAT catalog."
            )
        logger.info(f"EMDAT {dataset.id}: downloading {dataset.granule}.")
        fetched = earthaccess.download(
            wanted[:1], local_path=str(self.root_dir), show_progress=self._progress
        )
        # Trust what the downloader reports rather than assuming it used the
        # catalogued file name, and fail loudly here instead of at unzip time.
        paths = [Path(item) for item in (fetched or []) if item]
        if not paths or not paths[0].is_file():
            raise OSError(
                f"earthaccess reported no downloaded file for "
                f"{dataset.granule!r} (returned {fetched!r}). The granule may "
                "be unavailable, or the Earthdata account may not have accepted "
                "the SEDAC data-use agreement — see "
                "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas."
            )
        return paths[0]

    def _read_gdis_csv(self, path: Path) -> FeatureCollection:
        """Read the GDIS centroid CSV and build a point collection.

        Args:
            path: The extracted CSV.

        Returns:
            FeatureCollection: Matching locations as points.
        """
        dataset = self._dataset
        frame = pd.read_csv(path, encoding=dataset.encoding, low_memory=False)
        rows = _helpers.filter_frame(
            frame,
            dataset,
            hazards=self._hazards,
            country=self._country,
            year_range=self._year_range,
            bbox=self._bbox,
        )
        logger.info(
            f"EMDAT {dataset.id}: {len(rows)} of {len(frame)} location(s) matched."
        )
        return _helpers.points_to_feature_collection(rows, dataset)

    def _read_gdis_gpkg(self, path: Path) -> FeatureCollection:
        """Read the GDIS GeoPackage, pushing the cheap filters into the driver.

        The hazard and bbox filters go down to the driver, which is what keeps
        a 6.3 GB file readable. The year filter stays in memory: it has to be
        derived from the `disasterno` prefix, and the SQL string functions that
        would express it are not portable across drivers and dialects.

        Args:
            path: The extracted GeoPackage.

        Returns:
            FeatureCollection: Matching disaster footprints.
        """
        from pyramids.feature.collection import FeatureCollection

        dataset = self._dataset
        where = _helpers.combine_filters(
            _helpers.hazard_filter_sql(cast("str", dataset.type_column), self._hazards)
            if self._hazards
            else None,
            _helpers.country_filter_sql(cast("str", dataset.iso_column), self._country)
            if self._country and dataset.iso_column
            else None,
        )
        collection = FeatureCollection.read_file(
            path, layer=dataset.layer, where=where, bbox=self._bbox
        )

        first, last = self._year_range
        if first is None and last is None:
            logger.info(f"EMDAT {dataset.id}: {len(collection)} footprint(s) matched.")
            return collection

        years = _helpers.event_years(collection, dataset)
        keep = pd.Series(True, index=collection.index)
        if first is not None:
            keep &= (years >= first).fillna(False)
        if last is not None:
            keep &= (years <= last).fillna(False)
        # `reset_index` so this route matches the other two, which filter
        # through `filter_frame` and hand back a contiguous index.
        filtered = collection[keep].reset_index(drop=True)
        logger.info(f"EMDAT {dataset.id}: {len(filtered)} footprint(s) matched.")
        return FeatureCollection(filtered)

    def download(self, progress_bar: bool = True) -> pd.DataFrame | FeatureCollection:
        """Fetch the dataset and return its per-instance shape.

        Args:
            progress_bar: Whether to draw a download progress bar. Passed
                through to the transport, so `False` really does silence it.

        Returns:
            A :class:`pandas.DataFrame` for `emdat:events` (also written to
            `root_dir`), or an in-memory
            :class:`~pyramids.feature.collection.FeatureCollection` for a
            `gdis:*` dataset.

        Raises:
            AuthenticationError: When a `gdis:*` request has no usable
                Earthdata Login credentials.
            requests.HTTPError: If an upstream source returns a non-2xx status.
        """
        self._progress = progress_bar
        # Warn before the fetch: a user who is not eligible should learn that
        # before an 8 MB download, not after it.
        self._warn_license()
        results = self._api()
        result = results[0]
        self._log_citation()
        if self.OUTPUT_KIND == "vector":
            logger.info(
                f"EMDAT {self._dataset.id}: returned a FeatureCollection "
                f"({len(result)} feature(s))."
            )
            return result
        out_path = self.root_dir / self._result_filename()
        result.to_csv(out_path, index=False)
        logger.info(
            f"EMDAT {self._dataset.id}: {len(result)} row(s) written to {out_path}."
        )
        return result

    def _result_filename(self) -> str:
        """Return the output CSV name, which encodes the request's filters.

        The workbook cache is shared across requests by design, so two
        differently-filtered queries into one `path=` are a normal pattern. A
        name carrying only the dataset id would let the second silently
        overwrite the first, and the docs advertise that file by name. An
        unfiltered request keeps the plain name; any filter adds a short digest
        of the whole request. The digest is order-insensitive in `hazard=`, so
        the same set of hazards always resolves to the same file.

        Returns:
            str: `emdat_events.csv`, or `emdat_events-<digest>.csv` when the
                request is filtered.
        """
        stem = self._dataset.id.replace(":", "_")
        first_year, last_year = self._year_range
        # Each filter listed separately: a `(None, None)` year range is a
        # non-empty tuple and therefore truthy, so testing the tuple itself
        # would make every request look filtered.
        applied = (
            bool(self._hazards),
            self._country is not None,
            first_year is not None,
            last_year is not None,
            self._bbox is not None,
        )
        if not any(applied):
            return f"{stem}.csv"
        # Sorted: hazard=['flood', 'storm'] and ['storm', 'flood'] are the same
        # request and must resolve to the same file.
        request = (
            tuple(sorted(self._hazards)),
            self._country,
            self._year_range,
            self._bbox,
        )
        digest = hashlib.sha1(
            repr(request).encode(), usedforsecurity=False
        ).hexdigest()[:8]
        return f"{stem}-{digest}.csv"

    def _warn_license(self) -> None:
        """Emit a :class:`LicenseWarning` when the dataset's use is restricted.

        GDIS is CC-BY-4.0 and needs only attribution, so it warns about
        nothing. The EM-DAT archive is CC-BY-NC-ND under terms that also limit
        *who* may use it for free, which is the part a user is most likely to
        miss.

        The `stacklevel` targets a call through
        :class:`earthlens.earthlens.EarthLens`, which is what every doc
        example, both notebooks and the e2e tests use: `_warn_license` ->
        `download` -> the `_wrap_download` wrapper -> the facade -> the caller.
        A direct `EMDAT(...).download()` is one frame shorter, so the warning
        is attributed to earthlens rather than to that caller. No single value
        suits both paths, and the facade is the documented one.
        """
        dataset = self._dataset
        if not dataset.restricted_use:
            return
        warnings.warn(
            f"{dataset.id} is licensed {dataset.licence}: free use is limited to "
            "academic organisations, universities, non-profit research "
            "institutions, international public organisations and media, for "
            "research, teaching or information purposes. Any other use is "
            "commercial and needs a separate agreement with CRED/UCLouvain. The "
            "terms also forbid redistributing the database or building a "
            "derivative database from it, so treat this result as fetched for "
            f"you alone. See {dataset.terms_url}.",
            LicenseWarning,
            stacklevel=5,
        )

    def _log_citation(self) -> None:
        """Log the resolved dataset's source citation once (info, not a warning)."""
        if self._dataset.citation:
            logger.info(f"EMDAT source citation: {self._dataset.citation}")
