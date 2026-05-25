"""Backend that queries the NASA FIRMS active-fire CSV API over HTTPS.

`FIRMS(AbstractDataSource)` fetches active-fire detections — one point
per fire pixel, with brightness, confidence, and fire-radiative-power —
from the NASA FIRMS (Fire Information for Resource Management System)
area CSV endpoint, across MODIS (C6.1) and VIIRS (S-NPP / NOAA-20 /
NOAA-21) sensors. The rows for a `[start, end]` window over a bbox come
back as CSV, which :mod:`earthlens.firms.events` maps to a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of fire-pixel
points.

This is a `vector` backend: the on-the-wire result is a table of
geolocated detections, not a gridded array, so `OUTPUT_KIND = "vector"`
and the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (there is no meaningful gridded reduction of a
detection table). `download()` returns the in-memory FeatureCollection
and, as a side effect, writes it to one vector file under `path`.

FIRMS needs a free **`MAP_KEY`** (resolved by :class:`FirmsAuth`) but no
SDK and no `[firms]` extra — the only dependencies are `requests` +
`pandas`, both core. Sensor selection follows the vector-backend reading
of `variables` (see the package docstring): `variables` is a `list[str]`
of FIRMS sensor codes (`["VIIRS_SNPP_NRT"]`,
`["MODIS_NRT", "VIIRS_SNPP_NRT"]`); the detection filters ride as
explicit `min_confidence=` / `day_night=` keyword arguments. The
temporal window is chunked internally into ≤10-day requests (the FIRMS
per-request cap), so `temporal_resolution` carries the sentinel `"all"`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.firms.auth import FirmsAuth, FirmsCredentials
from earthlens.firms.catalog import Catalog

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.aggregate import AggregationConfig

#: Default sensor when `variables=[]` — the highest-resolution current
#: NRT sensor.
_DEFAULT_SENSORS = ["VIIRS_SNPP_NRT"]

#: FIRMS caps `day_range` at 10 days per area request (windows longer
#: than this are chunked in :meth:`FIRMS._search`).
MAX_DAY_RANGE = 10

FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}


class FIRMS(AbstractDataSource):
    """NASA FIRMS active-fire backend (vector point-feature output).

    Wraps the FIRMS area CSV API so a user can pull a space/time/sensor
    window of fire detections through the same `download()` shape every
    other earthlens backend uses. Windows longer than the FIRMS 10-day
    per-request cap are chunked, and each `(sensor, ≤10-day chunk)` is
    one CSV GET; the rows are mapped to a
    :class:`~pyramids.feature.collection.FeatureCollection`.

    FIRMS needs a free `MAP_KEY` (resolved by :class:`FirmsAuth`).

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of detection
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
        map_key: str | None = None,
        min_confidence: float | None = None,
        day_night: str | None = None,
        file_format: FileFormat = "gpkg",
        timeout: float = 60.0,
    ):
        """Initialise a FIRMS backend instance.

        Args:
            start: Inclusive start of the detection window, as a string
                parsed with `fmt`.
            end: Inclusive end of the detection window.
            variables: List of FIRMS sensor codes to query
                (`["VIIRS_SNPP_NRT"]`, `["MODIS_NRT", "VIIRS_SNPP_NRT"]`).
                For this backend `variables` names the *sensors*, not
                data variables (see the package docstring). An empty list
                defaults to `["VIIRS_SNPP_NRT"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: FIRMS chunks by ≤10-day windows
                internally, not by a daily/monthly cadence, so this is
                the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the written vector file. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            map_key: FIRMS `MAP_KEY`. Resolved (with the `FIRMS_MAP_KEY`
                env var as fallback) by :class:`FirmsAuth`; `None` defers
                to the environment.
            min_confidence: Optional 0-100 lower bound applied
                client-side on the normalised `confidence_pct` column
                (FIRMS has no server-side confidence filter). `None`
                keeps every detection.
            day_night: Optional `"D"` / `"N"` filter applied client-side
                on the `daynight` column. `None` keeps both.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.
            timeout: Per-request timeout in seconds for each CSV GET.

        Raises:
            ValueError: If `file_format` is not `"gpkg"` / `"geojson"`.
            TypeError: If `variables` is a mapping rather than a list of
                sensor codes.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got "
                f"{file_format!r}."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "FIRMS `variables` must be a list of sensor codes (e.g. "
                "['VIIRS_SNPP_NRT', 'MODIS_NRT']), not a mapping. For this "
                "backend `variables` selects sensors, not data variables; the "
                "detection filters are the explicit min_confidence= / "
                "day_night= keyword arguments."
            )
        self._map_key = map_key
        self._min_confidence = min_confidence
        self._day_night = day_night
        self._file_format: FileFormat = file_format
        self._timeout = timeout
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_SENSORS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> FirmsAuth:
        """Build and configure the :class:`FirmsAuth` for this instance.

        Returns:
            FirmsAuth: The configured auth holding the resolved
                `MAP_KEY`.

        Raises:
            AuthenticationError: If no `MAP_KEY` can be resolved from
                `map_key=` or `FIRMS_MAP_KEY`.
        """
        auth = FirmsAuth(FirmsCredentials(map_key=self._map_key))
        auth.configure()
        return auth

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        FIRMS clips server-side to the bbox path segment, so the box
        passes through unchanged.

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

        FIRMS chunks the window into ≤10-day requests internally (see
        :meth:`_search`), so the resolution is kept as the sentinel
        `"all"` (not a real pandas frequency alias) and `dates`
        collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label;
                FIRMS always chunks the full window.
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
        """List one product per `(sensor, ≤10-day chunk)` (wired in C2)."""
        raise NotImplementedError(
            "FIRMS._search is implemented in the search/fetch task (C2)."
        )

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Fetch each product's CSV into a FeatureCollection (wired in C2)."""
        raise NotImplementedError(
            "FIRMS._fetch is implemented in the search/fetch task (C2)."
        )

    def _api(self) -> list[FeatureCollection]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> FeatureCollection:
        """Query FIRMS and return the matched detections (wired in C2)."""
        raise NotImplementedError(
            "FIRMS.download is implemented in the search/fetch task (C2)."
        )
