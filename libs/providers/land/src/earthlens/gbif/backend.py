"""Backend that queries GBIF species occurrences via pygbif.

`GBIF(AbstractDataSource)` fetches species-occurrence records from the
Global Biodiversity Information Facility — 3 B+ georeferenced occurrences
— through `pygbif.occurrences.search`, the anonymous public occurrence
API. The request is one taxon/geometry/time window: every entry in
`variables` resolves to a GBIF backbone `taxonKey`, the bounding box
becomes a WKT polygon, and `[start, end]` becomes GBIF's `eventDate`
range. The paginated result is mapped to a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of points by the
shared `earthlens.biodiversity` mapper.

This is the reference occurrence backend the OBIS twin mirrors. Like
FDSN/Overture it is a `vector` backend (`OUTPUT_KIND = "vector"`), so the
:class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=`
argument. `download()` returns the in-memory FeatureCollection and, when
`path` is set, writes it (GeoParquet by default).

GBIF search is paginated at 300 records/page and capped at an
`offset + limit` of 100,000; the backend loops pages up to `max_records`
(default 100,000, GBIF's own ceiling) and logs a one-line note when the
upstream count exceeds the cap, pointing at GBIF's async download API for
larger pulls.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)
from earthlens.biodiversity import occurrences_to_fc, warn_license, wkt_from_bbox
from earthlens.gbif.catalog import Catalog

if TYPE_CHECKING:
    import pandas as pd
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

FileFormat = Literal["geoparquet", "gpkg", "geojson"]

#: Output format -> (OGR driver or `"parquet"`, file extension).
_FORMATS: dict[str, tuple[str, str]] = {
    "geoparquet": ("parquet", "parquet"),
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: GBIF returns at most 300 records per `occ.search` page.
_PAGE_SIZE = 300

#: GBIF rejects `offset + limit` beyond 100,000 on the search endpoint;
#: deeper pulls require the async download API. This is the record ceiling.
_OFFSET_CAP = 100_000

#: GBIF's normalised CC license value for non-commercial occurrence rows.
_CC_BY_NC = "CC_BY_NC_4_0"

#: Ordered occurrence attribute columns and their pandas dtypes. `geometry`
#: (a `shapely.Point` per row) is added by `occurrences_to_fc`.
GBIF_COLUMNS: dict[str, str] = {
    "key": "Int64",
    "scientificName": "string",
    "taxonKey": "Int64",
    "decimalLatitude": "float64",
    "decimalLongitude": "float64",
    "eventDate": "string",
    "basisOfRecord": "string",
    "datasetKey": "string",
    "license": "string",
    "countryCode": "string",
    "coordinateUncertaintyInMeters": "float64",
}


class GBIF(AbstractDataSource):
    """GBIF species-occurrence backend (vector point-feature output).

    Wraps `pygbif.occurrences.search` so a user can pull a
    taxon/space/time window of species occurrences through the same
    `download()` shape every other earthlens backend uses. Each entry in
    `variables` resolves to a GBIF backbone `taxonKey`; the bbox becomes a
    WKT polygon; the paginated result is mapped to a points
    :class:`~pyramids.feature.collection.FeatureCollection`.

    Attributes:
        OUTPUT_KIND: `"vector"` — the facade rejects `aggregate=`.

    Examples:
        - Build a backend for a one-year bird search (no network call):
            ```python
            >>> from earthlens.gbif import GBIF
            >>> backend = GBIF(
            ...     start="2020-01-01", end="2020-12-31",
            ...     variables=["birds"], lat_lim=[0.0, 10.0], lon_lim=[0.0, 10.0],
            ... )
            >>> backend.OUTPUT_KIND
            'vector'
            >>> backend._plan_search()["taxonKey"]
            212

            ```
    """

    OUTPUT_KIND: OutputKind = "vector"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list,
        lat_lim: list,
        lon_lim: list,
        temporal_resolution: str = "all",
        path: str = "",
        fmt: str = "%Y-%m-%d",
        max_records: int = _OFFSET_CAP,
        file_format: FileFormat = "geoparquet",
    ):
        """Configure a GBIF occurrence query.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string (parsed with `fmt`).
            variables: Taxon selectors — friendly catalog keys
                (`["birds"]`), raw integer `taxonKey`s, or
                `"taxon:<scientific name>"` live lookups. Empty defaults
                to `["animals"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: GBIF issues a single search spanning the
                whole window, so this is the sentinel `"all"`.
            path: Output directory for the occurrence file. The empty string
                (the default) opts out of writing — `download()` returns the
                in-memory FeatureCollection without touching the filesystem.
                Pass an explicit directory to write the file.
            fmt: `strptime` format for `start` / `end`.
            max_records: Page cap; the search stops once this many records
                are collected. Defaults to GBIF's 100,000 search ceiling.
                Larger pulls need GBIF's async download API.
            file_format: Output vector format — `"geoparquet"` (default),
                `"gpkg"`, or `"geojson"`.

        Raises:
            TypeError: If `variables` is a mapping.
            ValueError: If `file_format` is unknown.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "GBIF `variables` must be a list of taxon selectors (e.g. "
                "['birds'], [212], or ['taxon:Panthera leo']), not a mapping."
            )
        if file_format not in _FORMATS:
            raise ValueError(
                f"file_format must be one of {sorted(_FORMATS)}, got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
        self._max_records = max_records
        # Capture the user's original `path` so download() can honour `path=""`
        # as "do not write a file" — the parent class absolutises path into a
        # `Path` (always non-empty), so the original value is the only way to
        # detect an explicit opt-out.
        self._user_path = path
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or ["animals"],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """No global client — `pygbif` is a stateless module imported lazily.

        Returns:
            None: GBIF occurrence search is anonymous and stateless.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

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
        """Parse `[start, end]` into a :class:`TemporalExtent`.

        GBIF issues one search spanning the window, so there is no
        per-date loop and the resolution is the sentinel `"all"`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: The sentinel `"all"`.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: The validated `[start, end]` window.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _plan_search(self) -> dict[str, Any]:
        """Build the `occ.search` keyword arguments for the request.

        Resolves every `variables` entry to a backbone `taxonKey` (a
        single key stays an `int`; several become a `list`), turns the
        bbox into a WKT polygon (`wkt_from_bbox`, since `SpatialExtent`
        has no `.wkt()`), and joins the window into GBIF's `eventDate`
        range. `hasCoordinate=True` drops records without coordinates so
        every row maps to a point. The `taxonKey` rides through
        `occ.search`'s `**kwargs` (there is no explicit parameter).

        Returns:
            dict[str, Any]: The `occ.search` kwargs (`taxonKey`,
                `geometry`, `eventDate`, `hasCoordinate`).
        """
        keys = [self._catalog.resolve_taxon_key(v) for v in self.vars]
        start = self.time.start_date.strftime("%Y-%m-%d")
        end = self.time.end_date.strftime("%Y-%m-%d")
        return {
            "taxonKey": keys[0] if len(keys) == 1 else keys,
            "geometry": wkt_from_bbox(self.space),
            "eventDate": f"{start},{end}",
            "hasCoordinate": True,
        }

    def _page(self, occ: Any, params: dict[str, Any]) -> list[dict]:
        """Run the paginated `occ.search` loop up to `max_records`.

        Accumulates `result["results"]` page by page (300/page), stopping
        on `result["endOfRecords"]`, when `max_records` is reached, or
        before a page would breach GBIF's 100,000 `offset + limit` ceiling
        (since 300 does not divide 100,000, the last fully-valid page ends
        at 99,900 records). Logs one line when the upstream `count` exceeds
        `max_records`, pointing at the async download API.

        Args:
            occ: The `pygbif.occurrences` module (or a stand-in exposing
                `search(**kwargs)`).
            params: The `occ.search` kwargs from :meth:`_plan_search`.

        Returns:
            list[dict]: Up to `max_records` occurrence record dicts.
        """
        rows: list[dict] = []
        offset = 0
        count: int | None = None
        while True:
            result = occ.search(**params, limit=_PAGE_SIZE, offset=offset)
            if count is None:
                count = result.get("count")
            rows.extend(result.get("results", []))
            offset += _PAGE_SIZE
            # Stop before issuing a request whose `offset + limit` would breach
            # GBIF's 100,000 search ceiling (it 4xx's such requests).
            if (
                result.get("endOfRecords")
                or len(rows) >= self._max_records
                or offset + _PAGE_SIZE > _OFFSET_CAP
            ):
                break
        if count is not None and count > self._max_records:
            logger.info(
                f"GBIF search matched {count} records; capped at "
                f"{self._max_records}. Use GBIF's async download API "
                "(occ.download) for the full set."
            )
        return rows[: self._max_records]

    def _fetch(self) -> FeatureCollection:  # type: ignore[override]
        """Run the search and map the rows to a FeatureCollection.

        Imports `pygbif` lazily (so the package imports without the
        `[gbif]` extra), pages the search, maps the rows via the shared
        `occurrences_to_fc`, and warns once per distinct restrictive
        license present (GBIF mixes CC0 / CC-BY / CC-BY-NC).

        Returns:
            FeatureCollection: The occurrence points, CRS `EPSG:4326`;
                empty (schema-only) when nothing matched.
        """
        from pygbif import occurrences as occ

        rows = self._page(occ, self._plan_search())
        collection = occurrences_to_fc(
            rows,
            lat_field="decimalLatitude",
            lon_field="decimalLongitude",
            columns=GBIF_COLUMNS,
        )
        licenses = {row.get("license") for row in rows}
        for lic in sorted(lic for lic in licenses if lic is not None):
            warn_license(lic, "gbif")
        return collection

    def _api(self) -> FeatureCollection:
        """Run the occurrence search (satisfies the abstract contract)."""
        return self._fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> FeatureCollection:
        """Run the occurrence search and return the points FeatureCollection.

        Args:
            progress_bar: Accepted for signature parity; GBIF's paginated
                search has no progress bar, so this is a no-op.
            aggregate: Must be `None`. Occurrences are vector, not
                gridded; the facade already rejects a non-`None`
                `aggregate=` for a `vector` backend.

        Returns:
            FeatureCollection: The occurrence points, CRS `EPSG:4326`.
                Empty (schema-only) when nothing matched. Written to a
                file under `path` when `path` is set.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "GBIF.download(aggregate=...) is not supported: occurrences "
                "are vector point features, not gridded rasters. Call "
                "download() without aggregate= and post-process the returned "
                "FeatureCollection (a GeoDataFrame) directly."
            )
        collection = self._fetch()
        if self._user_path and len(collection):
            written = self._write(collection)
            logger.info(
                f"GBIF download summary: {len(collection)} occurrence(s) "
                f"written to {written}"
            )
        else:
            logger.info(
                f"GBIF download summary: {len(collection)} occurrence(s), "
                "nothing written"
            )
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the occurrences to a vector file under `root_dir`.

        Args:
            collection: The occurrence FeatureCollection.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _FORMATS[self._file_format]
        out_path = self.root_dir / f"gbif_occurrences.{ext}"
        if driver == "parquet":
            collection.to_parquet(str(out_path))
        else:
            collection.to_file(str(out_path), driver=driver)
        return out_path
