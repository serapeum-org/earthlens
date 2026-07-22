"""Backend that queries the GDACS multi-hazard alert feed over HTTPS.

`GDACS(AbstractDataSource)` fetches multi-hazard disaster alerts —
earthquakes, tropical cyclones, floods, volcanoes, wildfires, and
droughts, each with a green/orange/red impact score — from the Global
Disaster Alert and Coordination System (JRC / UN OCHA) SEARCH feed. The
whole event list for a `[start, end]` window comes back in a single
HTTPS GET, which :mod:`earthlens.gdacs.events` maps to a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of alert
features.

This is a `vector` backend: the on-the-wire result is a table of
geolocated alerts, not a gridded array, so `OUTPUT_KIND = "vector"` and
the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (there is no meaningful gridded reduction of an
alert table). `download()` returns the in-memory FeatureCollection and,
as a side effect, writes it to one vector file under `path`.

GDACS needs **no credentials** — the SEARCH feed is public, so there is
no `auth.py` and no `[gdacs]` extra (the only dependency is `requests`,
a core dep). Hazard-type selection follows the vector-backend reading
of `variables` (see the package docstring): `variables` is a
`list[str]` of GDACS hazard-type codes (`["EQ"]`, `["EQ", "TC"]`); the
alert-level filter rides as an explicit `alert_level=` kwarg. The
temporal window is a single unchunked `[start, end]` query — GDACS does
not iterate per day/month — so `temporal_resolution` carries the
sentinel `"all"`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
import requests  # noqa: F401  # runtime seam so tests can monkeypatch this module's `requests`
from earthlens.base.http import HttpClient
from earthlens.base.http import RequestsGet as _RequestsGet
from earthlens.gdacs.catalog import Catalog
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.gdacs import events

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from pyramids.feature.collection import FeatureCollection


#: GDACS SEARCH endpoint — returns the whole event list for a window in
#: one GeoJSON response (verified against the live service).
SEARCH_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"

#: GDACS SEARCH caps every response at the 100 most-recent events and
#: honours no pagination / `limit` / `offset` parameter (verified
#: against the live service: a one-year all-hazard query returns exactly
#: 100, and `page=2` / `offset=100` return the same first 100). A
#: response at this size is therefore assumed truncated, and `_fetch`
#: warns so the truncation is never silent. The remedy is to narrow the
#: date window (or query fewer hazard types) — there is no way to page
#: past it, and chunking would mean per-window fan-out this single-request
#: backend deliberately avoids.
MAX_EVENTS_PER_RESPONSE = 100

#: The complete GDACS hazard-type universe; `variables=[]` defaults here.
_ALL_TYPES = ["EQ", "TC", "FL", "VO", "WF", "DR"]

#: The three GDACS alert levels; `alert_level=None` defaults here.
_ALL_LEVELS = ["Green", "Orange", "Red"]

FileFormat = Literal["gpkg", "geojson"]

#: Map output format to the OGR driver and file extension `to_file` uses.
_DRIVERS: dict[str, tuple[str, str]] = {
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}


class GDACS(AbstractDataSource):
    """GDACS multi-hazard alert backend (vector point-feature output).

    Wraps the public GDACS SEARCH feed so a user can pull a
    space/time/hazard window of disaster alerts through the same
    `download()` shape every other earthlens backend uses. All requested
    hazard types come back in one combined GET; the GeoJSON is mapped to
    a :class:`~pyramids.feature.collection.FeatureCollection` and, if the
    feed did not honour a bbox, clipped to the requested box
    client-side.

    The feed needs no credentials.

    Attributes:
        OUTPUT_KIND: `"vector"` — the result is a table of alert
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
        alert_level: list[str] | None = None,
        file_format: FileFormat = "gpkg",
        timeout: float = 60.0,
    ):
        """Initialise a GDACS backend instance.

        Args:
            start: Inclusive start of the alert window, as a string
                parsed with `fmt`.
            end: Inclusive end of the alert window.
            variables: List of GDACS hazard-type codes to query
                (`["EQ"]`, `["EQ", "TC"]`). For this backend `variables`
                names the *hazard types*, not data variables (see the
                package docstring). An empty list defaults to all six
                types (`["EQ", "TC", "FL", "VO", "WF", "DR"]`).
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: GDACS does not chunk by day/month — the
                whole `[start, end]` window is one query — so this is
                the sentinel `"all"`, not a pandas frequency alias.
            path: Output directory for the written vector file. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            alert_level: Alert levels to keep — any of `"Green"`,
                `"Orange"`, `"Red"`. `None` (the default) keeps all
                three.
            file_format: Output vector format — `"gpkg"` (default,
                GeoPackage) or `"geojson"`.
            timeout: Per-request timeout in seconds for the SEARCH GET.

        Raises:
            ValueError: If `file_format` is not `"gpkg"` / `"geojson"`.
            TypeError: If `variables` is a mapping rather than a list of
                hazard codes.
        """
        if file_format not in _DRIVERS:
            raise ValueError(
                f"file_format must be one of {sorted(_DRIVERS)}, got {file_format!r}."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "GDACS `variables` must be a list of hazard-type codes (e.g. "
                "['EQ', 'TC']), not a mapping. For this backend `variables` "
                "selects hazard types, not data variables; the alert-level "
                "filter is the explicit alert_level= keyword argument."
            )
        self._alert_levels = list(alert_level) if alert_level else list(_ALL_LEVELS)
        self._file_format: FileFormat = file_format
        self._timeout = timeout
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_ALL_TYPES),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """No auth, no client — GDACS is a public feed.

        Returns:
            None: No per-instance client object.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        GDACS takes no server-side bbox, so the box passes through
        unchanged and is applied client-side in :meth:`_fetch`.

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

        GDACS issues a single SEARCH call spanning the whole window, so
        there is no per-date loop. The resolution is kept as the
        sentinel `"all"` (not a real pandas frequency alias — it means
        "single unchunked window") and `dates` collapses to the two
        endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Ignored beyond being recorded as the
                resolution label; GDACS always queries the full window.
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
        """One :class:`RemoteProduct` carrying the whole combined query.

        Resolves every hazard code in `self.vars` against the bundled
        catalog (raising with a did-you-mean hint on an unknown code)
        and records the requested hazard types, alert levels, and date
        window on a single product's metadata — GDACS returns every
        type in one GET, so there is no per-type product. No network
        call is made here.

        Returns:
            list[RemoteProduct]: A single product whose `metadata`
                carries `event_types`, `alert_levels`, `from`, and `to`.

        Raises:
            ValueError: If a code in `self.vars` is not a registered
                hazard type.
        """
        for code in self.vars:
            self._catalog.get_hazard(code)
        return [
            RemoteProduct(
                id="gdacs-query",
                metadata={
                    "event_types": list(self.vars),
                    "alert_levels": list(self._alert_levels),
                    "from": self.time.start_date,
                    "to": self.time.end_date,
                },
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[FeatureCollection]:
        """Issue the one combined GET and map the GeoJSON to a FeatureCollection.

        Widens the inherited `-> list[Path]` contract: a vector backend
        returns in-memory :class:`FeatureCollection`s, not file paths.
        An HTTP error propagates (it is not silently swallowed); an
        empty feed yields a schema-correct empty FeatureCollection.
        Because GDACS SEARCH has no documented bbox filter, the mapped
        alerts are clipped to `self.space` client-side.

        When the response hits the endpoint's
        :data:`MAX_EVENTS_PER_RESPONSE` cap (100), the result is almost
        certainly truncated to the most-recent matches; a warning is
        logged so the truncation is never silent (the endpoint offers no
        pagination — narrow the window to retrieve more).

        Args:
            products: The single-element list returned by
                :meth:`_search`.

        Returns:
            list[FeatureCollection]: A single-element list with the
                clipped alert collection.

        Raises:
            requests.HTTPError: If the SEARCH endpoint returns a
                non-2xx status.
        """
        product = products[0]
        params = {
            "fromDate": product.metadata["from"].strftime("%Y-%m-%d"),
            "toDate": product.metadata["to"].strftime("%Y-%m-%d"),
            "eventlist": ",".join(product.metadata["event_types"]),
            "alertlevel": ";".join(product.metadata["alert_levels"]),
        }
        logger.info(
            f"Querying GDACS SEARCH for {params['eventlist']} alerts "
            f"{params['fromDate']}..{params['toDate']} "
            f"(levels {params['alertlevel']})"
        )
        http = HttpClient(
            session=cast("requests.Session | None", _RequestsGet()),
            timeout=self._timeout,
            max_retries=0,
            status_forcelist=(),
            raise_for_status=True,
        )
        payload = http.get_json(SEARCH_URL, params=params)
        feature_count = len(payload.get("features") or [])
        if feature_count >= MAX_EVENTS_PER_RESPONSE:
            logger.warning(
                f"GDACS SEARCH returned {feature_count} events - its hard cap. "
                "The result is the 100 most-recent matching alerts and is "
                "almost certainly truncated; the endpoint offers no pagination. "
                "Narrow the date window (or query fewer hazard types) to "
                "retrieve the rest."
            )
        collection = events.geojson_to_fc(payload)
        clipped = events.clip_to_bbox(
            collection,
            [self.space.south, self.space.north],
            [self.space.west, self.space.east],
        )
        return [clipped]

    def _api(self) -> list[FeatureCollection]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> FeatureCollection:
        """Query GDACS once and return the matched alerts.

        Issues the single combined SEARCH GET, maps and clips the
        result, writes it to one vector file under `path`, and returns
        the in-memory :class:`FeatureCollection`. An empty result
        returns a schema-correct empty FeatureCollection and writes
        nothing.

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; GDACS is a single request, so this is a no-op.
            aggregate: Must be `None`. Alerts are vector, not gridded,
                so there is no meaningful aggregation. The facade
                already rejects a non-`None` `aggregate=` for a
                `vector` backend; this is the belt-and-suspenders guard
                for direct backend callers.

        Returns:
            FeatureCollection: The matched alerts, CRS `EPSG:4326`.
                Empty (schema-only) when nothing matched.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "GDACS.download(aggregate=...) is not supported: disaster "
                "alerts are vector features, not gridded rasters, so there "
                "is no meaningful gridded reduction. Call download() without "
                "aggregate= and post-process the returned FeatureCollection "
                "(a GeoDataFrame) directly."
            )

        collections = self._api()
        collection = collections[0] if collections else events.empty_fc()

        if len(collection):
            out_path = self._write(collection)
            logger.info(
                f"GDACS download summary: {len(collection)} alert(s) written "
                f"to {out_path}"
            )
        else:
            logger.warning(
                "GDACS download summary: no alerts matched the request, nothing written"
            )
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the alerts to one vector file under `root_dir`.

        The filename embeds the query's date window
        (`gdacs_alerts_<from>_<to>.<ext>`), so downloading successive
        windows into the same `path` — the recommended way to page past
        the 100-event cap — yields distinct files instead of silently
        overwriting one another. Two downloads of the *same* window do
        overwrite, which is the intended idempotent behaviour.

        Args:
            collection: The alerts to write.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _DRIVERS[self._file_format]
        stem = (
            f"gdacs_alerts_{self.time.start_date:%Y-%m-%d}"
            f"_{self.time.end_date:%Y-%m-%d}"
        )
        out_path = self.root_dir / f"{stem}.{ext}"
        collection.to_file(str(out_path), driver=driver)
        return out_path
