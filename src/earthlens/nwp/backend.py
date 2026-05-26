"""Backend that fetches open NWP forecasts as bbox-cropped COGs.

`NWP(AbstractDataSource)` is one backend over the open
numerical-weather-prediction buckets — NOAA NODD (GFS / GEFS / HRRR /
…), ECMWF Open Data (IFS), DWD Open Data (ICON), with Météo-France /
ECCC as follow-ons. It differs from the observation-time backends in
its **forecast time axis**: data is indexed by
`(cycle_datetime_utc, forecast_step_hours)`, not a single valid time.
`start` / `end` select the **cycle date range**; a `steps=` /
`horizon=` kwarg picks the forecast lead times; one COG is produced
per `(cycle, step)`.

The request shape is `variables = {model_key: [param, ...]}` (mirrors
the GEE / STAC backends). Each `param` resolves through the catalog to
the centre's selector — a Herbie `search` regex or a DWD variable
token. The download path per model is the catalog `backend:` value,
dispatched to a sibling :mod:`earthlens.nwp.centres` module.

`OUTPUT_KIND` is fixed `"raster"`: every centre yields a GRIB2 file
that the shared pipeline reads with `pyramids.grib.open_grib`, crops
to the request bbox, and writes as a COG (`C3`); `aggregate=` reduces
the `(cycle, step)` stack (`C6`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.nwp.catalog import KNOWN_BACKENDS, Catalog, NWPModel

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


class NWP(AbstractDataSource):
    """Open numerical-weather-prediction backend (forecast time axis).

    Resolves each requested model key against the bundled catalog,
    dispatches its download to the matching centre module, and yields
    one bbox-cropped COG per `(cycle, step)`. Open buckets only — no
    authentication.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every model yields gridded
            output, so the facade always forwards `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "6hourly",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        *,
        centre: str | None = None,
        mirror: str = "auto",
        steps: list[int] | None = None,
        horizon: int | None = None,
        catalog: Catalog | None = None,
    ):
        """Initialise an NWP backend instance.

        Resolves every requested model key against the catalog
        **before** the parent constructor runs, because the parent
        calls :meth:`_initialize` first and `self.vars` is not yet set
        there.

        Args:
            start: Inclusive start of the cycle-date range (parsed with
                `fmt`).
            end: Inclusive end of the cycle-date range.
            variables: Mapping from model key to a list of parameter
                names, e.g. `{"gfs": ["temperature_2m"]}`.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label. Defaults to
                `"6hourly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            centre: Optional explicit centre override (reserved; the
                catalog `backend:` normally selects the centre).
            mirror: Cloud-mirror key (`"auto"` lets the centre choose).
            steps: Explicit forecast lead times in hours. Defaults to
                `[0]` (the analysis step) when neither `steps` nor
                `horizon` is given.
            horizon: Maximum forecast lead time in hours; expands to a
                step list per model cadence (resolved in `C3`).
            catalog: Optional pre-built :class:`Catalog` (tests inject
                a faked one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a model key is
                unknown, or a model declares an unknown `backend:`.
        """
        if not variables:
            raise ValueError(
                "NWP requires a non-empty `variables` mapping of "
                "{model_key: [param, ...]}."
            )
        self._centre = centre
        self._mirror = mirror
        self._steps_arg = steps
        self._horizon_arg = horizon
        self._catalog = catalog if catalog is not None else Catalog()
        self._requests: list[tuple[str, NWPModel, list[str]]] = self._resolve_models(
            variables
        )

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _resolve_models(
        self, variables: dict[str, list[str]]
    ) -> list[tuple[str, NWPModel, list[str]]]:
        """Resolve every requested model key to a catalog row + params.

        Args:
            variables: The `{model_key: [param, ...]}` request.

        Returns:
            list[tuple[str, NWPModel, list[str]]]: One `(key, model,
                params)` triple per request key, in request order.

        Raises:
            ValueError: When a key is unknown (the catalog's
                did-you-mean is surfaced), a model declares an unknown
                `backend:`, or a requested param is not in the model's
                band map.
        """
        resolved: list[tuple[str, NWPModel, list[str]]] = []
        for model_key, params in variables.items():
            model = self._catalog.get_model(model_key)
            if model.backend not in KNOWN_BACKENDS:
                raise ValueError(
                    f"model {model_key!r} declares unknown backend "
                    f"{model.backend!r}; known: {sorted(KNOWN_BACKENDS)}."
                )
            unknown = [p for p in params if p not in model.bands]
            if unknown:
                raise ValueError(
                    f"model {model_key!r} has no band(s) {unknown}; "
                    f"known params: {sorted(model.bands)}."
                )
            resolved.append((model_key, model, list(params)))
        return resolved

    def _initialize(self):
        """No-op auth hook — every MVP centre is an open bucket.

        Returns `None`; the parent binds no `self.client`.
        """
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a :class:`SpatialExtent`.

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
        """Parse the cycle-date range into a :class:`TemporalExtent`.

        For NWP the `dates` index is the requested **cycle date**
        range; the per-cycle / per-step expansion happens in
        :meth:`_search` (`C3`).

        Args:
            start: Inclusive start of the cycle-date range.
            end: Inclusive end of the cycle-date range.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        dates = pd.date_range(start_dt, end_dt, freq="D")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="D",
            dates=dates,
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def _search(self) -> list[RemoteProduct]:
        """Expand the request into one product per `(model, cycle, step)`.

        Implemented in `C3` (the cycle-grid walk). The scaffold raises
        so an early caller gets a clear pointer rather than an empty
        result.

        Raises:
            NotImplementedError: Always, until `C3` lands the walk.
        """
        raise NotImplementedError(
            "NWP._search (the cycle-grid walk) is implemented in C3."
        )

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch + crop + write one COG per product.

        Implemented in `C3` (the `open_grib → crop → write_cog`
        pipeline).

        Raises:
            NotImplementedError: Always, until `C3`.
        """
        raise NotImplementedError(
            "NWP._fetch (GRIB2 → cropped COG) is implemented in C3."
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the requested forecasts as bbox-cropped COGs.

        Args:
            progress_bar: Whether to show per-product progress.
            aggregate: Optional
                :class:`earthlens.aggregate.AggregationConfig`; reduces
                the `(cycle, step)` COG stack (`C6`).

        Returns:
            list[Path]: One cropped COG per `(cycle, step)`, or — when
                `aggregate` is set — the per-window reduced rasters.
        """
        self._show_progress = progress_bar
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(
        self, paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce the `(cycle, step)` COG stack per window (`C6`).

        Implemented in `C6`.

        Raises:
            NotImplementedError: Always, until `C6`.
        """
        raise NotImplementedError(
            "NWP.download(aggregate=...) is implemented in C6."
        )
