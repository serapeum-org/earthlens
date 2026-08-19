"""Backend that fetches administrative-boundary polygons from four public sources.

`AdminBoundaries(AbstractDataSource)` resolves a requested dataset
(`variables=["geoboundaries:adm1"]`) against the bundled catalog, routes to the
right source — geoBoundaries (per-country ADM0–ADM5), CGAZ (seamless global
ADM0/1/2), Natural Earth (global cultural admin layers), or US Census TIGER/Line
(states / counties / tracts / nation) — reads the boundary file through pyramids
`~pyramids.feature.collection.FeatureCollection.read_file`, normalises it to
EPSG:4326, and returns a `FeatureCollection` of polygons.

This is a `vector` backend: the result is a table of boundary polygons, not a
gridded array, so `OUTPUT_KIND = "vector"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument (there is
no meaningful gridded reduction of a boundary layer). `download()` returns the
in-memory `FeatureCollection` and, when a `path` is set, also writes it to one
vector file.

All four sources are public — there is **no auth module** and **no extra SDK**
(the only dependencies are core `requests` and `pyramids`). Administrative
boundaries are static, so `start` / `end` are accepted but ignored (there is no
temporal axis to iterate). A request supplies the dataset's selector as an
explicit keyword: `country=<ISO3>` for geoBoundaries, an optional `scale=` for
Natural Earth, an optional `year=` (and `state=` FIPS for tracts) for TIGER;
CGAZ is seamless and needs none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import pandas as pd
from loguru import logger
from pyramids.feature.collection import FeatureCollection

from earthlens.admin._helpers import (
    cgaz_url,
    empty_fc,
    geoboundaries_resolve,
    natural_earth_url,
    read_vector,
    tiger_url,
    vsicurl,
)
from earthlens.admin.catalog import Catalog, Dataset
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)

FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Whole-Earth bbox used as the sentinel extent — admin boundaries are not
#: bbox-sampled (`G6`), so the spatial window is informational only.
_WHOLE_EARTH_LAT = [-90.0, 90.0]
_WHOLE_EARTH_LON = [-180.0, 180.0]

#: The Natural Earth scales the catalog serves; an explicit `scale=` is checked
#: against this set so a typo fails clearly instead of as an opaque read error.
_NE_SCALES = ("10m", "50m", "110m")


def _coerce_state(state: str | int | None) -> str | None:
    """Normalise a US state FIPS selector to a zero-padded two-digit string.

    Args:
        state: A numeric FIPS code as a string or int (`6`, `"6"`, `"06"`), or
            `None` when the request needs no state selector.

    Returns:
        str | None: The zero-padded two-digit FIPS code, or `None`.

    Raises:
        ValueError: If `state` is not a numeric FIPS code (e.g. a state name or
            postal abbreviation), with a message naming the expected form.
    """
    if state is None:
        return None
    try:
        return f"{int(state):02d}"
    except (TypeError, ValueError):
        raise ValueError(
            f"state= must be a numeric US state FIPS code (e.g. '06' for "
            f"California), got {state!r}. State names / postal abbreviations "
            "are not accepted."
        ) from None


class AdminBoundaries(AbstractDataSource):
    """Administrative-boundary backend (vector polygon output, four public sources).

    Resolves a requested dataset against the bundled catalog, routes to the
    right source, reads the boundary file through pyramids, normalises it to
    EPSG:4326, and returns a `FeatureCollection` of polygons. None of the four
    sources needs credentials.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of boundary polygons,
            so the facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "administrative boundaries are vector polygons, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate= and post-process the returned FeatureCollection (a GeoDataFrame) directly"

    #: Administrative boundaries are a snapshot with no time axis, so a missing `start`
    #: / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        variables: list[str] | dict[str, list[str]],
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        start: str | None = None,
        end: str | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        country: str | None = None,
        scale: str | None = None,
        year: int | None = None,
        state: str | None = None,
        file_format: FileFormat = "gpkg",
        timeout: float = 60.0,
    ):
        """Initialise an admin-boundaries backend instance.

        Args:
            variables: The dataset id(s) to fetch — a list of catalog ids
                (`["geoboundaries:adm1"]`, `["tiger:county"]`). A mapping is
                tolerated (its keys are used as ids) so the facade's `dataset=`
                sugar works. For this backend `variables` names *datasets*, not
                data variables. At least one id is required.
            lat_lim: `[lat_min, lat_max]` — accepted for signature parity but
                not used to subset (admin boundaries are not bbox-sampled,
                `G6`). Defaults to whole-Earth.
            lon_lim: `[lon_min, lon_max]` — accepted and not used (see
                `lat_lim`). Defaults to whole-Earth.
            start: Accepted for signature parity and recorded, but ignored —
                administrative boundaries are static.
            end: Accepted and ignored (see `start`).
            temporal_resolution: Sentinel `"all"` — admin is not chunked in
                time.
            path: Output directory for the written vector file. Created by the
                parent class if absent; when empty, nothing is written and only
                the in-memory `FeatureCollection` is returned.
            fmt: `strptime` format for `start` / `end` (only used when supplied,
                for record-keeping).
            country: ISO-3166-1 alpha-3 country code for geoBoundaries datasets
                (`"KEN"`). Required by every `geoboundaries:*` dataset.
            scale: Natural Earth scale override (`"10m"` / `"50m"` / `"110m"`);
                `None` uses the dataset's `default_scale`.
            year: TIGER vintage year override; `None` uses the dataset's
                `default_year`.
            state: Two-digit state FIPS code for per-state TIGER datasets
                (`tiger:tract`), e.g. `"06"` for California. Required by every
                per-state dataset.
            file_format: Output vector format — `"gpkg"` (default) or
                `"geojson"`.
            timeout: Per-request timeout in seconds for the geoBoundaries
                metadata GET.

        Raises:
            ValueError: If `variables` is empty, `file_format` is unsupported,
                `scale` is not a Natural Earth scale (`10m` / `50m` / `110m`),
                `state` is not a numeric FIPS code, a dataset id is unknown, or
                a dataset's required selector (`country` / `state`) is missing.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        ids = list(variables.keys()) if isinstance(variables, dict) else list(variables)
        if not ids:
            raise ValueError(
                "admin `variables` is empty; supply at least one dataset id, "
                "e.g. variables=['geoboundaries:adm1']."
            )
        if scale is not None and scale not in _NE_SCALES:
            raise ValueError(
                f"scale= must be one of {_NE_SCALES} (Natural Earth scales), "
                f"got {scale!r}."
            )
        self._ids = ids
        self._country = country.upper() if country else None
        self._scale = scale
        self._year = year
        self._state = _coerce_state(state)
        self._file_format: FileFormat = file_format
        self._timeout = timeout
        self._catalog = Catalog()
        # Only write a file when the caller passed a real output directory; an
        # empty / unset path returns the in-memory collection only. Decided from
        # the raw argument, before the base class resolves it, so an omitted
        # path still means "do not write" even though it now resolves to the
        # configured output directory rather than the cwd.
        self._should_write = bool(path) and str(path) != "."
        super().__init__(
            start=cast("str", start),
            end=cast("str", end),
            variables=ids,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else list(_WHOLE_EARTH_LAT),
            lon_lim=lon_lim if lon_lim is not None else list(_WHOLE_EARTH_LON),
            fmt=fmt,
            path=path,
        )
        # Admin is static — pin the sentinel even when the facade forwards its
        # default cadence, so the attribute never misrepresents a cadence.
        self.temporal_resolution = "all"

    def _initialize(self) -> None:
        """Resolve every requested dataset and validate its required selectors.

        No network, no client: the four sources are public. Each id in
        `variables` is resolved against the catalog (raising with a
        did-you-mean hint on an unknown id) and its required selectors
        (`country` for geoBoundaries, `state` for per-state TIGER) are checked
        against the constructor kwargs.

        Returns:
            None: No per-instance client object.

        Raises:
            ValueError: If a dataset id is unknown or a required selector is
                missing.
        """
        self._datasets: list[Dataset] = [self._catalog.get(v) for v in self._ids]
        selector_values = {"country": self._country, "state": self._state}
        for dataset in self._datasets:
            for selector in dataset.required_selectors:
                if selector_values.get(selector) is None:
                    raise ValueError(
                        f"dataset {dataset.id!r} requires a {selector!r} "
                        f"selector; pass {selector}= to the request (e.g. "
                        f"{selector}='KEN')."
                    )
        return None

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Record the (ignored) date window in a `TemporalExtent`.

        Administrative boundaries are static, so there is no date loop:
        `start` / `end` are parsed only when supplied (for record-keeping) and
        otherwise left `None`. The resolution is the sentinel `"all"`.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Ignored beyond being recorded.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model; `start_date` / `end_date` are `None`
                when the corresponding argument was `None`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def _search(self) -> list[RemoteProduct]:
        """Plan one `RemoteProduct` per requested dataset (no network).

        Each product carries the resolved `Dataset` row on its metadata; the
        actual URL resolution (including the geoBoundaries metadata GET) and the
        read happen in `_fetch`.

        Returns:
            list[RemoteProduct]: One product per dataset id, in request order;
                each `id` is the dataset id and `metadata["dataset"]` is its
                `Dataset` row.
        """
        return [
            RemoteProduct(id=dataset.id, metadata={"dataset": dataset})
            for dataset in self._datasets
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Resolve and read each product's boundary layer into a FeatureCollection.

        Routes on the dataset's `provider`, builds the source URL (the
        geoBoundaries two-step resolve happens here), reads it through
        `read_vector` (pyramids, normalised to EPSG:4326), and logs each
        source's license once per provider (`G7`).

        Args:
            products: The products returned by `_search`.

        Returns:
            list[FeatureCollection]: One collection per product, in order.
        """
        logged_licenses: set[str] = set()
        # Lazy so a `limit=` stops the work: a product past the cap is never
        # resolved or read, rather than downloaded and then trimmed away.
        return self._take_limited(
            (self._read_product(product, logged_licenses) for product in products),
            limit=self._limit,
        )

    def _read_product(
        self, product: RemoteProduct, logged_licenses: set[str]
    ) -> FeatureCollection:
        """Resolve one product's URL and read its boundary layer.

        Args:
            product: One product from `_search`.
            logged_licenses: Providers already logged this call; mutated so
                each provider's license note is emitted once per download.

        Returns:
            FeatureCollection: The dataset's features, normalised to EPSG:4326.
        """
        dataset: Dataset = product.metadata["dataset"]
        url = self._resolve_url(dataset)
        logger.info(f"Fetching {dataset.id} from {dataset.provider} ({url})")
        if dataset.provider not in logged_licenses:
            logger.info(f"{dataset.provider} license: {dataset.license_note}")
            logged_licenses.add(dataset.provider)
        collection = read_vector(url)
        logger.info(f"{dataset.id}: read {len(collection)} feature(s)")
        return collection

    def _resolve_url(self, dataset: Dataset) -> str:
        """Build the `read_vector` source path for one dataset (routes by provider).

        Args:
            dataset: The resolved catalog row to fetch.

        Returns:
            str: A `FeatureCollection.read_file` target — a `/vsicurl/` GeoJSON
                / GeoPackage path or a `/vsizip//vsicurl/` zipped-shapefile
                path.

        Raises:
            ValueError: If the row carries an unrecognised `provider`.
        """
        if dataset.provider == "geoboundaries":
            gj_url = geoboundaries_resolve(
                cast("str", self._country),
                cast("str", dataset.adm_level),
                timeout=self._timeout,
            )
            return vsicurl(gj_url)
        if dataset.provider == "cgaz":
            return cgaz_url(cast("str", dataset.adm_level))
        if dataset.provider == "natural_earth":
            scale = self._scale or dataset.default_scale
            return natural_earth_url(cast("str", scale), cast("str", dataset.layer))
        if dataset.provider == "tiger":
            year = self._year or dataset.default_year
            scope = self._state if dataset.per_state else "us"
            return tiger_url(
                cast("int", year),
                cast("str", dataset.layer),
                cast("str", dataset.resolution),
                scope=cast("str", scope),
            )
        raise ValueError(f"unsupported admin provider: {dataset.provider!r}")

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> FeatureCollection:
        """Fetch the requested administrative boundaries and return the polygons.

        Resolves and reads every requested dataset, concatenates them into a
        single `FeatureCollection` (the common case is one dataset), writes it
        to one vector file when `path` is set, and returns the in-memory
        collection (always EPSG:4326).

        Args:
            progress_bar: Accepted for signature parity with the other
                backends.
            limit: Cap on the total features returned, across every requested
                dataset. Applied as each dataset is read, so a dataset past the
                cap is never fetched. `None` (the default) reads everything.

        Returns:
            FeatureCollection: The boundary polygons, CRS EPSG:4326. Empty
                (schema-only) when nothing was fetched.
        """
        self._limit = self.check_limit(limit)
        collections = self._api()
        collection = self._combine(collections)
        if len(collection) and self._should_write:
            out_path = self._write(collection)
            logger.info(
                f"admin download summary: {len(collection)} feature(s) "
                f"written to {out_path}"
            )
        elif not len(collection):
            logger.warning(
                "admin download summary: no features fetched, nothing written"
            )
        return collection

    @staticmethod
    def _combine(collections: list[FeatureCollection]) -> FeatureCollection:
        """Combine one or more per-dataset collections into a single one.

        A single collection is returned unchanged; several are concatenated
        (a union of columns, EPSG:4326 preserved). An empty list yields the
        schema-only empty collection.

        Args:
            collections: The per-dataset collections from `_api`.

        Returns:
            FeatureCollection: The combined boundaries (EPSG:4326).
        """
        if not collections:
            return empty_fc()
        if len(collections) == 1:
            return collections[0]
        combined = pd.concat(collections, ignore_index=True)
        return FeatureCollection(combined)

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the boundaries to one vector file under `root_dir`.

        The filename embeds the requested dataset ids **and the active selectors**
        (`admin_<ids>[_<country>][_<scale>][_<year>][_<state>].<ext>`, `:`
        replaced by `_`), so two requests that differ only by selector — e.g.
        the same dataset for two countries — land in distinct files instead of
        silently overwriting one another.

        Args:
            collection: The boundaries to write.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        parts = ["admin"] + [v.replace(":", "_") for v in self.vars]
        parts += [
            str(selector)
            for selector in (self._country, self._scale, self._year, self._state)
            if selector
        ]
        stem = "_".join(parts)
        out_path = self.root_dir / f"{stem}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path
