"""ASF InSAR backend — SAR search + baseline `stack()` via `asf_search`.

`ASF(AbstractDataSource)` answers two request shapes through one
uniform interface:

1. **Plain SAR catalog search** — geometry (bbox) + time +
   platform/processing-level → matching products
   (`asf_search.geo_search`).
2. **InSAR baseline stack** — a `reference=<granule id>` (+ optional
   `perpendicular_baseline=` / `temporal_baseline=` windows) → the
   coregistered acquisitions from that reference
   (`ASFProduct.stack()`).

The mode is decided by a single rule: `reference` set → stack mode,
else → search mode. `_initialize` validates the mode invariants
(stack mode → exactly one stackable product in `variables`; search
mode → bbox present) so a misshapen request fails before the first
network call.

`OUTPUT_KIND = "raster"`. `download()` returns the list of written
SAR product paths (SLC / BURST / RTC / GRD). `download(aggregate=…)`
is rejected with `NotImplementedError` — an SLC is complex-valued
(I/Q), not a plain bbox crop target, so the MVP retrieves products
for downstream InSAR tools rather than processing them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from earthlens.asf.auth import ASFAuth, ASFCredentials
from earthlens.asf.catalog import Catalog, Product
from earthlens.asf._helpers import apply_baseline_windows, wkt_from_extent
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


class ASF(AbstractDataSource):
    """Alaska Satellite Facility SAR backend with InSAR baseline `stack()`.

    Wraps `asf_search` so a user can pull either a geometry/time
    SAR-catalog search or an InSAR baseline stack from a reference
    granule through the standard earthlens `download()` shape.
    Search calls run anonymously; only the download step
    authenticates, via :class:`ASFAuth` (which reuses
    :class:`EarthdataAuth` — no second credential system).

    Attributes:
        OUTPUT_KIND: `"raster"` — SAR products are gridded files
            (SLC / BURST / RTC / GRD). The facade accepts
            `aggregate=` for raster backends, but this backend
            explicitly rejects it: an SLC is complex-valued and not
            a plain crop target, so the MVP returns paths for
            downstream InSAR tooling rather than processing them
            in-flight.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        reference: str | None = None,
        perpendicular_baseline: tuple[float, float] | None = None,
        temporal_baseline: tuple[float, float] | None = None,
        beam_mode: str | None = None,
        flight_direction: str | None = None,
        polarization: str | None = None,
        max_results: int | None = None,
        processes: int = 4,
        credentials: ASFCredentials | None = None,
    ) -> None:
        """Initialise an ASF backend instance.

        Args:
            start: Inclusive start of the search window, parsed with
                `fmt`. Ignored in stack mode beyond being recorded
                (the reference granule defines the time origin).
            end: Inclusive end of the search window.
            variables: Curated product keys (`["sentinel-1-slc"]`,
                `["sentinel-1-burst"]`, `["opera-rtc-s1"]`). One
                key per call; stack mode requires exactly one
                stackable key.
            lat_lim: `[lat_min, lat_max]` in degrees. Required in
                search mode; in stack mode `None` is fine (the
                reference granule's footprint defines the geometry).
            lon_lim: `[lon_min, lon_max]` in degrees. Same shape as
                `lat_lim`.
            temporal_resolution: ASF does not chunk by day/month —
                the whole `[start, end]` window is one query — so
                this is the sentinel `"all"`.
            path: Output directory for the downloaded products.
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            reference: A granule id (e.g. an S1 SLC scene name).
                Setting it switches the backend into stack mode.
                `None` (default) means search mode.
            perpendicular_baseline: `(min_m, max_m)` perpendicular
                baseline window in metres, applied to the stack.
                `None` disables the filter. Translated to
                `ASFSearchOptions(minBaselinePerp, maxBaselinePerp)`.
            temporal_baseline: `(min_days, max_days)` temporal
                baseline window in days. Translated to
                `ASFSearchOptions(temporalBaselineDays)`.
            beam_mode: Restrict to one ASF beam mode (e.g. `"IW"`
                for Sentinel-1 Interferometric Wide swath), or
                `None`.
            flight_direction: `"ASCENDING"`, `"DESCENDING"`, or
                `None` for both.
            polarization: A polarisation string (e.g. `"VV"`,
                `"VV+VH"`), or `None` for all.
            max_results: Cap on the number of products returned by
                the search. `None` is "no cap" (subject to ASF's
                server-side limit).
            processes: Worker count for the parallel ASF download.
                Forwarded to
                `ASFSearchResults.download(processes=…)`.
            credentials: Optional :class:`ASFCredentials`. `None`
                builds a default `ASFCredentials()` that defers
                to the EDL env vars / `~/.netrc`.
        """
        self._reference = reference
        self._perpendicular_baseline = perpendicular_baseline
        self._temporal_baseline = temporal_baseline
        self._beam_mode = beam_mode
        self._flight_direction = flight_direction
        self._polarization = polarization
        self._max_results = max_results
        self._processes = processes
        self._creds_arg = credentials
        self._user_lat_lim = lat_lim
        self._user_lon_lim = lon_lim

        # Resolve the catalog and validate the mode invariants up
        # front — `self.vars` is not yet bound when the parent's
        # `__init__` calls `_initialize`, and the EDL auth object
        # needs nothing from the parent. Doing this here mirrors the
        # earthdata backend, which resolves its catalog before
        # `super().__init__()` for the same reason.
        self._catalog = Catalog()
        if not variables:
            raise ValueError("ASF requires variables=[<product key>] (got empty list)")
        if len(variables) != 1:
            raise ValueError(
                "ASF accepts exactly one product per call "
                f"(got variables={variables!r})"
            )
        product_key = self._catalog.resolve(variables[0])
        self._product_key = product_key
        self._product: Product = self._catalog.get_product(product_key)
        if reference is not None:
            if not self._product.stackable:
                raise ValueError(
                    f"{product_key!r} is not InSAR-stackable; use search mode "
                    "or pick an SLC / BURST / CSLC product"
                )
        else:
            if lat_lim is None or lon_lim is None:
                raise ValueError(
                    "ASF search mode requires lat_lim and lon_lim; pass a "
                    "bbox or switch to stack mode by setting reference=<granule>"
                )

        # In stack mode the reference granule defines the area of
        # interest, so a bbox is optional. The parent's __init__
        # captures lat_lim / lon_lim into self.space if both are
        # present; we fill in a world-bbox placeholder in stack mode
        # so the parent's SpatialExtent build succeeds. `_search`
        # ignores `self.space` in stack mode.
        super().__init__(
            start=start,
            end=end,
            variables=variables,
            lat_lim=lat_lim if lat_lim is not None else [-90.0, 90.0],
            lon_lim=lon_lim if lon_lim is not None else [-180.0, 180.0],
            temporal_resolution=temporal_resolution,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> None:
        """Build the (lazy) :class:`ASFAuth`; defer the EDL login.

        `asf_search` is imported lazily inside `_search` / `_fetch` /
        `ASFAuth`, so neither this method nor the constructor pulls
        the SDK. The :class:`ASFAuth` is built but **not**
        configured here — search runs anonymously; `_fetch` calls
        `configure()` on first use. Catalog resolution and mode
        validation happen in `__init__` before `super().__init__()`
        (the parent calls `_initialize` before binding `self.vars`,
        which we need for the resolution).

        Returns:
            None: No per-instance client object.
        """
        self._auth = ASFAuth(self._creds_arg or ASFCredentials())
        return None

    def _create_grid(
        self, lat_lim: list[float], lon_lim: list[float]
    ) -> SpatialExtent:
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
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        ASF issues a single query spanning the whole window (no
        per-date loop), so the resolution is the sentinel `"all"`
        and `dates` collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Ignored beyond being recorded.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than
                `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    @property
    def _mode(self) -> str:
        """`"stack"` when a reference granule was passed, else `"search"`."""
        return "stack" if self._reference is not None else "search"

    def _stack_opts(self, asf_search_module: Any) -> Any:
        """Build the `ASFSearchOptions` for the baseline stack.

        Translates the public-API kwargs
        (`perpendicular_baseline`, `temporal_baseline`) to the SDK's
        camelCase fields (`minBaselinePerp` / `maxBaselinePerp` /
        `temporalBaselineDays`). `temporalBaselineDays` accepts a
        `"<min>,<max>"` string in the SDK; we encode the tuple
        accordingly.

        Args:
            asf_search_module: The `asf_search` module (passed in
                so callers can reuse the already-imported handle).

        Returns:
            asf_search.ASFSearchOptions: Configured options object.
        """
        kwargs: dict[str, Any] = {}
        if self._perpendicular_baseline is not None:
            kwargs["minBaselinePerp"] = float(self._perpendicular_baseline[0])
            kwargs["maxBaselinePerp"] = float(self._perpendicular_baseline[1])
        if self._temporal_baseline is not None:
            kwargs["temporalBaselineDays"] = (
                f"{int(self._temporal_baseline[0])},"
                f"{int(self._temporal_baseline[1])}"
            )
        return asf_search_module.ASFSearchOptions(**kwargs)

    def _search(self) -> list[RemoteProduct]:
        """Run a geo/temporal search or build a baseline stack.

        In search mode: `geo_search(intersectsWith=<WKT>,
        platform=..., processingLevel=..., dataset=..., start=...,
        end=..., …)`. In stack mode: `granule_search([reference])`
        → `ref.stack(opts=…)`. The baseline windows are applied via
        `ASFSearchOptions` (so the SDK enforces them server-side)
        and re-checked client-side as a defensive backstop.

        Returns:
            list[RemoteProduct]: One product per matching ASF
                product, in result order. `id` is
                `product.properties["sceneName"]`; `metadata`
                carries the raw `ASFProduct` plus `fileName` and
                the two baseline values.

        Raises:
            ValueError: When stack mode references an unknown
                granule.
        """
        import asf_search as asf

        if self._mode == "stack":
            ref_results = asf.granule_search([self._reference])
            if not ref_results:
                raise ValueError(
                    f"ASF reference granule not found: {self._reference!r}"
                )
            stack = ref_results[0].stack(opts=self._stack_opts(asf))
            products = apply_baseline_windows(
                list(stack),
                self._perpendicular_baseline,
                self._temporal_baseline,
            )
        else:
            search_kwargs: dict[str, Any] = {
                "intersectsWith": wkt_from_extent(self.space),
                "processingLevel": self._product.product_type,
                "start": self.time.start_date.isoformat(),
                "end": self.time.end_date.isoformat(),
            }
            if self._product.platform is not None:
                search_kwargs["platform"] = self._product.platform
            else:
                search_kwargs["dataset"] = self._product.dataset
            if self._beam_mode is not None:
                search_kwargs["beamMode"] = self._beam_mode
            if self._flight_direction is not None:
                search_kwargs["flightDirection"] = self._flight_direction
            if self._polarization is not None:
                search_kwargs["polarization"] = self._polarization
            if self._max_results is not None:
                search_kwargs["maxResults"] = self._max_results
            products = list(asf.geo_search(**search_kwargs))

        return [
            RemoteProduct(
                id=product.properties.get("sceneName") or product.properties.get(
                    "fileID", "?"
                ),
                href=product.properties.get("url"),
                metadata={
                    "product": product,
                    "fileName": product.properties.get("fileName"),
                    "perpendicularBaseline": product.properties.get(
                        "perpendicularBaseline"
                    ),
                    "temporalBaseline": product.properties.get("temporalBaseline"),
                },
            )
            for product in products
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download every product to `self.path` and return the written paths.

        Calls `ASFAuth.session()` once (which runs the EDL login
        on first access) and reuses the same authenticated session
        for the whole batch. The download is idempotent — products
        whose target file already exists under `self.path` are
        skipped rather than re-downloaded (ASF SAR products are
        large; CI re-runs would otherwise re-fetch hundreds of
        megabytes).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[Path]: The on-disk file paths, in `products`
                order. Includes already-present files (the path is
                still the correct on-disk artefact).
        """
        import asf_search as asf

        out_dir = Path(self.path)
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = [
            out_dir / remote.metadata["fileName"] for remote in products
        ]
        to_fetch = [
            remote
            for remote, target in zip(products, targets)
            if not target.exists()
        ]
        if to_fetch:
            session = self._auth.session()
            asf.ASFSearchResults(
                [remote.metadata["product"] for remote in to_fetch]
            ).download(
                path=str(out_dir),
                session=session,
                processes=self._processes,
            )
        return targets

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape.

        Returns:
            list[Path]: Whatever :meth:`_fetch` returned, or an
                empty list when `_search` matched nothing.
        """
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Run the search/stack + download and return the written paths.

        Args:
            progress_bar: Accepted for facade-compatibility; not
                used (the SDK manages its own per-file progress).
            aggregate: Must be `None`. ASF returns SAR products for
                downstream InSAR / RTC tooling, not gridded
                summaries — passing an :class:`AggregationConfig`
                raises `NotImplementedError`.

        Returns:
            list[Path]: The on-disk paths of every downloaded
                product, including the products that were already
                present and skipped.

        Raises:
            NotImplementedError: When `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "ASF returns SAR products for downstream InSAR/RTC tooling; "
                "aggregate= is not supported. Post-process the downloaded "
                "SLC/RTC stack with a dedicated InSAR tool."
            )
        return self._api()
