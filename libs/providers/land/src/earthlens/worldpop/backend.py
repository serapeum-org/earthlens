"""WorldPop backend — open population data hub over anonymous HTTPS.

`WorldPop` is a download-and-localise backend (`OUTPUT_KIND="mixed"`). A
request is an AOI (ISO3 / bbox / `GeoDataFrame`) + time window + a list of
WorldPop **product aliases** (`variables=["pop", ...]`, canonical or
friendly) plus `constrained` / `unadjusted` / `resolution` / `scope` /
`generation` / `year(s)` / `crs` / `api` selectors. The backend resolves
each product to a concrete REST sub-alias against the bundled catalog,
queries the WorldPop REST API for the matching per-country GeoTIFF URLs,
downloads them over anonymous HTTPS, and uses `pyramids` to mosaic + crop
(and reproject only when `crs != 4326`) — writing population GeoTIFFs and,
for demographic products (`age_structures`), per-cohort rasters plus a tidy
age/sex table.

The provider is **open, CC-BY-4.0** — no credentials (see
`earthlens.worldpop.auth`). The default `api="rest"` path needs only the
core dependencies (`requests` + `pyramids`); the optional
`api="worldpoppy"` path imports `worldpoppy` lazily and consumes only its
**file cache** (never its `xarray` return), so the package imports without
the `[worldpop]` extra.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd
import requests
from joblib import Parallel, delayed

from earthlens.base import (
    OutputKind,
    RemoteProduct,
    date_windows,
    to_datetime,
    window_labels,
)
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    TemporalExtent,
)
from earthlens.base.http import HttpClient
from earthlens.base.spatial import crop_to_aoi, resolve_aoi
from earthlens.worldpop._helpers import (
    cohort_of,
    continent_for_bbox,
    epsg_int,
    extract_geotiffs,
    iso3_for_bbox,
    load_iso3_bbox,
    normalise_iso3,
)
from earthlens.worldpop.auth import WorldPopAuth
from earthlens.worldpop.catalog import GENERATIONS, Catalog
from earthlens.worldpop.rest import (
    files_for_year,
    global_files_for_year,
    global_records,
    record_archive_files,
    rest_records,
)

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

#: Sub-directory under the output path where raw per-country GeoTIFFs land.
_RAW_DIRNAME: str = ".worldpop_raw"
#: Default parallelism for per-file HTTPS downloads.
_DOWNLOAD_JOBS: int = 4
#: Per-download HTTP timeout in seconds.
_HTTP_TIMEOUT: int = 120
#: Attempts per file download before giving up (transient errors only).
_MAX_RETRIES: int = 3
#: Base seconds for the exponential backoff between retries.
_BACKOFF_BASE: float = 1.0
#: WorldPop's GeoTIFF no-data value (verified live; merge must preserve it,
#: since 0 is a valid population count).
_WORLDPOP_NODATA: float = -99999.0

#: Allowed values for the `api=` access-path selector.
_API_MODES: frozenset[str] = frozenset({"rest", "worldpoppy"})
#: Allowed values for the `resolution=` selector.
_RESOLUTIONS: frozenset[str] = frozenset({"100m", "1km"})
#: Allowed values for the `scope=` selector.
_SCOPES: frozenset[str] = frozenset({"countries", "global"})
#: Allowed values for the `level=` selector (only `pwd` offers both).
_LEVELS: frozenset[str] = frozenset({"national", "subnational"})


class WorldPop(AbstractDataSource):
    """Download WorldPop population + demographic products, localised via pyramids.

    Attributes:
        OUTPUT_KIND: Fixed `"mixed"` — population products yield GeoTIFFs
            and demographic products (`age_structures`) additionally yield a
            tidy age/sex table, so the facade forwards `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "mixed"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: Set by `download(force=...)`; bypasses the skip-if-exists check.
    _force: bool = False

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "yearly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        aoi: str | list[str] | list[float] | object | None = None,
        constrained: bool = False,
        unadjusted: bool = True,
        resolution: str = "100m",
        scope: str = "countries",
        generation: str = "R2021",
        level: str = "national",
        year: int | None = None,
        years: list[int] | None = None,
        crs: str = "EPSG:4326",
        api: str = "rest",
        ssp: str = "SSP2",
        allow_large_archive: bool = False,
        catalog: Catalog | None = None,
    ):
        """Initialise a WorldPop backend instance.

        Resolves and statically validates every requested product +
        sub-alias selector against the catalog **before** the parent
        constructor runs (the parent calls `_initialize` first). The AOI is
        resolved to a set of ISO3 codes here too, since `_initialize`
        receives no bbox.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`);
                its year selects the first WorldPop year in range.
            end: Inclusive end of the date window.
            variables: WorldPop product keys — canonical (`"pop"`) or
                friendly aliases (`"population"`, `"age_sex"`, …).
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label only (WorldPop years are
                annual points); accepted for facade parity. Defaults to
                `"yearly"`.
            path: Output directory. Created by the parent class.
            fmt: `strptime` format for `start` / `end`.
            aoi: Explicit AOI — an ISO3 string, a list of ISO3 strings, a
                `[w, s, e, n]` bbox, or a `GeoDataFrame`. `None` (default)
                derives the ISO3 set from `lat_lim` / `lon_lim`.
            constrained: Settlement-masked *constrained* variant (`True`)
                vs *unconstrained* (`False`, default).
            unadjusted: Raw variant (`True`, default → `wpgp`) vs the
                UN-adjusted variant (`False` → `wpgpunadj`).
            resolution: `"100m"` (default) or `"1km"`.
            scope: `"countries"` (per-ISO3, default) or `"global"` (the
                global mosaic, where the product offers one).
            generation: Product generation — `"R2021"` (default, the
                classic 2000–2020 line) or a Global-2 line (`"R2025A"`, …).
            level: `"national"` (default) or `"subnational"` — only the
                population-weighted-density (`pwd`) product offers both.
            year: A single year to fetch (overrides the date window).
            years: An explicit list of years (overrides the date window
                and `year`).
            crs: Output CRS as an EPSG string / code (default
                `"EPSG:4326"` — WorldPop's native CRS, so no reproject).
            api: Access path — `"rest"` (default; direct REST + pyramids,
                no optional SDK) or `"worldpoppy"` (the optional SDK via its
                file cache).
            ssp: SSP scenario for the `future_pop` `.zip` archives
                (`"SSP1"`…`"SSP5"`; default `"SSP2"`). Ignored by other
                products.
            allow_large_archive: Opt-in required to download the multi-GB
                `future_pop` per-SSP `.zip` archives (~4 GB each). Defaults
                to `False`.
            catalog: Optional pre-built `Catalog` (tests inject a faked
                one); defaults to the bundled catalog.

        Raises:
            ValueError: When `variables` is empty, a product / alias is
                unknown, the selector tuple matches no sub-alias, or
                `resolution` / `scope` / `generation` / `api` is malformed.
            ImportError: When `api="worldpoppy"` but the `[worldpop]` extra
                is not installed.
        """
        if not variables:
            raise ValueError(
                "WorldPop requires a non-empty `variables` list of product keys, "
                'e.g. ["pop"] or ["population"].'
            )
        if api not in _API_MODES:
            raise ValueError(f"api must be one of {sorted(_API_MODES)}; got {api!r}.")
        if resolution not in _RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {sorted(_RESOLUTIONS)}; got {resolution!r}."
            )
        if scope not in _SCOPES:
            raise ValueError(f"scope must be one of {sorted(_SCOPES)}; got {scope!r}.")
        if level not in _LEVELS:
            raise ValueError(f"level must be one of {sorted(_LEVELS)}; got {level!r}.")
        if generation not in GENERATIONS:
            raise ValueError(
                f"generation must be one of {list(GENERATIONS)}; got {generation!r}."
            )
        if api == "worldpoppy":
            _require_worldpoppy()

        self._catalog = catalog if catalog is not None else Catalog()
        self._constrained = constrained
        self._unadjusted = unadjusted
        self._resolution = resolution
        self._scope = scope
        self._generation = generation
        self._level = level
        self._year_arg = year
        self._years_arg = years
        self._crs = crs
        self._output_epsg = epsg_int(crs)
        self._api_mode = api
        self._ssp = ssp
        self._allow_large_archive = allow_large_archive
        self._auth = WorldPopAuth()
        self._aggregate_cfg: AggregationConfig | None = None
        self._show_progress = True

        # Resolve + statically validate (product + selector → sub-alias) up
        # front; year validation happens once the year list is derived.
        self._products: list[str] = [self._catalog.resolve(v) for v in variables]
        self._subalias_ids: dict[str, str] = {
            product: self._catalog.pick_subalias(
                product,
                constrained=constrained,
                unadjusted=unadjusted,
                resolution=resolution,
                scope=scope,
                generation=generation,
                level=level,
            )
            for product in self._products
        }
        self._guard_unsupported()
        self._iso3s: list[str] = self._resolve_aoi(aoi, lat_lim, lon_lim)

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

        # A polygon AOI (a GeoDataFrame / geometry, not an ISO3 code or
        # bbox) masks the fetched country mosaics to the exact shape; the
        # ISO3 set above already selected which countries to fetch.
        if aoi is not None and (
            hasattr(aoi, "total_bounds") or hasattr(aoi, "__geo_interface__")
        ):
            _, _, geometry = resolve_aoi(aoi)
            if geometry is not None:
                self._attach_clip_geometry(geometry)

    def _guard_unsupported(self) -> None:
        """Gate the multi-GB `.zip` archive products behind an explicit opt-in.

        Per-country / global-mosaic GeoTIFF products and the small `.7z`
        per-continent products (`dependency_ratios`) are fetched directly.
        The `future_pop` SSP `.zip` bundles are **~4 GB each** (×5 scenarios),
        so they require `allow_large_archive=True` to avoid a multi-GB
        surprise download.

        Raises:
            NotImplementedError: If a `.zip` archive product is requested
                without `allow_large_archive=True`.
        """
        for product, subalias_id in self._subalias_ids.items():
            sub = self._catalog.subalias(product, subalias_id)
            if sub.archive == "zip" and not self._allow_large_archive:
                raise NotImplementedError(
                    f"WorldPop {product!r} ({subalias_id!r}) ships as ~4 GB per-SSP "
                    ".zip archives (×5 scenarios). Pass allow_large_archive=True "
                    "(and an ssp=, e.g. ssp='SSP2') to opt into the large download."
                )

    def _resolve_aoi(
        self,
        aoi: str | list[str] | list[float] | object | None,
        lat_lim: list[float],
        lon_lim: list[float],
    ) -> list[str]:
        """Resolve the AOI to a sorted list of ISO3 country codes.

        Args:
            aoi: An ISO3 string, a list of ISO3 strings, a `[w, s, e, n]`
                bbox, a `GeoDataFrame`, or `None` (derive from the bbox).
            lat_lim: `[lat_min, lat_max]` used when `aoi` is `None`.
            lon_lim: `[lon_min, lon_max]` used when `aoi` is `None`.

        Returns:
            list[str]: The intersecting / requested ISO3 codes, sorted.
        """
        if isinstance(aoi, str):
            return [normalise_iso3(aoi)]
        if isinstance(aoi, list) and aoi and isinstance(aoi[0], str):
            codes = cast("list[str]", aoi)
            return sorted({normalise_iso3(code) for code in codes})
        if isinstance(aoi, list) and len(aoi) == 4 and isinstance(aoi[0], (int, float)):
            bbox = [float(x) for x in aoi]
        elif aoi is not None and hasattr(aoi, "total_bounds"):  # a GeoDataFrame
            gdf: Any = aoi
            crs = getattr(gdf, "crs", None)
            if crs is not None and crs.to_epsg() != 4326:
                gdf = gdf.to_crs(4326)
            bbox = [float(x) for x in gdf.total_bounds]
        else:
            bbox = [lon_lim[0], lat_lim[0], lon_lim[1], lat_lim[1]]
        return iso3_for_bbox(bbox, load_iso3_bbox())

    def _initialize(self):
        """Configure the (no-op) auth; no network client is created.

        Returns:
            None: WorldPop is open + anonymous, so the parent binds no
                `self.client`.
        """
        self._auth.configure()
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date window into a `TemporalExtent`.

        The window's years select the WorldPop years in range; the
        per-year expansion happens in `_years`.

        Args:
            start: Inclusive start of the window.
            end: Inclusive end of the window.
            temporal_resolution: Advisory label (ignored).
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        dates = date_windows(start_dt, end_dt, "YS")
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="YS",
            dates=dates,
        )

    def _years(self) -> list[int]:
        """Return the explicit years to fetch (from `years` / `year` / window).

        Returns:
            list[int]: Sorted, de-duplicated years. `years=` wins, then
                `year=`, else every year spanned by `start`/`end`.
        """
        if self._years_arg is not None:
            return sorted({int(y) for y in self._years_arg})
        if self._year_arg is not None:
            return [int(self._year_arg)]
        return sorted({d.year for d in self.time.dates})

    def _search(self) -> list[RemoteProduct]:
        """Plan the download — one `RemoteProduct` per `(product, iso3, year, file)`.

        Queries the REST API once per `(product, iso3)` (records carry every
        year) and filters client-side to the requested years. For
        demographic products each year yields many cohort files; for plain
        population products, one.

        Returns:
            list[RemoteProduct]: One item per GeoTIFF URL to download, each
                carrying `product` / `iso3` / `year` / `demographic` /
                `subalias` metadata.
        """
        out: list[RemoteProduct] = []
        years = self._years()
        if self._api_mode == "worldpoppy":
            # The WorldPopPy SDK resolves files itself; the plan is just the
            # (product, iso3, year) cross-product (no REST query, no URLs).
            for product in self._products:
                demographic = self._catalog.get(product).demographic
                for iso3 in self._iso3s:
                    for year in years:
                        out.append(
                            RemoteProduct(
                                id=f"{product}_{iso3}_{year}",
                                href=None,
                                metadata={
                                    "product": product,
                                    "iso3": iso3,
                                    "year": year,
                                    "demographic": demographic,
                                },
                            )
                        )
            return out
        for product in self._products:
            subalias_id = self._subalias_ids[product]
            demographic = self._catalog.get(product).demographic
            if self._catalog.subalias(product, subalias_id).scope == "global":
                out.extend(self._plan_global(product, subalias_id, demographic, years))
            else:
                out.extend(
                    self._plan_countries(product, subalias_id, demographic, years)
                )
        return out

    def _plan_countries(
        self, product: str, subalias_id: str, demographic: bool, years: list[int]
    ) -> list[RemoteProduct]:
        """Plan per-country downloads for one product (records once per ISO3).

        Covariate layers are **undated** (one record, `popyear` is `None`),
        so they are planned once per ISO3 with the year read from the
        filename rather than looped over the requested years.
        """
        endpoint = self._catalog.get(product).endpoint()
        out: list[RemoteProduct] = []
        for iso3 in self._iso3s:
            records = rest_records(endpoint, subalias_id, iso3)
            undated = not any(rec.get("popyear") for rec in records)
            for year in [None] if undated else years:
                for url in files_for_year(records, year):
                    resolved_year = year if year is not None else _year_in(url)
                    out.append(
                        RemoteProduct(
                            id=f"{product}_{iso3}_{resolved_year}_{Path(url).stem}",
                            href=url,
                            metadata={
                                "product": product,
                                "iso3": iso3,
                                "year": resolved_year,
                                "subalias": subalias_id,
                                "demographic": demographic,
                            },
                        )
                    )
        return out

    def _plan_global(
        self, product: str, subalias_id: str, demographic: bool, years: list[int]
    ) -> list[RemoteProduct]:
        """Plan global-mosaic downloads for one product (no ISO3; `?id=` detail).

        A global mosaic is one whole-world GeoTIFF per year (or one per
        age/sex cohort), downloaded once and cropped to the AOI bbox by the
        localise step — there is no per-country mosaic.
        """
        out: list[RemoteProduct] = []
        for year in years:
            for url in global_files_for_year(product, subalias_id, year):
                out.append(
                    RemoteProduct(
                        id=f"{product}_global_{year}_{Path(url).stem}",
                        href=url,
                        metadata={
                            "product": product,
                            "iso3": "global",
                            "year": year,
                            "subalias": subalias_id,
                            "demographic": demographic,
                        },
                    )
                )
        return out

    def _raw_dir(self) -> Path:
        """Return (creating) the directory raw per-country downloads land in."""
        raw = self.root_dir / _RAW_DIRNAME
        raw.mkdir(parents=True, exist_ok=True)
        return raw

    def _http_get(self, url: str, dest: Path) -> Path:
        """Stream `url` to `dest`, skipping when the file already exists.

        The body is streamed to disk in blocks rather than buffered in
        memory: a WorldPop national mosaic is routinely over a gigabyte, so
        reading it whole before writing it would hold two copies at peak.
        The write is atomic — the bytes land in a sibling `.part` that is
        renamed only on success — so an interrupted download never leaves a
        truncated GeoTIFF for the next run's skip check to accept.

        Transient connection / timeout errors are retried up to
        `_MAX_RETRIES` with exponential backoff; an HTTP status error (e.g.
        404) propagates immediately without retry.

        Args:
            url: The GeoTIFF URL.
            dest: Local destination path.

        Returns:
            Path: `dest`.

        Raises:
            requests.HTTPError: On a non-2xx response (the URL is named).
            requests.ConnectionError | requests.Timeout: If every retry of a
                transient network error is exhausted.
        """
        if self._is_complete(dest, force=self._force):
            return dest
        http = HttpClient(
            retry_on_exceptions=(requests.ConnectionError, requests.Timeout),
            status_forcelist=(),
            max_retries=_MAX_RETRIES - 1,
            backoff_factor=_BACKOFF_BASE,
            max_backoff=None,
            timeout=_HTTP_TIMEOUT,
            sleep=lambda seconds: time.sleep(seconds),
        )
        # Stream to disk. A WorldPop national mosaic is routinely > 1 GB, and
        # buffering the whole body as `resp.content` before writing it holds
        # two copies at peak for no benefit.
        # No per-file bar: `_http_get` runs inside a 4-thread joblib pool and
        # `download`'s tqdm takes no `position=`, so four bars would interleave
        # on one stream. The caller already shows a per-product bar.
        return http.download(url, dest, progress=False)

    def _group_for_mosaic(
        self, products: list[RemoteProduct]
    ) -> dict[
        tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
    ]:
        """Download every product and group the files for mosaicking.

        Groups by `(product, year, cohort)` so multi-country requests merge
        correctly and `age_structures` keeps each age/sex cohort separate.

        Args:
            products: The `_search` result.

        Returns:
            A mapping `(product, year, cohort) -> [(local_path, product), …]`.
        """
        raw = self._raw_dir()
        paths = Parallel(n_jobs=_DOWNLOAD_JOBS, prefer="threads")(
            delayed(self._http_get)(rp.href, raw / Path(cast("str", rp.href)).name)
            for rp in products
        )
        groups: dict[
            tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
        ] = {}
        for path, rp in zip(paths, products):
            assert rp.href is not None  # worldpop products always carry a URL
            key = (rp.metadata["product"], rp.metadata["year"], cohort_of(rp.href))
            groups.setdefault(key, []).append((path, rp))
        return groups

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download, localise, tabularise demographics, and optionally aggregate.

        Downloads (idempotently, in parallel), mosaics + crops each
        `(product, year, cohort)` group to the AOI, writes a tidy age/sex
        table for demographic products, and — when `aggregate=` was passed
        to `download` — reduces the per-year rasters across years.

        Args:
            products: The `_search` result.

        Returns:
            list[Path]: The written GeoTIFF and table paths (the reduced
                per-window rasters replace the per-year rasters when
                `aggregate=` is set; tables are always included).
        """
        if self._api_mode == "worldpoppy":
            return self._fetch_via_worldpoppy(products)
        return self._finish(self._group_for_mosaic(products))

    def _finish(
        self,
        groups: dict[
            tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
        ],
    ) -> list[Path]:
        """Localise each group, write demographic tables, and optionally aggregate.

        Shared tail of both the REST and WorldPopPy fetch paths.

        Args:
            groups: The `(product, year, cohort) -> [(path, product), …]`
                map of downloaded / cached per-country tiles.

        Returns:
            list[Path]: The written GeoTIFF + table paths (reduced
                per-window rasters replace per-year rasters when
                `aggregate=` is set).
        """
        localised: dict[tuple[str, int, tuple[str, int] | None], Path] = {
            key: self._localise(group) for key, group in groups.items()
        }
        tables = self._write_demographic_tables(localised)
        if self._aggregate_cfg is not None:
            rasters = self._aggregate_years(localised, self._aggregate_cfg)
        else:
            rasters = list(localised.values())
        self._sweep_intermediates()
        return rasters + tables

    def _sweep_intermediates(self) -> None:
        """Best-effort removal of leftover `*_merged.tif` mosaics.

        `_localise` deletes each intermediate inline, but on Windows the GDAL
        handle may still be open at that point. Once the per-`_localise`
        datasets are out of scope, a `gc.collect()` releases the handles, so
        this final sweep clears any that survived. Failures are ignored — the
        files live in the hidden raw cache dir and are harmless.
        """
        import gc

        gc.collect()
        raw = self._raw_dir()
        # _localise writes the intermediate with a leading dot, so match both.
        leftovers = list(raw.glob("*_merged.tif")) + list(raw.glob(".*_merged.tif"))
        for leftover in leftovers:
            try:
                leftover.unlink()
            except OSError:
                pass

    def _write_demographic_tables(
        self, localised: dict[tuple[str, int, tuple[str, int] | None], Path]
    ) -> list[Path]:
        """Write a tidy age/sex table per `(product, year)` for demographic products.

        For each cohort raster of a demographic product (`age_structures`),
        the AOI population total is summed and emitted as one tidy row
        `{aoi, year, sex, age_low, population}`; the rows for a
        `(product, year)` are written to `{product}_{year}.csv` alongside
        the per-cohort GeoTIFFs.

        Args:
            localised: The `(product, year, cohort) -> output_path` map.

        Returns:
            list[Path]: The written table paths (empty if no demographic
                product was requested).
        """
        rows_by_table: dict[tuple[str, int], list[dict[str, object]]] = {}
        aoi_label = "+".join(self._iso3s) if self._iso3s else "aoi"
        for (product, year, cohort), path in localised.items():
            if cohort is None or not self._catalog.get(product).demographic:
                continue
            sex, age_low = cohort
            rows_by_table.setdefault((product, year), []).append(
                {
                    "aoi": aoi_label,
                    "year": year,
                    "sex": sex,
                    "age_low": age_low,
                    "population": _zonal_sum(path),
                }
            )
        out: list[Path] = []
        for (product, year), rows in rows_by_table.items():
            frame = pd.DataFrame(rows).sort_values(["sex", "age_low"])
            target = Path(self.path) / f"{product}_{year}.csv"
            frame.to_csv(target, index=False)
            out.append(target)
        return out

    def _aggregate_years(
        self,
        localised: dict[tuple[str, int, tuple[str, int] | None], Path],
        cfg,
    ) -> list[Path]:
        """Reduce the per-year rasters across years, bucketed by `cfg.freq`.

        Groups the localised rasters by `(product, cohort)`, buckets their
        years by the pandas offset `cfg.freq`, reduces each bucket with
        `cfg.op` (`auto` → `mean` for population), and writes one GeoTIFF
        per window.

        Args:
            localised: The `(product, year, cohort) -> output_path` map.
            cfg: An `earthlens.aggregate.AggregationConfig`.

        Returns:
            list[Path]: One reduced GeoTIFF per `(product, cohort, window)`.
        """
        from pyramids.dataset import Dataset, DatasetCollection, GeoReference

        op = "mean" if cfg.op == "auto" else cfg.op
        by_series: dict[tuple[str, tuple[str, int] | None], dict[int, Path]] = {}
        for (product, year, cohort), path in localised.items():
            by_series.setdefault((product, cohort), {})[year] = path

        out: list[Path] = []
        for (product, cohort), year_paths in by_series.items():
            years = sorted(year_paths)
            # Label each year by its `cfg.freq` window start (as %Y%m%d), then
            # let DatasetCollection.groupby reduce the co-registered year
            # rasters per window — the same COG-stack path stac / nwp use. This
            # replaces the hand-rolled np.stack + local NaN-aware reducer table;
            # DatasetCollection honours each raster's no-data under skipna.
            dates = pd.to_datetime([f"{y}-01-01" for y in years])
            files = [str(year_paths[y]) for y in years]
            labels = window_labels(dates, cfg.freq)
            collection = DatasetCollection.from_files(files)
            reduced = getattr(collection.groupby(labels), op)(skipna=cfg.skipna)
            template = Dataset.read_file(files[0])
            geo, epsg = template.geotransform, template.epsg
            tag = f"_{cohort[0]}_{cohort[1]}" if cohort else ""
            for label, array in reduced.items():
                target = Path(self.path) / f"{product}{tag}_{cfg.freq}_{label}_{op}.tif"
                Dataset.from_array(
                    arr=array, geo_ref=GeoReference(geo=geo, epsg=epsg)
                ).to_file(str(target))
                out.append(target)
        return out

    def _fetch_via_worldpoppy(self, products: list[RemoteProduct]) -> list[Path]:
        """Fetch via the optional WorldPopPy SDK — through its file cache only.

        Calls `wp_raster(..., download_dry_run=True)` to populate the SDK's
        on-disk cache, **discards the returned `xarray.DataArray`** (CLAUDE.md
        forbids importing xarray in `src/`), then reads the cached GeoTIFFs
        from `get_cache_dir()` and runs them through the same localise /
        table / aggregate tail as the REST path.

        Args:
            products: The `_search` plan (one item per product / iso3 /
                year); used to bound which cached files are consumed.

        Returns:
            list[Path]: The written GeoTIFF + table paths.

        Raises:
            ValueError: If a requested product has no `worldpoppy_id`.
        """
        from worldpoppy import get_cache_dir, wp_raster

        years = self._years()
        cache = Path(get_cache_dir())
        groups: dict[
            tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
        ] = {}
        for product in self._products:
            wp_id = self._catalog.get(product).worldpoppy_id
            if not wp_id:
                raise ValueError(
                    f"{product!r} has no worldpoppy_id mapping; use api='rest'."
                )
            demographic = self._catalog.get(product).demographic
            # Snapshot the cache around each call so the files this product
            # produced are attributed to it by provenance, not by filename
            # convention. If the product was already fully cached (no new
            # files), fall back to demographic-vs-cohort matching across the
            # cache for this product.
            before = set(cache.rglob("*.tif"))
            wp_raster(
                product_name=wp_id,
                aoi=self._iso3s,
                years=years,
                download_dry_run=True,  # the returned xarray.DataArray is discarded
            )
            produced = set(cache.rglob("*.tif")) - before
            if not produced:
                produced = set(cache.rglob("*.tif"))
            for tif in sorted(produced):
                match = re.search(r"_(\d{4})\.tif$", tif.name)
                if match is None:
                    continue
                year = int(match.group(1))
                iso3 = tif.name[:3].upper()
                cohort = cohort_of(tif.name)
                if iso3 not in self._iso3s or year not in years:
                    continue
                if demographic != (cohort is not None):
                    continue
                rp = RemoteProduct(
                    id=f"{product}_{iso3}_{year}_{tif.stem}",
                    href=str(tif),
                    metadata={
                        "product": product,
                        "iso3": iso3,
                        "year": year,
                        "demographic": demographic,
                    },
                )
                groups.setdefault((product, year, cohort), []).append((tif, rp))
        return self._finish(groups)

    def _localise(self, group: list[tuple[Path, RemoteProduct]]) -> Path:
        """Mosaic the per-country GeoTIFFs of one group + crop to the AOI.

        WorldPop is WGS84 (EPSG:4326) natively, so no reproject happens
        unless `crs != 4326`. The crop bbox is always WGS84 regardless of
        the output CRS (pyramids reprojects the bbox).

        Args:
            group: `[(local_path, product), …]` for one `(product, year,
                cohort)` — the per-country tiles to merge.

        Returns:
            Path: The written AOI-cropped GeoTIFF under `self.path`.
        """
        from pyramids.dataset import Dataset
        from pyramids.dataset.merge import merge_rasters

        tifs = [str(path) for path, _ in group]
        rp = group[0][1]
        assert rp.href is not None  # worldpop products always carry a URL
        product = rp.metadata["product"]
        year = rp.metadata["year"]
        dst_crs = self._output_epsg if self._output_epsg != 4326 else None

        work = self._raw_dir() / f".{product}_{year}_{Path(rp.href).stem}_merged.tif"
        merge_rasters(
            tifs,
            str(work),
            method="last",
            dst_crs=dst_crs,
            no_data_value=_WORLDPOP_NODATA,
        )
        dataset = Dataset.read_file(str(work))
        cropped = crop_to_aoi(
            dataset,
            self.space,
            bbox=[
                self.space.west,
                self.space.south,
                self.space.east,
                self.space.north,
            ],
            touch=True,
        )
        cohort = cohort_of(rp.href)
        tag = f"_{cohort[0]}_{cohort[1]}" if cohort else ""
        target = Path(self.path) / f"{product}_{year}{tag}_{self._resolution}.tif"
        cropped.to_file(str(target))
        # Best-effort cleanup of the intermediate mosaic; on Windows the GDAL
        # handle may still hold it open, in which case it stays in the raw
        # cache dir (harmless — a distinct name per group).
        try:
            work.unlink(missing_ok=True)
        except OSError:
            pass
        return target

    def _archive_products(self) -> list[str]:
        """Return the requested products whose sub-alias is archive-distributed."""
        return [
            product
            for product in self._products
            if self._catalog.subalias(product, self._subalias_ids[product]).archive
        ]

    def _dispatch(self) -> list[Path]:
        """Route the request to the archive path or the GeoTIFF search/fetch.

        Archive products (`.7z` / `.zip`) and plain GeoTIFF products cannot
        be combined in one request — they take different fetch paths.

        Raises:
            ValueError: If the request mixes archive and GeoTIFF products.
        """
        archive = self._archive_products()
        if archive and len(archive) != len(self._products):
            raise ValueError(
                "WorldPop cannot mix archive products "
                f"({archive}) with GeoTIFF products in one request; fetch them "
                "separately."
            )
        if archive:
            return self._fetch_archive()
        return self._api_via_search_fetch()

    def _api(self) -> list[Path]:
        """Dispatch the request (archive path or GeoTIFF search/fetch)."""
        return self._dispatch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs (+ tables).

        Args:
            progress_bar: Whether per-download progress is shown.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-year raster stack across years. It reduces
                the **rasters** only — for demographic products the per-cohort
                age/sex tables are still written per year (the table column is
                not aggregated). Ignored by the archive products.
            force: Re-fetch every raw file even when a complete one already
                exists, bypassing the skip-if-exists check. Defaults to
                `False`.

        Returns:
            list[Path]: The written GeoTIFF / table paths.
        """
        self._show_progress = progress_bar
        self._force = force
        self._aggregate_cfg = aggregate
        return self._dispatch()

    def _fetch_archive(self) -> list[Path]:
        """Fetch archive products (`.7z` / `.zip`): download, extract, crop.

        `dependency_ratios` ships one small `.7z` per continent (the AOI's
        continent is resolved); `future_pop` ships one ~4 GB `.zip` per SSP
        scenario (`ssp=`). Each archive's GeoTIFF members are extracted and
        cropped to the AOI bbox.

        Returns:
            list[Path]: One cropped GeoTIFF per extracted archive member
                (year-filtered for the per-year `.zip` products).
        """
        out: list[Path] = []
        bbox = [self.space.west, self.space.south, self.space.east, self.space.north]
        for product in self._products:
            subalias_id = self._subalias_ids[product]
            fmt = self._catalog.subalias(product, subalias_id).archive
            for url in self._archive_urls(product, subalias_id, fmt, bbox):
                local = self._http_get(url, self._raw_dir() / Path(url).name)
                extract_dir = self._raw_dir() / f".{product}_{Path(url).stem}_extract"
                members = extract_geotiffs(local, fmt, extract_dir)
                for tif in self._select_archive_members(members, fmt):
                    out.append(self._crop_archive_member(tif, product, bbox))
        return out

    def _archive_urls(
        self, product: str, subalias_id: str, fmt: str, bbox: list[float]
    ) -> list[str]:
        """Resolve the archive download URL(s) for one archive product.

        `.7z` (`dependency_ratios`) is per-continent — the AOI's continent
        selects the record; `.zip` (`future_pop`) is per-SSP — `self._ssp`
        selects the archive.

        Raises:
            ValueError: If no record / archive matches the continent or SSP.
        """
        records = global_records(product, subalias_id)
        if fmt == "7z":
            continent = continent_for_bbox(bbox)
            record = next(
                (r for r in records if continent.lower() in r.get("title", "").lower()),
                None,
            )
            if record is None:
                titles = [r.get("title") for r in records]
                raise ValueError(
                    f"WorldPop {product!r} has no {continent!r} archive; "
                    f"available: {titles}."
                )
            return record_archive_files(product, subalias_id, record["id"], "7z")
        # zip (future_pop): a single record whose files are the per-SSP archives.
        urls = record_archive_files(product, subalias_id, records[0]["id"], "zip")
        wanted = [u for u in urls if self._ssp.lower() in Path(u).name.lower()]
        if not wanted:
            names = [Path(u).name for u in urls]
            raise ValueError(
                f"WorldPop {product!r} has no {self._ssp!r} archive; available: {names}."
            )
        return wanted

    def _select_archive_members(self, members: list[Path], fmt: str) -> list[Path]:
        """Filter extracted GeoTIFFs — by requested year for the per-year `.zip`.

        The `.7z` continent products are a single year (all members kept); the
        `.zip` SSP projections are per-year, so keep only members whose
        filename carries one of the requested years.
        """
        if fmt != "zip":
            return members
        wanted_years = {str(y) for y in self._years()}
        selected = [
            m for m in members if set(re.findall(r"(\d{4})", m.stem)) & wanted_years
        ]
        return selected or members

    def _crop_archive_member(self, tif: Path, product: str, bbox: list[float]) -> Path:
        """Crop one extracted GeoTIFF to the AOI bbox (reproject if `crs != 4326`)."""
        from pyramids.dataset import Dataset

        dataset = Dataset.read_file(str(tif))
        cropped = crop_to_aoi(dataset, self.space, bbox=bbox, touch=True)
        if self._output_epsg != 4326:
            cropped = cropped.to_crs(self._output_epsg)
        target = Path(self.path) / f"{product}_{tif.stem}_{self._resolution}.tif"
        cropped.to_file(str(target))
        return target


def _masked_array(dataset) -> np.ndarray:
    """Return a dataset's first band as a float array with no-data → NaN.

    Args:
        dataset: An open `pyramids.dataset.Dataset`.

    Returns:
        np.ndarray: The 2-D float array, no-data cells set to `NaN`.
    """
    arr = np.asarray(dataset.read_array(), dtype="float64")
    if arr.ndim == 3:
        arr = arr[0]
    nodata = dataset.no_data_value
    nodata = nodata[0] if isinstance(nodata, (tuple, list)) else nodata
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    return arr


def _zonal_sum(path: Path) -> float:
    """Return the NaN-aware sum of a single-band raster (no-data excluded).

    Args:
        path: A localised GeoTIFF path.

    Returns:
        float: The total of all valid cells (e.g. AOI population for a
            cohort raster).
    """
    from pyramids.dataset import Dataset

    return float(np.nansum(_masked_array(Dataset.read_file(str(path)))))


def _year_in(name: str) -> int:
    """Return the last 4-digit year in a filename / URL, or 0 if none.

    Args:
        name: A filename or URL (e.g. `ken_viirs_100m_2012.tif`).

    Returns:
        int: The last 4-digit token (a year), or 0 when none is present.
    """
    years = re.findall(r"(19|20)\d{2}", name.rsplit("/", 1)[-1])
    return (
        int(re.findall(r"(?:19|20)\d{2}", name.rsplit("/", 1)[-1])[-1]) if years else 0
    )


def _require_worldpoppy() -> None:
    """Import-check the optional `worldpoppy` SDK, with a friendly error.

    Raises:
        ImportError: If `worldpoppy` is not installed, naming the
            `earthlens[worldpop]` extra.
    """
    try:
        import worldpoppy  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "api='worldpoppy' needs the optional WorldPopPy SDK. "
            "Install it with: pip install earthlens[worldpop]"
        ) from exc
