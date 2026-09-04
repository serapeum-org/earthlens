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
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    crop_to_aoi,
    date_windows,
    to_datetime,
)
from earthlens.nwp._helpers import (
    cog_name,
    enumerate_cycles,
    parse_cog_valid_time,
    window_labels,
)
from earthlens.nwp._warnings import RetentionWarning
from earthlens.nwp.catalog import KNOWN_BACKENDS, Catalog, NWPModel
from earthlens.nwp.centres import resolve_centre

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.nwp.centres.base import _NWPCentre

#: The download modes `NWP` accepts. `"subset"` fetches only the
#: requested bands (`.idx` byte-range where the model has one, else the
#: whole field); `"whole"` forces a full-file download even for
#: `.idx`-capable models. `"zarr"` is deliberately excluded — no NWP
#: catalog row carries a `zarr_url` — and is rejected with a clear error.
_VALID_MODES: frozenset[str] = frozenset({"subset", "whole"})


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

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "6hourly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        mirror: str = "auto",
        steps: list[int] | None = None,
        horizon: int | None = None,
        members: list[str] | None = None,
        mode: str = "subset",
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
            temporal_resolution: Advisory label only — **ignored** by
                NWP. The real cadence is per-model (`cycles_utc` for
                cycles, `step_cadence_h` for steps), so this argument
                does not affect the request. Accepted for parity with
                the other backends and the facade (whose default is
                `"daily"`); defaults to `"6hourly"` here.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            mirror: Cloud-mirror key (`"auto"` lets the centre choose).
            steps: Explicit forecast lead times in hours. Defaults to
                `[0]` (the analysis step) when neither `steps` nor
                `horizon` is given.
            horizon: Maximum forecast lead time in hours; expands to a
                step list per model cadence (resolved in `C3`).
            members: Ensemble member ids to fetch (e.g. GEFS `["mean",
                "1", "2"]`, ENS `["control", "10"]`). Defaults to the
                model's first listed member when omitted; ignored for
                deterministic models. One COG is written per
                `(cycle, step, member)`.
            mode: How much of each GRIB2 to download — `"subset"` (the
                default) fetches only the requested bands via the
                `.idx` byte-range index where the model has one
                (`idx: true`), else the whole field; `"whole"` forces
                a full-file download even for `.idx`-capable models,
                then crops. `"whole"` only changes behaviour for the
                NOAA / Herbie centre — the other centres are already
                whole-per-variable, so `mode` is a no-op there.
                `"zarr"` is rejected (no `nwp` catalog row carries a
                `zarr_url`).
            catalog: Optional pre-built :class:`Catalog` (tests inject
                a faked one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, `mode` is not
                `"subset"` / `"whole"`, a model key is unknown, or a
                model declares an unknown `backend:`.
        """
        if not variables:
            raise ValueError(
                "NWP requires a non-empty `variables` mapping of "
                "{model_key: [param, ...]}."
            )
        if mode not in _VALID_MODES:
            if mode == "zarr":
                raise ValueError(
                    "mode='zarr' is not supported: no NWP catalog row carries a "
                    "`zarr_url`, and Zarr sources (NWM, hrrrzarr) are separate "
                    "backends. Use mode='subset' (default) or mode='whole'."
                )
            raise ValueError(
                f"mode must be one of {sorted(_VALID_MODES)}; got {mode!r}."
            )
        self._mode = mode
        self._mirror = mirror
        #: Per-batch context `_fetch` sets up for `_fetch_one`: the crop box
        #: and the two pyramids entry points, imported once per download.
        self._crop_bbox: list[float] = []
        self._open_grib: Any = None
        self._write_cog: Any = None
        self._steps_arg = steps
        self._horizon_arg = horizon
        self._members_arg = members
        self._catalog = catalog if catalog is not None else Catalog()
        self._requests: list[tuple[str, NWPModel, list[str]]] = self._resolve_models(
            variables
        )
        # Centre instances are cached per backend so a multi-cycle fetch
        # reuses one Herbie / ecmwf-opendata adapter rather than rebuilding it.
        self._centres: dict[str, _NWPCentre] = {}

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
        self._warn_retention()

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

    def _warn_retention(self) -> None:
        """Emit `RetentionWarning` for any request older than a model's window.

        Iterates `self._requests` once per construction; a model row with
        `retention_days = None` is treated as archival and is silent. The
        cutoff is computed in naive UTC against `self.time.start_date` so
        the comparison matches the catalog's `start` / `end` parsing.

        The warning message renders both `start` and `cutoff` to the hour
        (`timespec='hours'`) rather than to the day, so a same-day
        sub-window failure reads as "older than 2026-06-16T14:00" rather
        than the ambiguous "older than 2026-06-16".

        Stacklevel attribution: 3 frames is correct for a direct
        `NWP(...)` call (1=this method, 2=`__init__`, 3=caller). The
        :class:`~earthlens.core.EarthLens` facade adds one frame, so a
        facade-route warning is attributed to `earthlens.py`; users
        wanting a precise call-site should filter on
        `category=RetentionWarning` rather than module.
        """
        cutoff_base = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        start = self.time.start_date
        for model_key, model, _params in self._requests:
            window = model.retention_days
            if window is None:
                continue
            cutoff = cutoff_base - dt.timedelta(days=window)
            if start < cutoff:
                warnings.warn(
                    f"{model_key!r} retains ~{window} day(s); requested "
                    f"start {start.isoformat(timespec='hours')} is older than "
                    f"the retention cutoff at {cutoff.isoformat(timespec='hours')} "
                    "UTC — expect empty results.",
                    RetentionWarning,
                    stacklevel=3,
                )

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
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        dates = date_windows(start_dt, end_dt, "D")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="D",
            dates=dates,
        )

    def _steps_for(self, model: NWPModel) -> list[int]:
        """Resolve the forecast lead times to fetch for one model (`G1`).

        Precedence: an explicit `steps=` list wins; otherwise `horizon=`
        expands from `0` to the horizon on the model's `step_cadence_h`
        (e.g. every 3 h for GFS), so it does not request hourly steps a
        coarse model never publishes (`M2`); otherwise the default is
        `[0]` (the analysis step), keeping the MVP bounded. A step the
        model still doesn't carry is handled by the `errors` fetch
        policy (`M1`), not here.

        Args:
            model: The resolved catalog row (bounds the request via
                `horizon_h`, and sets the `horizon=` cadence via
                `step_cadence_h`).

        Returns:
            list[int]: Sorted, de-duplicated lead times in hours.

        Raises:
            ValueError: When a requested step exceeds the model's
                `horizon_h`.
        """
        if self._steps_arg is not None:
            steps = sorted({int(s) for s in self._steps_arg})
        elif self._horizon_arg is not None:
            steps = list(
                range(0, int(self._horizon_arg) + 1, max(model.step_cadence_h, 1))
            )
        else:
            steps = [0]
        too_far = [s for s in steps if s > model.horizon_h]
        if too_far:
            raise ValueError(
                f"step(s) {too_far} exceed the {model.horizon_h} h horizon "
                f"of the requested model."
            )
        return steps

    def _members_for(self, model: NWPModel) -> list[str | None]:
        """Resolve the ensemble members to fetch for one model.

        A deterministic model (no `members`) has a single `[None]` axis.
        For an ensemble model, an explicit `members=` list wins (each
        validated against the model's members); otherwise the default is
        the model's first listed member (e.g. the mean/control), keeping
        a plain ensemble request bounded.

        Args:
            model: The resolved catalog row.

        Returns:
            list[str | None]: The member ids to fetch (`[None]` for a
                deterministic model).

        Raises:
            ValueError: When a requested member is not one of the
                model's members.
        """
        if not model.members:
            return [None]
        if self._members_arg is not None:
            unknown = [m for m in self._members_arg if m not in model.members]
            if unknown:
                raise ValueError(
                    f"members {unknown} are not in the model's members {model.members}."
                )
            return list(self._members_arg)
        return [model.members[0]]

    def _centre_for(self, backend: str) -> _NWPCentre:
        """Return the cached :class:`_NWPCentre` for a catalog `backend:`.

        Args:
            backend: The model's `backend:` value (e.g. `"herbie"`).

        Returns:
            _NWPCentre: A centre bound to the output directory; one
                instance per backend, reused across cycles.
        """
        if backend not in self._centres:
            self._centres[backend] = resolve_centre(backend, self.root_dir)
        # Reflect the current download(progress_bar=) onto the centre so a
        # progress-aware SDK (Herbie) can honour it (L4).
        self._centres[backend].show_progress = getattr(self, "_show_progress", True)
        # Give server-side-subsetting centres (the Météo-France WCS API) the
        # request bbox; others ignore it (the backend crops their full field).
        self._centres[backend].bbox = (
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        )
        return self._centres[backend]

    def _search(self) -> list[RemoteProduct]:
        """Expand the request into one product per `(model, cycle, step)`.

        Walks the cycle grid (`G1`): for each requested model, every
        cycle in the `start`/`end` date range (per the model's
        `cycles_utc`) crossed with every requested forecast step.

        Returns:
            list[RemoteProduct]: One product per `(model, cycle, step)`,
                each carrying the model row, cycle, step, and requested
                params in `metadata` so `_fetch` needs no re-query.
        """
        products: list[RemoteProduct] = []
        for model_key, model, params in self._requests:
            cycles = enumerate_cycles(
                self.time.start_date, self.time.end_date, model.cycles_utc
            )
            for cycle in cycles:
                for step in self._steps_for(model):
                    for member in self._members_for(model):
                        suffix = f".m{member}" if member is not None else ""
                        products.append(
                            RemoteProduct(
                                id=f"{model_key}.{cycle:%Y%m%d%H}.f{step:03d}{suffix}",
                                metadata={
                                    "model_key": model_key,
                                    "model": model,
                                    "cycle": cycle,
                                    "step": step,
                                    "member": member,
                                    "params": params,
                                },
                            )
                        )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch each product's GRIB2, crop to the bbox, write a COG (`G4`).

        Per product: the matching centre downloads the variable-subset
        GRIB2 (the >99 % bandwidth win — Herbie `.idx` or DWD's
        per-variable files), then `pyramids.grib.open_grib` reads it,
        the result is cropped to the request bbox, and written as a COG.
        Global models on a 0–360° longitude grid are normalised to
        −180..180 first when the bbox reaches into negative longitudes,
        so an Americas crop lands correctly.

        A single `(cycle, step)` can legitimately be unavailable — the
        latest cycle may not be published yet, or a model may not carry
        a step on every cycle (`M2`/`M4`). The `errors` policy (set by
        :meth:`download`, default `"warn"`) governs that: `"warn"` logs
        the miss and keeps the COGs already produced, `"skip"` drops it
        silently, and `"raise"` aborts the whole fetch.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One cropped COG path per successfully fetched
                product, in order. Shorter than `products` when some
                were skipped under `errors` in `{"warn", "skip"}`.
        """
        from earthlens.nwp._eccodes import ensure_eccodes

        ensure_eccodes()

        from pyramids.dataset.cog import write_cog
        from pyramids.grib import open_grib

        # The crop box and the two pyramids entry points are the same for
        # every item, so they ride on the instance for the batch instead of
        # widening `_fetch_one` past the base hook's one-argument shape.
        self._crop_bbox = [
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        ]
        self._open_grib = open_grib
        self._write_cog = write_cog
        try:
            out, _failed = self._run_items(
                products,
                self._fetch_one,
                errors=getattr(self, "_errors", "warn"),
                label="forecast step",
                describe=lambda product: str(product.id),
            )
        finally:
            # Clear the batch context so a later stray `_fetch_one` fails
            # loudly instead of silently reusing the previous download's crop
            # box and pyramids handles.
            self._crop_bbox = []
            self._open_grib = None
            self._write_cog = None
        return out

    def _fetch_one(self, product: RemoteProduct) -> Path:
        """Fetch + crop + write the COG for one product (no error handling).

        Reads the batch context :meth:`_fetch` set up — the crop box and the
        two pyramids entry points it imported once — from the instance.

        Args:
            product: One product from :meth:`_search`.

        Returns:
            pathlib.Path: The written COG path.
        """
        if not self._crop_bbox or self._open_grib is None or self._write_cog is None:
            raise RuntimeError(
                "NWP._fetch_one was called outside a download: the per-batch "
                "crop box and pyramids handles are only set up by _fetch(). "
                "Checked before the download so a stray call costs nothing."
            )
        meta = product.metadata
        centre = self._centre_for(meta["model"].backend)
        grib_path = centre.fetch_one(
            meta["model"],
            meta["cycle"],
            meta["step"],
            meta["params"],
            self._mirror,
            meta.get("member"),
            whole=self._mode == "whole",
        )
        dataset = self._open_grib(str(grib_path))
        dataset = self._normalise_longitude(dataset)
        # touch=False crops to the bbox *extent*; touch=True takes pyramids'
        # cutline path, which masks the field but keeps the full grid extent
        # (and historically crashed on the GRIB driver's EPSG:9122 CRS — fixed
        # in pyramids 0.24.1, pyramids#403 / PY-1). We want the bbox window.
        cropped = crop_to_aoi(dataset, self.space, bbox=self._crop_bbox, touch=False)
        target = self.root_dir / cog_name(
            meta["model_key"], meta["cycle"], meta["step"], meta.get("member")
        )
        self._write_cog(cropped, str(target))
        return target

    def _normalise_longitude(self, dataset):
        """Shift a 0–360° global grid to −180..180 when the bbox needs it.

        `pyramids` `wrap_longitude` only applies to a whole-globe
        0–360 raster (it raises otherwise). A regional model (HRRR) or a
        bbox entirely in the eastern hemisphere needs no shift, so this
        is a no-op unless the request bbox reaches a negative longitude.

        This handles the 0–360 ↔ −180..180 convention only. A bbox that
        *crosses* the antimeridian would need `longitude_min >
        longitude_max`, which the `SpatialExtent` value object forbids (it
        requires `longitude_min <= longitude_max`). pyramids' `crop` itself
        gained antimeridian-crossing support in 0.41, so the residual
        limitation is earthlens's own `SpatialExtent`, not the GIS backend;
        until that is relaxed, split such an AOI into two requests.

        Args:
            dataset: The freshly opened GRIB2 `Dataset`.

        Returns:
            The same `Dataset`, or a longitude-shifted copy.
        """
        if self.space.west >= 0:
            return dataset
        try:
            return dataset.wrap_longitude()
        except ValueError:
            # Not a 0–360 global raster (e.g. a regional model already in
            # −180..180); the bbox is already in the dataset's CRS.
            return dataset

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        errors: str = "warn",
    ) -> list[Path]:
        """Fetch the requested forecasts as bbox-cropped COGs.

        Args:
            progress_bar: Whether the centres show per-download progress
                (threaded into Herbie's `verbose`).
            aggregate: Optional
                :class:`earthlens.aggregate.AggregationConfig`; reduces
                the `(cycle, step)` COG stack (`C6`).
            errors: How to treat a `(cycle, step)` that fails to fetch or
                crop (an unpublished cycle, a step the model does not
                carry):

                * `"warn"` (default) — log the miss and return the COGs
                  that did succeed.
                * `"skip"` — drop the miss silently.
                * `"raise"` — abort the whole download on the first miss.

        Returns:
            list[Path]: One cropped COG per successfully fetched
                `(cycle, step)`, or — when `aggregate` is set — the
                per-window reduced rasters.

        Raises:
            ValueError: If `errors` is not one of
                `{"raise", "warn", "skip"}`.
        """
        self._show_progress = progress_bar
        # Shared validator: accepts the canonical raise/warn/ignore and keeps
        # nwp's original "skip" working as an alias for "ignore".
        self._errors = self.check_errors_policy(errors)
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(self, paths: list[Path], config: AggregationConfig) -> list[Path]:
        """Reduce the `(cycle, step)` COG stack into per-window COGs (`C6`).

        Labels each COG by the window its **valid time** (`cycle + step`)
        falls in, then reduces the co-registered stack with `config.op`
        via `DatasetCollection.groupby(labels).<op>()` — the COG analog
        of the NetCDF reducer the observation-time backends use. One COG
        is written per window.

        Aggregation requires a **single model**: different models have
        different native grids and cannot be co-registered into one
        stack, so a multi-model request is rejected here rather than
        silently mixing grids.

        Accumulated bands (the `*_acc` convention, e.g.
        `precipitation_acc`) are reduced like any other, but a warning is
        logged because summing/averaging an accumulation across steps
        mixes its step-dependent windows and can mislead (`M3`).

        Args:
            paths: The per-`(cycle, step)` COGs from :meth:`_fetch`.
            config: The aggregation request (`freq` window, `op`
                reducer, `out_dir`, `skipna`).

        Returns:
            list[Path]: The per-window reduced COG paths.

        Raises:
            ValueError: When the request names more than one model.
        """
        if not paths:
            return []
        model_keys = {key for key, _, _ in self._requests}
        if len(model_keys) > 1:
            raise ValueError(
                "aggregate= over an NWP request needs a single model; "
                f"got {sorted(model_keys)}. Different models have different "
                "native grids and cannot be co-registered into one stack — "
                "issue one request per model."
            )
        # The only model row that is not a regular lat/lon raster is ICON
        # global on its native icosahedral grid (DWD `icon_global_icosahedral_…`
        # URL pattern). The shared COG stack reducer assumes co-registered
        # rasters, so refuse the aggregation explicitly here rather than letting
        # pyramids silently mis-grid an unstructured layout (the C4 / M12
        # icosahedral guard).
        only_key, only_model, _ = self._requests[0]
        # `grid_kind` is the declarative source of truth for whether a row
        # is co-registerable into a DatasetCollection stack. The catalog
        # tags every icosahedral DWD ICON row (icon-global, icon-d2,
        # icon-eps, icon-eu-eps, icon-d2-eps); every other row defaults to
        # `"regular-latlon"`.
        if only_model.grid_kind == "icosahedral":
            raise NotImplementedError(
                f"NWP aggregate: {only_key!r} is on an icosahedral grid "
                "(not a regular lat/lon raster); aggregation is not "
                "supported. Request a griddable model (ICON-EU, or a "
                "regridded global feed) instead."
            )
        from pyramids.dataset import Dataset, DatasetCollection, GeoReference
        from pyramids.dataset.cog import write_cog

        op = "mean" if config.op == "auto" else config.op
        # Accumulated fields (precipitation_acc / APCP / tp) carry a running
        # total over a step-dependent window; reducing them across steps by
        # valid time mixes accumulation intervals and can mislead. Warn rather
        # than silently produce wrong totals (M3) — de-accumulation is a future
        # enhancement.
        accumulated = [p for p in self._requests[0][2] if p.endswith("_acc")]
        if accumulated:
            logger.warning(
                f"NWP aggregate: {accumulated} are accumulated field(s); "
                f"reducing them by valid time with op={op!r} mixes accumulation "
                "windows and may give misleading totals. Prefer the per-(cycle, "
                "step) COGs, or de-accumulate before aggregating."
            )
        out_dir = (
            Path(config.out_dir) if config.out_dir is not None else Path(self.root_dir)
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        dated = sorted((parse_cog_valid_time(p), str(p)) for p in paths)
        times = [t for t, _ in dated]
        files = [f for _, f in dated]
        labels = window_labels(times, config.freq)
        collection = DatasetCollection.from_files(files)
        reduced = getattr(collection.groupby(labels), op)(skipna=config.skipna)
        reference = Dataset.read_file(files[0])
        geo, epsg = reference.geotransform, reference.epsg
        model_key = model_keys.pop()
        written: list[Path] = []
        for label, array in reduced.items():
            target = out_dir / f"{model_key}_{op}_{config.freq}_{label}.tif"
            write_cog(
                Dataset.from_array(arr=array, geo_ref=GeoReference(geo=geo, epsg=epsg)),
                str(target),
            )
            written.append(target)
        return written
