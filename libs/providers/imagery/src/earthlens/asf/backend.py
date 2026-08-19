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

from pathlib import Path
from typing import Any, cast

from earthlens.asf._helpers import apply_baseline_windows, wkt_from_extent
from earthlens.asf.auth import ASFAuth, ASFCredentials
from earthlens.asf.catalog import Catalog, Product
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)


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

    Note:
        ASF ships every product as a SAFE `.zip` archive (not a
        bare GeoTIFF / NetCDF), so :meth:`earthlens.earthlens.EarthLens.load`
        — which calls `pyramids` on `.tif` / `.nc` / `.cog` /
        `.zarr` extensions — returns the downloaded `.zip` path as
        a plain :class:`pathlib.Path` rather than a pyramids object.
        Use a dedicated SAR reader (`asf_search.export.read`, ISCE,
        SNAP, `sarsen`, …) to open the archive.

    Examples:
        - Inspect the class-level `OUTPUT_KIND` declaration without
          constructing an instance:
            ```python
            >>> from earthlens.asf import ASF
            >>> ASF.OUTPUT_KIND
            'raster'

            ```
        - Construct in search mode (requires bbox) — `# doctest: +SKIP`
          because a live search would need the `[asf]` extra installed:
            ```python
            >>> from earthlens.asf import ASF
            >>> backend = ASF(                          # doctest: +SKIP
            ...     start="2024-06-01", end="2024-06-15",
            ...     variables=["sentinel-1-slc"],
            ...     lat_lim=[37.0, 37.5], lon_lim=[-122.5, -122.0],
            ...     path="asf_out",
            ... )

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "ASF returns SAR products for downstream InSAR/RTC tooling; aggregate= is not supported. Post-process the downloaded SLC/RTC stack with a dedicated InSAR tool"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        reference: str | None = None,
        perpendicular_baseline: tuple[float, float] | None = None,
        temporal_baseline: tuple[int, int] | None = None,
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
                baseline window in whole days. Translated to
                `ASFSearchOptions(temporalBaselineDays="<min>,<max>")`.
                Both values are required to be ints; fractional days
                are not supported by the SDK's query encoding.
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

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        ASF issues a single query spanning the whole window (no
        per-date loop), so the resolution is always coerced to the
        sentinel `"all"` regardless of the user-supplied value (the
        `EarthLens` facade defaults `temporal_resolution="daily"`
        for every backend; ASF does not chunk by cadence, so the
        value is silently ignored rather than warned about).
        `dates` collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Ignored — ASF queries always span
                the full window in one call.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than
                `end`.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="all")

    @property
    def _mode(self) -> str:
        """`"stack"` when a reference granule was passed, else `"search"`."""
        return "stack" if self._reference is not None else "search"

    def _stack_opts(self, asf_search_module: Any) -> Any:
        """Build an `ASFSearchOptions` for `ASFProduct.stack()`.

        Empirically the SDK's `.stack()` filter fields
        (`minBaselinePerp` / `maxBaselinePerp` / `temporalBaselineDays`)
        are passed straight into the internal `search()` call that
        rebuilds the stack — adding them makes that search return
        zero products before the baseline post-processing runs,
        breaking the stack with "No products found matching stack
        parameters". The reliable contract is: hand `.stack()` an
        empty options bundle, let it return the full reference-frame
        stack, and apply the perpendicular- / temporal-baseline
        windows client-side via :func:`apply_baseline_windows`.

        Args:
            asf_search_module: The `asf_search` module.

        Returns:
            asf_search.ASFSearchOptions: An empty options object;
                the baseline filtering happens after the stack
                returns.
        """
        return asf_search_module.ASFSearchOptions()

    def _search(self) -> list[RemoteProduct]:
        """Run a geo/temporal search or build a baseline stack.

        In search mode: `geo_search(intersectsWith=<WKT>,
        platform=..., processingLevel=..., dataset=..., start=...,
        end=..., …)`. In stack mode: `granule_search([reference])`
        → `ref.stack(opts=…)`, then the perpendicular- /
        temporal-baseline windows are applied client-side via
        :func:`apply_baseline_windows` (the SDK's
        `ASFSearchOptions` baseline fields break `.stack()` and
        are not used; see :meth:`_stack_opts`).

        Returns:
            list[RemoteProduct]: One product per matching ASF
                product, in result order. `id` is
                `product.properties["sceneName"]` (with `fileID`
                as a fallback); `metadata` always carries the raw
                `ASFProduct` and `fileName`. In **stack mode**
                `metadata` also carries `perpendicularBaseline` and
                `temporalBaseline`; search-mode metadata omits
                those keys because `geo_search` results do not
                carry baseline properties (including them as
                `None` would silently mis-match downstream
                filters).

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
            # The catalog stores each row's enum *member name*
            # (`SENTINEL1`, `OPERA_S1`, `SLC`) — not the underlying
            # value the SDK actually filters on (`'SENTINEL-1'`,
            # `'OPERA-S1'`, `'SLC'`). Resolve the value at request
            # time so a constant like `PLATFORM.SENTINEL1` reaches
            # `geo_search(platform='SENTINEL-1')` correctly. Without
            # this, every search whose platform/dataset value differs
            # from its member name (most of them) silently returns
            # zero products.
            platform_value = (
                getattr(asf.PLATFORM, self._product.platform)
                if self._product.platform is not None
                else None
            )
            dataset_value = (
                getattr(asf.DATASET, self._product.dataset)
                if self._product.dataset is not None
                else None
            )
            product_type_value = getattr(asf.PRODUCT_TYPE, self._product.product_type)
            search_kwargs: dict[str, Any] = {
                "intersectsWith": wkt_from_extent(self.space),
                "processingLevel": product_type_value,
                "start": self.time.start_date.isoformat(),
                "end": self.time.end_date.isoformat(),
            }
            if platform_value is not None:
                search_kwargs["platform"] = platform_value
            else:
                search_kwargs["dataset"] = dataset_value
            if self._beam_mode is not None:
                search_kwargs["beamMode"] = self._beam_mode
            if self._flight_direction is not None:
                search_kwargs["flightDirection"] = self._flight_direction
            if self._polarization is not None:
                search_kwargs["polarization"] = self._polarization
            if self._max_results is not None:
                search_kwargs["maxResults"] = self._max_results
            products = list(asf.geo_search(**search_kwargs))

        is_stack_mode = self._mode == "stack"
        result: list[RemoteProduct] = []
        for product in products:
            metadata: dict[str, Any] = {
                "product": product,
                "fileName": product.properties.get("fileName"),
            }
            # Baseline keys are only meaningful for stacked products
            # — `ASFProduct.stack()` writes `perpendicularBaseline` /
            # `temporalBaseline` onto each result; plain `geo_search`
            # results carry neither. Including the keys (as `None`) in
            # search mode would let a downstream consumer write
            # `metadata["temporalBaseline"]` and silently get nothing.
            if is_stack_mode:
                metadata["perpendicularBaseline"] = product.properties.get(
                    "perpendicularBaseline"
                )
                metadata["temporalBaseline"] = product.properties.get(
                    "temporalBaseline"
                )
            result.append(
                RemoteProduct(
                    id=product.properties.get("sceneName")
                    or product.properties.get("fileID", "?"),
                    href=product.properties.get("url"),
                    metadata=metadata,
                )
            )
        return result

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download every product to `self.path` and return the written paths.

        Calls :meth:`ASFAuth.session` once **if any product needs to
        be fetched** (which runs the EDL login on first access) and
        reuses the same authenticated session for the whole batch.
        The download is idempotent — products whose target file
        already exists under `self.path` are skipped rather than
        re-downloaded (ASF SAR products are large; CI re-runs would
        otherwise re-fetch hundreds of megabytes). A `products` list
        with nothing missing therefore does not authenticate.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[Path]: The on-disk file paths, in `products`
                order. Includes already-present files (the path is
                still the correct on-disk artefact).

        Raises:
            ValueError: When an `asf_search.ASFProduct` carries no
                resolvable `fileName` property (defensive — should
                not happen with the current SDK, but better to fail
                loudly than crash with `TypeError` on `Path / None`).
        """
        import asf_search as asf

        out_dir = Path(self.path)
        out_dir.mkdir(parents=True, exist_ok=True)
        missing = [
            remote.id for remote in products if not remote.metadata.get("fileName")
        ]
        if missing:
            raise ValueError(
                "ASF product(s) returned without a resolvable fileName: "
                f"{missing!r}. This points at an asf_search regression "
                "or an unsupported product class; report it upstream."
            )
        targets = [out_dir / remote.metadata["fileName"] for remote in products]
        to_fetch = [
            remote for remote, target in zip(products, targets) if not target.exists()
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

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Run the search/stack + download and return the written paths.

        Args:
            progress_bar: Accepted for facade-compatibility; not
                used (the SDK manages its own per-file progress).

        Returns:
            list[Path]: The on-disk paths of every downloaded
                product, including the products that were already
                present and skipped.

        Examples:
            - Construct in search mode and reject an `aggregate=`
              argument before any network call (the check happens
              entirely client-side):
                ```python
                >>> import tempfile
                >>> from earthlens.asf import ASF
                >>> backend = ASF(
                ...     start="2024-06-01", end="2024-06-15",
                ...     variables=["sentinel-1-slc"],
                ...     lat_lim=[37.0, 37.5], lon_lim=[-122.5, -122.0],
                ...     path=tempfile.mkdtemp(),
                ... )
                >>> try:
                ...     backend.download(aggregate=object())
                ... except NotImplementedError as exc:
                ...     "InSAR" in str(exc)
                True

                ```
        """
        return cast("list[Path]", self._api())
