"""Backend that fetches glacier outlines / fluctuations from three open sources.

`Glaciers(AbstractDataSource)` routes one request to whichever of three open
glacier sources the chosen dataset names — RGI 7.0 (per-region outline
shapefiles via UNESCO IHP-WINS), GLIMS (time-series outlines via the GLIMS
GeoServer WFS), or WGMS (Fluctuations of Glaciers tabular CSV tables) — and
returns the result in the shape that dataset declares. A request is one dataset
id (`variables=["rgi:outlines"]`) plus a bbox (rgi/glims) or an optional
glacier / region selector (wgms).

Three design points carry this backend:

* **Per-instance `OUTPUT_KIND` (`G1`).** The resolved dataset's `output_kind` is
  copied onto `self.OUTPUT_KIND` in `__init__`: `vector` returns a pyramids
  :class:`~pyramids.feature.collection.FeatureCollection` (rgi/glims outlines),
  `tabular` returns a :class:`pandas.DataFrame` (wgms fluctuations). The
  :class:`earthlens.earthlens.EarthLens` facade reads the instance attribute to
  gate `aggregate=` (rejected for both) and to know the return shape.
* **Region-download-then-clip (`G6`).** RGI ships per GTN-G region; the backend
  maps the request bbox to the overlapping region(s) (a `region=` override is
  accepted), downloads + caches each region ZIP once (the ghsl model), reads it
  via pyramids `FeatureCollection.read_file`, clips to the bbox, and merges.
* **No auth (`G5`).** All three sources are open, so the backend ships no auth
  module and never touches the Earthdata-gated NSIDC host.

Outlines / fluctuations are pre-computed inventories, so `aggregate=` is rejected
(`G8`), vector file I/O always goes through pyramids (`G3`), and the WGMS path is
pure pandas.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.config import cache_dir
from earthlens.glaciers import _helpers
from earthlens.glaciers.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk output formats for a written tabular result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Whole-globe sentinel bounds — the WGMS path defaults to global (its filters
#: are glacier / region selectors, not a bbox), and the global bbox is the
#: "no spatial filter" marker for the RGI / GLIMS guards.
_GLOBAL_LAT: list[float] = [-90.0, 90.0]
_GLOBAL_LON: list[float] = [-180.0, 180.0]


class Glaciers(AbstractDataSource):
    """Glacier outlines / fluctuations backend (mixed output).

    Resolves one dataset id to its catalog row, routes the request to that row's
    source (RGI / GLIMS / WGMS), and returns a pyramids
    :class:`~pyramids.feature.collection.FeatureCollection` (`vector`) or a
    :class:`pandas.DataFrame` (`tabular`) per the row's `output_kind`. The query
    is a search/fetch split: :meth:`_search` pins the products (one per
    overlapping RGI region, or a single GLIMS / WGMS product) and :meth:`_fetch`
    downloads + reads each.

    All three sources are open, so the backend needs no credentials. `aggregate=`
    is rejected — outlines / fluctuations are pre-computed inventories, not
    gridded rasters (`G8`).

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the resolved
            dataset's `output_kind` (`"vector"` or `"tabular"`). The facade reads
            it to gate `aggregate=` and to know the return shape.
    """

    OUTPUT_KIND: OutputKind = "vector"

    AGGREGATE_REFUSAL_REASON = "glacier outlines / fluctuations are pre-computed inventories, not gridded rasters, so there is no meaningful gridded reduction. Call download() without aggregate="

    #: The outline / fluctuation records span their whole archive, so a missing `start`
    #: / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "annual",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        region: str | list[str] | None = None,
        max_features: int = 10000,
        glacier_id: int | str | list | None = None,
        glacier_name: str | None = None,
        output_format: OutputFormat = "csv",
    ):
        """Initialise a glaciers backend instance.

        Args:
            start: Inclusive start of an optional window, parsed with `fmt`;
                `None` is allowed (outlines are an inventory, not a time series).
            end: Inclusive end of the optional window; `None` allowed.
            variables: A one-element list naming the dataset id
                (`["rgi:outlines"]`). Exactly one dataset is resolved per
                instance, because `OUTPUT_KIND` is per instance.
            lat_lim: `[lat_min, lat_max]` AOI. Required (with `lon_lim`) for an
                RGI request without a `region=` override, and for GLIMS; the
                WGMS path defaults to global.
            lon_lim: `[lon_min, lon_max]` AOI.
            temporal_resolution: Recorded as the resolution label only.
            path: Output directory. When omitted it falls back to the configured
                earthlens output directory (`set_output_dir()` /
                `EARTHLENS_DATA_DIR`); see `earthlens.config`. The download cache
                lives under the shared cache directory, not here.
            fmt: `strptime` format for `start` / `end`.
            region: RGI only — one GTN-G region id (`"11"`) or a list, overriding
                the bbox -> region mapping. Also accepted as a WGMS region filter.
            max_features: GLIMS only — cap on WFS-returned features.
            glacier_id: WGMS only — one glacier id or a list to filter on.
            glacier_name: WGMS only — a case-insensitive substring filter.
            output_format: On-disk format for a written WGMS result — `"csv"`
                (default) or `"parquet"`.

        Raises:
            TypeError: If `variables` is a mapping (pass a list of one id).
            ValueError: If `variables` is not exactly one dataset id,
                `output_format` is unrecognised, or the required spatial selector
                for the resolved source is missing.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "Glaciers `variables` must be a one-element list naming the "
                "dataset id (e.g. ['rgi:outlines']), not a mapping."
            )
        ids = list(dict.fromkeys(variables)) if variables else []
        if len(ids) != 1:
            raise ValueError(
                "Glaciers needs exactly one dataset id in variables= "
                "(OUTPUT_KIND is per instance); got "
                f"{ids!r}. Available: {Catalog().available()}."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(ids[0])
        self._region = region
        self._max_features = max_features
        self._glacier_id = glacier_id
        self._glacier_name = glacier_name
        self._output_format: OutputFormat = output_format

        # G1 — the per-instance output shape comes from the resolved dataset.
        self.OUTPUT_KIND = self._dataset.output_kind

        self._has_bbox = lat_lim is not None and lon_lim is not None
        self._validate_selector()

        super().__init__(
            # Glaciers permits a None window (outlines are an inventory); its
            # _check_input_dates override accepts str | None, though the base
            # signature is typed str.
            start=cast("str", start),
            end=cast("str", end),
            variables=ids,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else _GLOBAL_LAT,
            lon_lim=lon_lim if lon_lim is not None else _GLOBAL_LON,
            fmt=fmt,
            path=path,
        )

    @property
    def _cache_dir(self) -> Path:
        """The directory downloaded archives are cached in.

        Resolved per call from the shared earthlens cache directory, so a later
        `set_cache_dir()` moves it.

        Returns:
            Path: `<cache_dir()>/glaciers`.
        """
        return cache_dir() / "glaciers"

    def _validate_selector(self) -> None:
        """Check the request carries the spatial selector the source needs.

        RGI needs a bbox or a `region=` override (else it would pull all 19
        regions); GLIMS needs a bbox (a global WFS query is rejected); WGMS needs
        nothing (its filters are optional).

        Raises:
            ValueError: When a required selector is missing.
        """
        source = self._dataset.source
        if source == "rgi" and not (self._has_bbox or self._region):
            raise ValueError(
                f"dataset {self._dataset.id!r} (RGI) needs a bbox "
                "(lat_lim=/lon_lim= or aoi=) or a region= override; refusing to "
                "download every GTN-G region."
            )
        if source == "glims" and not self._has_bbox:
            raise ValueError(
                f"dataset {self._dataset.id!r} (GLIMS) needs a bbox "
                "(lat_lim=/lon_lim= or aoi=) — a global WFS query is too large."
            )

    def _check_input_dates(
        self,
        start: str | None,
        end: str | None,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the optional `[start, end]` window into a :class:`TemporalExtent`.

        Dates are not part of the request (outlines are an inventory), so `None`
        bounds are allowed and yield a `None`-dated extent.

        Args:
            start: Inclusive start date string, or `None`.
            end: Inclusive end date string, or `None`.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed (or `None`) endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = to_datetime(start, fmt) if start else None
        end_dt = to_datetime(end, fmt) if end else None
        dates = (
            pd.DatetimeIndex([start_dt, end_dt])
            if start_dt is not None and end_dt is not None
            else pd.DatetimeIndex([])
        )
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=dates,
        )

    @property
    def _bbox(self) -> list[float]:
        """The request AOI as `[west, south, east, north]` in EPSG:4326."""
        space = self.space
        return [space.west, space.south, space.east, space.north]

    def _search(self) -> list[RemoteProduct]:
        """List the products to fetch for the resolved source.

        For RGI, one product per overlapping GTN-G region (`G6`); for GLIMS and
        WGMS, a single product.

        Returns:
            list[RemoteProduct]: The products. Empty when an RGI bbox overlaps no
                glacier region (the vector path then returns an empty
                collection).
        """
        source = self._dataset.source
        if source == "rgi":
            if self._region is not None:
                region_ids = [str(r) for r in _helpers._as_list(self._region)]
            else:
                region_ids = _helpers.regions_for_bbox(
                    self._bbox, self._catalog.regions
                )
            products = []
            for region_id in region_ids:
                region = self._catalog.regions[region_id]
                products.append(
                    RemoteProduct(
                        id=f"{self._dataset.id}/{region_id}",
                        href=region.url,
                        metadata={"region_id": region_id},
                    )
                )
            return products
        return [RemoteProduct(id=self._dataset.id)]

    def _fetch(self, products: list[RemoteProduct]) -> list:
        """Download + read every product `_search` returned.

        Args:
            products: The list from :meth:`_search`.

        Returns:
            list: One :class:`~pyramids.feature.collection.FeatureCollection`
                per RGI region / the GLIMS query (`vector`), or a single
                :class:`pandas.DataFrame` (`tabular`, wgms).
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct):
        """Fetch and read one product per its source.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            A `FeatureCollection` (rgi/glims) or a `DataFrame` (wgms).

        Raises:
            requests.HTTPError: If a download / WFS query fails after retries.
        """
        dataset = self._dataset
        if dataset.source == "rgi":
            assert product.href is not None  # rgi products always carry an href
            zip_path = _helpers.download_zip(product.href, self._cache_dir)
            return _helpers.read_outlines(zip_path, self._bbox)
        if dataset.source == "glims":
            # A glims row guarantees both WFS fields (catalog model validator).
            assert dataset.wfs_url is not None and dataset.wfs_typename is not None
            dest = self._cache_dir / "glims_query.geojson"
            return _helpers.fetch_glims(
                dataset.wfs_url,
                dataset.wfs_typename,
                self._bbox,
                dest,
                max_features=self._max_features,
            )
        # source == "wgms"
        # A wgms row guarantees archive_url + table (catalog model validator).
        assert dataset.archive_url is not None and dataset.table is not None
        zip_path = _helpers.download_zip(dataset.archive_url, self._cache_dir)
        table = _helpers.parse_wgms_csv(zip_path, dataset.table)
        glaciers = _helpers.wgms_glacier_table(zip_path)
        bbox = self._bbox if self._has_bbox else None
        return _helpers.filter_wgms(
            table,
            glaciers,
            glacier_id=self._glacier_id,
            glacier_name=self._glacier_name,
            region=self._region,
            bbox=bbox,
        )

    def download(
        self,
        progress_bar: bool = True,
    ) -> FeatureCollection | pd.DataFrame:
        """Fetch the dataset and return its per-instance shape.

        Args:
            progress_bar: Accepted for signature parity; the per-region RGI
                fetch is a small loop, so this is a no-op.

        Returns:
            A pyramids :class:`~pyramids.feature.collection.FeatureCollection`
            (`vector`, rgi/glims; clipped, EPSG:4326) or a
            :class:`pandas.DataFrame` (`tabular`, wgms; also written to
            `root_dir`).

        Raises:
            requests.HTTPError: If a download / WFS query fails after retries.
        """
        results = self._api()
        self._log_citation()
        if self.OUTPUT_KIND == "vector":
            if not results:
                collection = _helpers.empty_feature_collection()
            else:
                collection = _helpers.concat_outlines(results)
            logger.info(
                f"Glaciers {self._dataset.id}: returned a FeatureCollection "
                f"({len(collection)} outline(s))."
            )
            return collection
        df = results[0] if results else _helpers.empty_canonical(["glacier_id"])
        out_path = self._write_table(df)
        if len(df):
            logger.info(
                f"Glaciers {self._dataset.id}: {len(df)} row(s) written to {out_path}."
            )
        else:
            logger.warning(
                f"Glaciers {self._dataset.id}: no rows matched; wrote an empty "
                f"(schema-only) table to {out_path}."
            )
        return df

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write a WGMS result to `root_dir` and return the path.

        Args:
            df: The result frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: If `output_format="parquet"` but `pyarrow` is missing.
        """
        stem = self._dataset.id.replace(":", "_")
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"{stem}.{ext}"
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

    def _log_citation(self) -> None:
        """Log the resolved dataset's source citation once (info, not a warning)."""
        citation = self._dataset.citation
        if citation:
            logger.info(f"Glaciers source citation: {citation}")
