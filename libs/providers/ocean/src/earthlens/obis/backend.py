"""Backend that queries OBIS marine occurrences via pyobis.

`OBIS(AbstractDataSource)` fetches marine species-occurrence records from
the Ocean Biodiversity Information System — 140 M+ georeferenced marine
occurrences — through `pyobis.occurrences.search`, the anonymous public
API. It is the marine twin of the GBIF backend: same `OUTPUT_KIND =
"vector"`, the same `occurrences_to_fc` mapper, and the same return
contract; only the SDK call and the request fields differ.

`pyobis` 1.x returns a lazy `OccResponse` query object whose `.execute()`
yields a `pandas.DataFrame` (one row per occurrence) — not the 0.x
`{"results": [...]}` dict. The DataFrame is mapped straight to a points
:class:`~pyramids.feature.collection.FeatureCollection` via the shared
`earthlens.biodiversity` mapper (OBIS is Darwin Core, so the coordinate
fields are `decimalLatitude` / `decimalLongitude`, same as GBIF).

`download()` returns the in-memory FeatureCollection and, when `path` is
set, writes it (GeoParquet by default). Like GBIF the facade rejects an
`aggregate=` argument.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from earthlens.obis.catalog import Catalog
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.biodiversity import occurrences_to_fc, warn_license, wkt_from_bbox

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from pyramids.feature.collection import FeatureCollection

FileFormat = Literal["geoparquet", "gpkg", "geojson"]

#: Output format -> (OGR driver or `"parquet"`, file extension).
_FORMATS: dict[str, tuple[str, str]] = {
    "geoparquet": ("parquet", "parquet"),
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}

#: Default cap on the number of occurrence records fetched per request.
_DEFAULT_SIZE = 10_000

#: Ordered occurrence attribute columns and their pandas dtypes. `geometry`
#: (a `shapely.Point` per row) is added by `occurrences_to_fc`.
OBIS_COLUMNS: dict[str, str] = {
    "id": "string",
    "scientificName": "string",
    "decimalLatitude": "float64",
    "decimalLongitude": "float64",
    "eventDate": "string",
    "depth": "float64",
    "basisOfRecord": "string",
    "dataset_id": "string",
    "license": "string",
}


class OBIS(AbstractDataSource):
    """OBIS marine-occurrence backend (vector point-feature output).

    Wraps `pyobis.occurrences.search` so a user can pull a
    species/space/time window of marine occurrences through the same
    `download()` shape every other earthlens backend uses. Each entry in
    `variables` resolves to an OBIS `scientificname`; the bbox becomes a
    WKT polygon; the `.execute()` DataFrame is mapped to a points
    :class:`~pyramids.feature.collection.FeatureCollection`.

    Attributes:
        OUTPUT_KIND: `"vector"` — the facade rejects `aggregate=`.

    Examples:
        - Build a backend for a dolphin search (no network call):
            ```python
            >>> from earthlens.obis import OBIS
            >>> backend = OBIS(
            ...     start="2015-01-01", end="2020-12-31",
            ...     variables=["common-dolphin"],
            ...     lat_lim=[30.0, 45.0], lon_lim=[-10.0, 5.0],
            ... )
            >>> backend.OUTPUT_KIND
            'vector'
            >>> backend._plan_search()["scientificname"]
            'Delphinus delphis'

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
        size: int = _DEFAULT_SIZE,
        file_format: FileFormat = "geoparquet",
    ):
        """Configure an OBIS occurrence query.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string (parsed with `fmt`).
            variables: Species selectors — friendly catalog keys
                (`["blue-whale"]`) or `"species:<scientific name>"`
                explicit names. Must be non-empty.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: OBIS issues a single search spanning the
                whole window, so this is the sentinel `"all"`.
            path: Output directory for the occurrence file. The empty string
                (the default) opts out of writing — `download()` returns the
                in-memory FeatureCollection without touching the filesystem.
                Pass an explicit directory to write the file.
            fmt: `strptime` format for `start` / `end`.
            size: Maximum number of occurrence records to request.
            file_format: Output vector format — `"geoparquet"` (default),
                `"gpkg"`, or `"geojson"`.

        Raises:
            TypeError: If `variables` is a mapping.
            ValueError: If `variables` is empty or `file_format` unknown.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "OBIS `variables` must be a list of species selectors (e.g. "
                "['blue-whale'] or ['species:Mola mola']), not a mapping."
            )
        if not list(variables):
            raise ValueError(
                "OBIS `variables` must name at least one species (a friendly "
                "key like 'blue-whale' or 'species:<scientific name>')."
            )
        if file_format not in _FORMATS:
            raise ValueError(
                f"file_format must be one of {sorted(_FORMATS)}, got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
        self._size = size
        # Preserve the user's original `path` so download() can honour
        # `path=""` as "do not write a file".
        self._user_path = path
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """No global client — `pyobis` is imported lazily in `_fetch`.

        Returns:
            None: OBIS occurrence search is anonymous and stateless.
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

        OBIS issues one search spanning the window, so the resolution is
        the sentinel `"all"`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: The sentinel `"all"`.
            fmt: `strptime` format for both ends.

        Returns:
            TemporalExtent: The validated `[start, end]` window.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _plan_search(self, name: str | None = None) -> dict[str, Any]:
        """Build the `occurrences.search` keyword arguments for one species.

        Turns the bbox into a WKT polygon and maps the window to OBIS's
        `startdate` / `enddate`. `name` defaults to the first `variables`
        entry resolved to a `scientificname`.

        Args:
            name: The OBIS `scientificname` to search; defaults to the
                first `variables` selector resolved through the catalog.

        Returns:
            dict[str, Any]: The `occurrences.search` kwargs
                (`scientificname`, `geometry`, `startdate`, `enddate`,
                `size`).
        """
        if name is None:
            name = self._catalog.resolve_scientific_name(self.vars[0])
        return {
            "scientificname": name,
            "geometry": wkt_from_bbox(self.space),
            "startdate": self.time.start_date.strftime("%Y-%m-%d"),
            "enddate": self.time.end_date.strftime("%Y-%m-%d"),
            "size": self._size,
        }

    def _fetch(self) -> FeatureCollection:
        """Search every requested species and map the rows to a FeatureCollection.

        Imports `pyobis` lazily (so the package imports without the
        `[obis]` extra), runs one lazy query per `variables` entry to a
        `pandas.DataFrame` via `.execute()`, concatenates them, maps via
        the shared `occurrences_to_fc`, and warns once per distinct
        restrictive license present.

        Returns:
            FeatureCollection: The occurrence points, CRS `EPSG:4326`;
                empty (schema-only) when nothing matched.
        """
        from pyobis import occurrences

        frames: list[pd.DataFrame] = []
        licenses: set[str] = set()
        for selector in self.vars:
            name = self._catalog.resolve_scientific_name(selector)
            frame = occurrences.search(**self._plan_search(name)).execute()
            frames.append(frame)
            if "license" in frame.columns:
                licenses.update(frame["license"].dropna())
        combined = (
            pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        )
        collection = occurrences_to_fc(
            combined,
            lat_field="decimalLatitude",
            lon_field="decimalLongitude",
            columns=OBIS_COLUMNS,
        )
        for lic in sorted(licenses):
            warn_license(lic, "obis")
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
            progress_bar: Accepted for signature parity; OBIS search has no
                progress bar, so this is a no-op.
            aggregate: Must be `None`. Occurrences are vector, not gridded;
                the facade already rejects a non-`None` `aggregate=` for a
                `vector` backend.

        Returns:
            FeatureCollection: The occurrence points, CRS `EPSG:4326`.
                Empty (schema-only) when nothing matched. Written to a file
                under `path` when `path` is set.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "OBIS.download(aggregate=...) is not supported: occurrences "
                "are vector point features, not gridded rasters. Call "
                "download() without aggregate= and post-process the returned "
                "FeatureCollection (a GeoDataFrame) directly."
            )
        collection = self._fetch()
        if self._user_path and len(collection):
            written = self._write(collection)
            logger.info(
                f"OBIS download summary: {len(collection)} occurrence(s) "
                f"written to {written}"
            )
        else:
            logger.info(
                f"OBIS download summary: {len(collection)} occurrence(s), "
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
        out_path = self.root_dir / f"obis_occurrences.{ext}"
        if driver == "parquet":
            collection.to_parquet(str(out_path))
        else:
            collection.to_file(str(out_path), driver=driver)
        return out_path
