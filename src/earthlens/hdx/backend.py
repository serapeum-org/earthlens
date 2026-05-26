"""Backend that fetches Humanitarian Data Exchange resources via CKAN.

`HDX(AbstractDataSource)` wraps UN OCHA's `hdx-python-api` (a read-only
client over the CKAN catalogue at `data.humdata.org`, ~21k datasets:
Kontur Population, Meta HRSL / RWI, HOTOSM building & road exports,
VIDA conflated buildings, UNDP / UNHCR / WFP layers, country
humanitarian profiles). A request names a curated **dataset** + an
optional **resource filter**; the backend resolves the dataset, filters
its resources, and downloads the matching files to disk.

**This backend's `OUTPUT_KIND` is the fixed value `"mixed"` — the first
mixed backend (`G1`).** An HDX resource is whatever the contributor
uploaded — CSV, GeoTIFF, GeoPackage, GeoJSON, Parquet — and one dataset
can carry several kinds at once, so no single raster / vector / tabular
label fits. The MVP downloads each resource file *as-is* and records its
CKAN format label (`G4`); reading / sniffing / converting a resource
into a pyramids type is the deferred `PY-D` work item, not done here.

Three sharp distinctions from the sibling Earthdata backend shape the
design:

* **No spatial/temporal search (`G2`).** CKAN addresses datasets by id,
  not by bbox/time, so `lat_lim` / `lon_lim` / `start` / `end` are
  accepted (the facade requires them) but **ignored for the query** —
  at most recorded as metadata. `variables` is a
  `{dataset_key: [resource_filter, ...]}` mapping.
* **Mixed output, dispatched by format (`G1`/`G4`).** `OUTPUT_KIND =
  "mixed"`; the facade *forwards* `aggregate=` for a mixed backend, so
  this backend itself rejects a non-`None` `aggregate=` with
  `NotImplementedError` (aggregating an arbitrary CKAN resource is
  meaningless).
* **No auth (`G3`).** HDX is public read-only — `_initialize` calls
  `Configuration.create(hdx_read_only=True, user_agent=...)` once
  (guarded against the singleton re-create error). No `AbstractAuth`,
  no `[hdx]` credential.

Beyond the curated catalog, the `hdx_id=` (+ optional `resource=`)
escape-hatch kwargs fetch **any** of the ~21k datasets by its HDX id
without a catalog row (`G6`); when `hdx_id=` is set the catalog is
bypassed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.hdx.catalog import Catalog

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

#: Resolved `(hdx_id, [resource_filter, ...])` download target. An empty
#: filter list means "every resource of the dataset".
Target = tuple[str, list[str]]


class HDX(AbstractDataSource):
    """Humanitarian Data Exchange backend (mixed-format file output).

    Wraps the read-only `hdx-python-api` client so a user can resolve a
    curated HDX dataset (or an arbitrary HDX id), filter its resources,
    and download the matching files through the same `download()` shape
    every other earthlens backend uses. HDX is a public catalogue, so no
    credentials are needed.

    Attributes:
        OUTPUT_KIND: `"mixed"` — an HDX dataset can carry raster, vector
            and tabular resources at once, so no single kind fits. The
            facade *forwards* `aggregate=` for a mixed backend; this
            backend rejects it itself (`G1`).
    """

    OUTPUT_KIND: OutputKind = "mixed"

    def __init__(
        self,
        variables: dict[str, list[str]] | None = None,
        start: str = "1970-01-01",
        end: str = "2100-01-01",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        hdx_site: str = "prod",
        user_agent: str = "earthlens",
        hdx_id: str | None = None,
        resource: str | list[str] | None = None,
    ):
        """Initialise an HDX backend instance.

        Resolves every requested dataset key against the catalog
        **before** the parent constructor runs, so an unknown key fails
        fast (with a did-you-mean hint) rather than at download time.
        When `hdx_id=` is given the catalog is bypassed entirely (`G6`).

        Args:
            variables: Mapping from curated dataset key to a list of
                resource filters, e.g. `{"kontur-population": []}` (all
                resources) or `{"hotosm-roads": ["*.gpkg"]}`. Each filter
                is a resource-name glob or a CKAN format label; an empty
                list (or a key with no filter) falls back to the
                catalog row's default `resource_filter`. Required unless
                `hdx_id=` is given.
            start: Inclusive start date string. **Ignored for the query**
                (`G2`) — CKAN has no temporal search; accepted only for
                facade parity and recorded as metadata.
            end: Inclusive end date string. Ignored for the query
                (`G2`).
            lat_lim: `[lat_min, lat_max]`. **Ignored for the query**
                (`G2`); defaults to the whole globe.
            lon_lim: `[lon_min, lon_max]`. Ignored for the query (`G2`);
                defaults to the whole globe.
            temporal_resolution: Sentinel `"all"` — HDX is not chunked
                by date.
            path: Output directory. Created by the parent class if it
                does not exist.
            fmt: `strptime` format for `start` / `end`.
            hdx_site: HDX site to target — `"prod"` (default) or
                `"stage"`.
            user_agent: User agent string the SDK requires; defaults to
                `"earthlens"`.
            hdx_id: Optional arbitrary HDX dataset id / name. When given,
                the curated catalog is bypassed and this id is read
                directly (`G6`); `variables` is then optional.
            resource: Optional resource filter(s) for the `hdx_id=`
                escape hatch — a single glob / format label or a list of
                them.

        Raises:
            ValueError: When neither `variables` nor `hdx_id=` is given,
                or a dataset key is unknown (the catalog's did-you-mean
                hint is surfaced).
        """
        self._hdx_site = hdx_site
        self._user_agent = user_agent
        self._hdx_id = hdx_id
        self._resource = resource
        self._show_progress = True

        self._catalog = Catalog()
        self._targets: list[Target] = self._resolve_targets(
            variables, hdx_id, resource
        )

        super().__init__(
            start=start,
            end=end,
            variables=variables or {},
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else [-90.0, 90.0],
            lon_lim=lon_lim if lon_lim is not None else [-180.0, 180.0],
            fmt=fmt,
            path=path,
        )

    def _resolve_targets(
        self,
        variables: dict[str, list[str]] | None,
        hdx_id: str | None,
        resource: str | list[str] | None,
    ) -> list[Target]:
        """Resolve the request into `(hdx_id, [resource_filter, ...])` targets.

        The `hdx_id=` escape hatch (`G6`) takes precedence over
        `variables`: when set, the single arbitrary id is returned with
        its optional `resource=` filter(s) and the catalog is not
        consulted. Otherwise every key in `variables` is resolved to its
        catalog row; the per-key filter list (if non-empty) overrides
        the row's default `resource_filter`.

        Args:
            variables: The `{dataset_key: [resource_filter, ...]}`
                request, or `None`.
            hdx_id: The arbitrary-dataset escape hatch id, or `None`.
            resource: The escape hatch's resource filter(s), or `None`.

        Returns:
            list[Target]: One `(hdx_id, [resource_filter, ...])` per
                requested dataset, in request order.

        Raises:
            ValueError: When neither `variables` nor `hdx_id` is given,
                or a dataset key is unknown.
        """
        if hdx_id is not None:
            filters = _as_filter_list(resource)
            return [(hdx_id, filters)]
        if not variables:
            raise ValueError(
                "HDX requires a non-empty `variables` mapping of "
                "{dataset_key: [resource_filter, ...]} (or pass hdx_id= to "
                "fetch an arbitrary HDX dataset by its id)."
            )
        targets: list[Target] = []
        for key, filters in variables.items():
            row = self._catalog.resolve(key)
            requested = _as_filter_list(filters)
            if not requested and row.resource_filter:
                requested = [row.resource_filter]
            targets.append((row.hdx_id, requested))
        return targets

    def _initialize(self):
        """Configure the read-only HDX client once (`G3`).

        Calls `Configuration.create(hdx_read_only=True, user_agent=...)`
        guarded against the SDK's singleton re-create error: HDX keeps
        one global `Configuration`, so re-creating raises
        `ConfigurationError`. The guard reads the existing config first
        and only creates one when none exists, so constructing several
        `HDX` instances in one process is safe.

        Returns:
            None: The SDK keeps the configuration as a global singleton,
                so no per-instance client object is bound.

        Raises:
            ImportError: When the `[hdx]` extra is not installed.
        """
        try:
            from hdx.api.configuration import Configuration, ConfigurationError
        except ImportError as exc:
            raise ImportError(
                "the HDX backend needs `hdx-python-api`; install "
                "`pip install earthlens[hdx]`."
            ) from exc

        try:
            Configuration.read()
        except ConfigurationError:
            Configuration.create(
                hdx_site=self._hdx_site,
                user_agent=self._user_agent,
                hdx_read_only=True,
            )
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (ignored, `G2`).

        HDX/CKAN has no spatial query, so the bbox is never sent to the
        server; it is validated and kept only so the request shape
        matches the other backends.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox (unused by the query).
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent` (ignored).

        HDX has no temporal query (`G2`); the window is parsed only for
        facade parity and never narrows the resource selection. The
        resolution is kept as the sentinel `"all"`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label; HDX
                always returns whole resources.
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

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Resolve the requested datasets and download their resources.

        Composes :meth:`_search` (resolve each dataset → filter its
        resources) and :meth:`_fetch` (download the matching files to
        `self.root_dir`) and returns the local paths.

        Args:
            progress_bar: Forwarded to the SDK download as a best-effort
                progress signal.
            aggregate: Must be `None`. An HDX resource is returned
                as-is in its native format (`G4`); there is no gridded
                reduction to apply, so a non-`None` value is rejected
                even though the facade forwards `aggregate=` for a
                `"mixed"` backend (`G1`).

        Returns:
            list[Path]: Local paths of every downloaded resource, in
                dataset/resource order.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "HDX.download(aggregate=...) is not supported: HDX returns "
                "resource files as-is in their native (mixed) formats, so "
                "there is no meaningful gridded reduction to apply. Call "
                "download() without aggregate= and post-process the files."
            )
        self._show_progress = progress_bar
        return self._api_via_search_fetch()


def _as_filter_list(value: str | list[str] | None) -> list[str]:
    """Normalise a resource-filter argument into a list of filter strings.

    Args:
        value: A single filter string, a list of them, or `None`.

    Returns:
        list[str]: The filters as a list (empty for `None` / empty
            input).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [f for f in value if f]
