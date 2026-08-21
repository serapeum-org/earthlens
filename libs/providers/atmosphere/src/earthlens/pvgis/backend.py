"""Backend that fetches solar-radiation / PV time series from JRC PVGIS.

`PVGIS(AbstractDataSource)` hits the keyless JRC PVGIS 5.3 non-interactive
REST service (`https://re.jrc.ec.europa.eu/api/v5_3/<tool>`) directly with
`requests` — no SDK, no authentication, no `pyramids` array layer. A request
selects a tool with `variables=["seriescalc"]` (hourly radiation / PV power
time series) or `["tmy"]` (typical meteorological year), samples the request
location(s) — a single `point=(lat, lon)` (or a degenerate bbox), or a bbox
expanded to a point grid at `spacing_deg` — issues one keyless `GET` per point
throttled to the 30 req/s rate limit, parses each JSON response into a
long-format `pandas.DataFrame` tagged with `lat`/`lon`/`product`, and
concatenates them.

This is a `tabular` backend: the result is a per-coordinate hourly table, so
`OUTPUT_KIND = "tabular"` and the `earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument for it — PVGIS already returns the resolved
hourly / TMY series.

Per-tool knobs (PV `pvcalculation` / `peakpower` / `loss` / `angle` /
`aspect` / `components`, and `raddatabase`) are passed as keyword arguments
and merged over the catalog defaults; for `seriescalc` the `start`/`end`
window supplies `startyear`/`endyear`.

PVGIS is **not global** (high latitudes / open sea are excluded): an
out-of-coverage point returns an HTTP 4xx with a JSON `message`. For a
multi-point bbox the point is skipped with a warning; for a single explicit
point a `ValueError` naming the coordinate is raised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.pvgis import _helpers
from earthlens.pvgis.catalog import Catalog, Product

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Default bbox sampling step in degrees (`G3`).
DEFAULT_SPACING_DEG = 0.1

#: Soft / hard request-count guards (`G5`). A grid larger than the soft
#: threshold warns; larger than the hard cap raises, so a country-scale bbox
#: never silently fires thousands of keyless GETs.
WARN_POINTS = 50
DEFAULT_MAX_POINTS = 400

#: The JRC attribution logged once on a successful download (`G7`).
_CITATION = (
    "PVGIS (c) European Union, 2001-2024 — data from the JRC Photovoltaic "
    "Geographical Information System (PVGIS), "
    "https://re.jrc.ec.europa.eu/pvg_tools/. Free reuse with attribution."
)


class PVGIS(AbstractDataSource):
    """JRC PVGIS solar-radiation / PV backend (long-format tabular output).

    Fetches per-coordinate hourly time series for a point (or a bbox sampled
    to a point grid) / date window / tool through the same `download()` shape
    every other earthlens backend uses, and returns a long-format
    `pandas.DataFrame` (one block of rows per sampled point). The query is a
    search/fetch split: `_search` enumerates the `(lat, lon)` points to pull,
    and `_fetch` issues one throttled keyless GET per point and parses it.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row hourly observations,
            so the facade rejects `aggregate=` with `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "PVGIS output is a per-coordinate hourly time series (tabular), not a gridded raster, so there is no meaningful gridded reduction. PVGIS already returns the resolved hourly / TMY series"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "hourly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        point: tuple[float, float] | None = None,
        spacing_deg: float = DEFAULT_SPACING_DEG,
        output_format: OutputFormat = "csv",
        max_points: int = DEFAULT_MAX_POINTS,
        **knobs: Any,
    ):
        """Initialise a PVGIS backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`. For
                `seriescalc` its year becomes `startyear`; `tmy` ignores it.
            end: Inclusive end of the window (its year becomes `endyear`).
            variables: Single-element list naming the product / tool —
                `["seriescalc"]` or `["tmy"]`. An empty list defaults to
                `["seriescalc"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Optional
                when `point=` is given.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Optional
                when `point=` is given.
            temporal_resolution: Recorded resolution label (PVGIS is hourly).
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            point: An explicit `(lat, lon)` to query a single location. When
                given it wins, collapsing the request to that one coordinate
                and overriding any `lat_lim` / `lon_lim` (including the
                whole-Earth defaults the facade injects).
            spacing_deg: Grid step in degrees when sampling a bbox (`G3`).
            output_format: On-disk format — `"csv"` (default) or `"parquet"`.
            max_points: Hard cap on the number of sampled grid points (`G5`);
                a request that would exceed it raises `ValueError`.
            **knobs: PV / radiation query knobs merged over the catalog
                defaults — `raddatabase`, `pvcalculation`, `peakpower`,
                `loss`, `angle`, `aspect`, `components`, plus any other raw
                PVGIS query parameter.

        Raises:
            ValueError: When `output_format` is unrecognised, or when no
                location (neither `point=` nor a bbox) is supplied.
            TypeError: When `variables` is a mapping (this backend takes a
                single-element list naming the tool).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "PVGIS `variables` must be a single-element list naming the "
                "tool (e.g. ['seriescalc'] or ['tmy']), not a mapping. PV "
                "knobs are explicit PVGIS(...) keyword arguments "
                "(peakpower=, angle=, raddatabase=, ...)."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )
        if point is not None:
            # `point=` wins and collapses the bbox to that single coordinate,
            # overriding any `lat_lim`/`lon_lim` (including the whole-Earth
            # defaults the EarthLens facade injects), so a single-point
            # request works identically through the facade and directly.
            lat_lim = [float(point[0]), float(point[0])]
            lon_lim = [float(point[1]), float(point[1])]
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "PVGIS needs a location: pass point=(lat, lon) for a single "
                "site, or lat_lim=/lon_lim= (or aoi=) for a bbox."
            )

        self._catalog = Catalog()
        self._spacing_deg = spacing_deg
        self._output_format: OutputFormat = output_format
        self._max_points = max_points
        self._knobs = dict(knobs)
        self._product_id = (list(variables) or ["seriescalc"])[0]
        self._product: Product | None = None
        self._show_progress = True
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or ["seriescalc"],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Resolve the requested product from the catalog (`G4`).

        Returns `None` — PVGIS is keyless, so there is no client object.

        Raises:
            ValueError: If the first `variables` entry is not a known product
                id (with a did-you-mean hint).
        """
        self._product = self._catalog.get(self._product_id)
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a `TemporalExtent`.

        The resolution label is always `"hourly"` — PVGIS only serves hourly
        series — so the value the caller (or the `EarthLens` facade, whose
        default is `"daily"`) passes for `temporal_resolution` is ignored
        here; recording it verbatim would mislabel the cadence.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Accepted for the shared constructor shape but
                not recorded — the label is fixed to `"hourly"`.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints and an
                `"hourly"` resolution label.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="hourly")

    def _resolved_params(self) -> dict[str, Any]:
        """Build the per-request query params (catalog defaults + knobs).

        Starts from the product's `default_params`, adds the
        `startyear`/`endyear` window for `seriescalc`, then merges the PV
        knobs and any extra raw params so a caller value always wins.

        Returns:
            dict[str, Any]: The query params (excluding `lat`/`lon`/
                `outputformat`, which `build_url` supplies).
        """
        assert self._product is not None  # set by _initialize
        params: dict[str, Any] = dict(self._product.default_params)
        if self._product.tool == "seriescalc":
            params["startyear"] = self.time.start_date.year
            params["endyear"] = self.time.end_date.year
        params.update({k: v for k, v in self._knobs.items() if v is not None})
        return params

    def _points(self) -> list[tuple[float, float]]:
        """Enumerate and guard the `(lat, lon)` sample points (`G3`, `G5`).

        Returns:
            list[tuple[float, float]]: The sampled coordinates.

        Raises:
            ValueError: If the grid exceeds `max_points` — the message tells
                the user to shrink the bbox or coarsen `spacing_deg`.
        """
        points = _helpers.point_grid(self.space, self._spacing_deg)
        if len(points) > self._max_points:
            raise ValueError(
                f"PVGIS request would sample {len(points)} points (> the "
                f"max_points={self._max_points} cap): one keyless GET each. "
                f"Shrink the bbox, coarsen spacing_deg (now {self._spacing_deg}"
                f"), or raise max_points= deliberately."
            )
        if len(points) > WARN_POINTS:
            logger.warning(
                f"PVGIS will issue {len(points)} keyless GETs (one per grid "
                f"point); throttled to <=30 req/s. Coarsen spacing_deg to "
                f"reduce the count."
            )
        return points

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch every sampled point, write the table, and return it.

        Args:
            progress_bar: Show a per-point `tqdm` bar while fetching.
            limit: Cap on the total rows returned, across every requested
                point. Applied as each point's frame arrives, so a point past
                the cap is never requested. `None` (the default) fetches
                everything.

        Returns:
            pd.DataFrame: The concatenated long-format frame — one block of
                hourly rows per in-coverage sampled point, tagged with
                `lat`/`lon`/`product`.

        Raises:
            ValueError: If a single explicit point is out of PVGIS coverage,
                or the grid exceeds `max_points`.
        """
        self._limit = self.check_limit(limit)
        assert self._product is not None  # set by _initialize
        self._show_progress = progress_bar
        frames = [frame for frame in self._api() if frame is not None]
        if frames:
            df = pd.concat(frames, ignore_index=True)
        else:
            df = _helpers.empty_canonical(
                [*self._product.columns, "lat", "lon", "product"]
            )
        out_path = self._write_table(df)
        if len(df):
            logger.info(f"PVGIS {self._product.tool}: {len(df)} row(s) -> {out_path}")
            logger.info(_CITATION)
        else:
            logger.warning(
                f"PVGIS {self._product.tool}: no rows returned; wrote an empty "
                f"(schema-only) table to {out_path}"
            )
        return df

    def _search(self) -> list[RemoteProduct]:
        """Enumerate one product per sampled `(lat, lon)` point (`G3`).

        Returns:
            list[RemoteProduct]: One product per grid point, each carrying
                the `lat`/`lon` and the resolved query params in `metadata`.

        Raises:
            ValueError: If the grid exceeds `max_points` (`G5`).
        """
        assert self._product is not None  # set by _initialize
        params = self._resolved_params()
        return [
            RemoteProduct(
                id=f"{self._product.tool}:{lat},{lon}",
                metadata={"lat": lat, "lon": lon, "params": params},
            )
            for lat, lon in self._points()
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame | None]:
        """Fetch each point's series, throttled and coverage-aware.

        Widens the inherited `-> list[Path]` contract: a tabular backend
        returns in-memory long-format frames (or `None` for a skipped
        out-of-coverage point), not file paths.

        Args:
            products: The list returned by `_search`.

        Returns:
            list[pd.DataFrame | None]: One frame per in-coverage point (same
                order); `None` where a multi-point bbox skipped a point.
                Truncated when `limit=` was passed to `download`.
        """
        from tqdm import tqdm

        last_call = [0.0]
        single = len(products) == 1
        # `with`, so a cap that stops the sweep early still closes the bar:
        # tqdm keeps redrawing and holds the terminal until it is closed.
        with (
            tqdm(
                products,
                disable=not self._show_progress,
                desc="PVGIS",
                unit="point",
            ) as iterator,
            requests.Session() as session,
        ):
            # Lazy so a `limit=` stops the work: a point past the cap is never
            # requested. Skipped (`None`) points count as zero rows, so the cap
            # bounds returned rows rather than attempted points.
            return self._take_limited(
                (
                    self._fetch_point(product, session, last_call, single=single)
                    for product in iterator
                ),
                limit=self._limit,
                size=lambda frame: 0 if frame is None else len(frame),
            )

    def _fetch_point(
        self,
        product: RemoteProduct,
        session: Any,
        last_call: list[float],
        *,
        single: bool,
    ) -> pd.DataFrame | None:
        """Fetch and parse one point, applying the coverage policy (`G6`).

        Args:
            product: One `RemoteProduct` from `_search`.
            session: The shared `requests.Session`.
            last_call: The shared throttle timestamp (one-element list).
            single: Whether this is the only point (an explicit single site).

        Returns:
            pd.DataFrame | None: The parsed frame tagged with
                `lat`/`lon`/`product`, or `None` when an out-of-coverage
                point is skipped (multi-point bbox only).

        Raises:
            ValueError: When a single explicit point is out of coverage; the
                message names the coordinate and the PVGIS error.
        """
        assert self._product is not None  # set by _initialize
        lat = product.metadata["lat"]
        lon = product.metadata["lon"]
        params = product.metadata["params"]
        url = _helpers.build_url(self._product.endpoint, lat, lon, params)
        resp = _helpers.throttled_get(session, url, last_call=last_call)
        if resp.status_code >= 400:
            message = self._error_message(resp)
            if single:
                raise ValueError(
                    f"PVGIS returned no data for point (lat={lat}, lon={lon}): "
                    f"{message} (the location may be outside PVGIS coverage)."
                )
            logger.warning(f"PVGIS skipped point (lat={lat}, lon={lon}): {message}")
            return None
        payload = resp.json()
        if self._product.tool == "tmy":
            df = _helpers.parse_tmy(payload)
        else:
            df = _helpers.parse_seriescalc(payload)
        df["lat"] = lat
        df["lon"] = lon
        df["product"] = self._product.tool
        return df

    @staticmethod
    def _error_message(resp: Any) -> str:
        """Extract a readable error message from a PVGIS error response.

        Args:
            resp: The HTTP response with a 4xx/5xx status.

        Returns:
            str: The JSON `message` field when present, else the raw body
                (truncated).
        """
        try:
            return cast("str", _helpers.error_message(resp.json()) or resp.text[:200])
        except ValueError:
            return cast("str", resp.text[:200])

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write the long-format table to `root_dir` and return the path.

        Args:
            df: The canonical long-format frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: When `output_format="parquet"` but `pyarrow` is
                missing.
        """
        assert self._product is not None  # set by _initialize
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"pvgis_{self._product.tool}.{ext}"
        if self._output_format == "parquet":
            try:
                df.to_parquet(out_path, index=False)
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "Writing Parquet requires 'pyarrow'. Install it (pip "
                    "install pyarrow) or use output_format='csv'."
                ) from exc
        else:
            df.to_csv(out_path, index=False)
        return out_path
