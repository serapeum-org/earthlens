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

import datetime as dt
import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from joblib import Parallel, delayed

from earthlens.base import OutputKind, RemoteProduct
from earthlens.base.abstractdatasource import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.worldpop._helpers import (
    cohort_of,
    epsg_int,
    iso3_for_bbox,
    load_iso3_bbox,
    normalise_iso3,
)
from earthlens.worldpop.auth import WorldPopAuth
from earthlens.worldpop.catalog import GENERATIONS, Catalog
from earthlens.worldpop.rest import files_for_year, rest_records

#: Sub-directory under the output path where raw per-country GeoTIFFs land.
_RAW_DIRNAME: str = ".worldpop_raw"
#: Default parallelism for per-file HTTPS downloads.
_DOWNLOAD_JOBS: int = 4
#: Per-download HTTP timeout in seconds.
_HTTP_TIMEOUT: int = 120
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

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "yearly",
        path: Path | str = "",
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
        self._auth = WorldPopAuth()
        self._aggregate_cfg = None
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
            return sorted({normalise_iso3(code) for code in aoi})
        if isinstance(aoi, list) and len(aoi) == 4 and isinstance(aoi[0], (int, float)):
            bbox = [float(x) for x in aoi]
        elif aoi is not None and hasattr(aoi, "total_bounds"):
            bbox = [float(x) for x in aoi.total_bounds]  # a GeoDataFrame
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

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a `SpatialExtent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox (WGS84).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        dates = pd.date_range(start_dt, end_dt, freq="YS")
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
            for iso3 in self._iso3s:
                records = rest_records(product, subalias_id, iso3)
                for year in years:
                    for url in files_for_year(records, year):
                        out.append(
                            RemoteProduct(
                                id=f"{product}_{iso3}_{year}_{Path(url).stem}",
                                href=url,
                                metadata={
                                    "product": product,
                                    "iso3": iso3,
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
        """Download `url` to `dest`, skipping when the file already exists.

        Args:
            url: The GeoTIFF URL.
            dest: Local destination path.

        Returns:
            Path: `dest`.

        Raises:
            requests.HTTPError: On a non-2xx response (e.g. a 404 names the
                offending URL).
        """
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

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
            delayed(self._http_get)(rp.href, raw / Path(rp.href).name)
            for rp in products
        )
        groups: dict[
            tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
        ] = {}
        for path, rp in zip(paths, products):
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
        return rasters + tables

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
        from pyramids.dataset import Dataset

        op = "mean" if cfg.op == "auto" else cfg.op
        by_series: dict[tuple[str, tuple[str, int] | None], dict[int, Path]] = {}
        for (product, year, cohort), path in localised.items():
            by_series.setdefault((product, cohort), {})[year] = path

        out: list[Path] = []
        for (product, cohort), year_paths in by_series.items():
            years = sorted(year_paths)
            index = pd.to_datetime([f"{y}-01-01" for y in years])
            series = pd.Series(years, index=index)
            for window_label, bucket in series.groupby(pd.Grouper(freq=cfg.freq)):
                bucket_years = list(bucket.values)
                if not bucket_years:
                    continue
                template = Dataset.read_file(str(year_paths[bucket_years[0]]))
                stack = np.stack(
                    [
                        _masked_array(Dataset.read_file(str(year_paths[y])))
                        for y in bucket_years
                    ]
                )
                with warnings.catch_warnings():
                    # All-no-data cells reduce to NaN; that is expected for
                    # ocean / outside-AOI pixels, so silence the empty-slice
                    # RuntimeWarning numpy emits for them.
                    warnings.simplefilter("ignore", RuntimeWarning)
                    reduced = _REDUCERS[op](stack, axis=0)
                tag = f"_{cohort[0]}_{cohort[1]}" if cohort else ""
                target = (
                    Path(self.path)
                    / f"{product}{tag}_{cfg.freq}_{window_label:%Y%m%d}_{op}.tif"
                )
                Dataset.create_from_array(
                    arr=reduced, geo=template.geotransform, epsg=template.epsg
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
        for product in self._products:
            wp_id = self._catalog.get(product).worldpoppy_id
            if not wp_id:
                raise ValueError(
                    f"{product!r} has no worldpoppy_id mapping; use api='rest'."
                )
            # Populate the cache; the returned xarray.DataArray is discarded.
            wp_raster(
                product_name=wp_id,
                aoi=self._iso3s,
                years=years,
                download_dry_run=True,
            )
        cache = Path(get_cache_dir())
        groups: dict[
            tuple[str, int, tuple[str, int] | None], list[tuple[Path, RemoteProduct]]
        ] = {}
        for tif in sorted(cache.rglob("*.tif")):
            match = re.search(r"_(\d{4})\.tif$", tif.name)
            if match is None:
                continue
            year = int(match.group(1))
            iso3 = tif.name[:3].upper()
            cohort = cohort_of(tif.name)
            if iso3 not in self._iso3s or year not in years:
                continue
            for product in self._products:
                demographic = self._catalog.get(product).demographic
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
                break
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
        cropped = dataset.crop(
            bbox=[
                self.space.west,
                self.space.south,
                self.space.east,
                self.space.north,
            ],
            epsg=4326,
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

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical search/fetch shape."""
        return self._api_via_search_fetch()

    def download(self, progress_bar: bool = True, aggregate=None) -> list[Path]:
        """Fetch the requested products as AOI-cropped GeoTIFFs (+ tables).

        Args:
            progress_bar: Whether per-download progress is shown.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`;
                reduces the per-year raster stack across years (`C6`).

        Returns:
            list[Path]: The written GeoTIFF / table paths.
        """
        self._show_progress = progress_bar
        self._aggregate_cfg = aggregate
        return self._api_via_search_fetch()


#: Per-op reducers over the year axis (axis 0), NaN-aware (NaN = no-data).
_REDUCERS = {
    "mean": np.nanmean,
    "sum": np.nansum,
    "min": np.nanmin,
    "max": np.nanmax,
    "std": np.nanstd,
}


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
