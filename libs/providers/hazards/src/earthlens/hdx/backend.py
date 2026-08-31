"""Backend that fetches Humanitarian Data Exchange resources via CKAN.

`HDX(AbstractDataSource)` wraps UN OCHA's `hdx-python-api` (a read-only
client over the CKAN catalogue at `data.humdata.org`, ~41k datasets:
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
escape-hatch kwargs fetch **any** of the ~41k datasets by its HDX id
without a catalog row (`G6`); when `hdx_id=` is set the catalog is
bypassed.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.hdx._helpers import match_resource
from earthlens.hdx.catalog import Catalog

#: Resolved `(hdx_id, [resource_filter, ...])` download target. An empty
#: filter list means "every resource of the dataset".
Target = tuple[str, list[str]]

_T = TypeVar("_T")


def _with_retries(
    call: Callable[[], _T], *, attempts: int = 4, base_delay: float = 1.0
) -> _T:
    """Call `call`, retrying transient failures with exponential backoff.

    CKAN reads occasionally fail with a transient 5xx / rate-limit; one
    flaky response should not abort a whole resolve. Retries up to
    `attempts` times with `base_delay * 2**n` seconds between tries
    (1s, 2s, 4s, …); the final attempt's exception propagates unchanged.
    `attempts <= 1` disables retrying.

    Args:
        call: A zero-argument callable performing the network read.
        attempts: Total number of tries before giving up.
        base_delay: Seconds for the first backoff; doubles each retry.

    Returns:
        Whatever `call` returns on the first successful attempt.
    """
    for attempt in range(max(attempts - 1, 0)):
        try:
            return call()
        except Exception:  # noqa: BLE001 - retry any transient SDK/HTTP error
            time.sleep(base_delay * 2**attempt)
    # Final attempt (or the only one when attempts <= 1): let it raise.
    return call()


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

    AGGREGATE_REFUSAL_REASON = "HDX returns resource files as-is in their native (mixed) formats, so there is no meaningful gridded reduction to apply. Call download() without aggregate= and post-process the files"

    def __init__(
        self,
        variables: dict[str, list[str]] | None = None,
        start: str | None = "1970-01-01",
        end: str | None = "2100-01-01",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "all",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        hdx_site: str = "prod",
        user_agent: str = "earthlens",
        hdx_id: str | None = None,
        resource: str | list[str] | None = None,
        cores: int = 1,
        max_retries: int = 4,
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
            cores: Number of worker threads for downloading resources in
                :meth:`_fetch`. `1` (default) downloads sequentially;
                `> 1` downloads concurrently (I/O-bound HTTP, so threads),
                preserving result order. Mirrors the CHC backend's
                `cores=`.
            max_retries: Total attempts for each CKAN read in
                :meth:`_search` (`Dataset.read_from_hdx`) before giving
                up, with exponential backoff between tries. `1` disables
                retrying. Defaults to `4` (≈1s, 2s, 4s backoff).

        Raises:
            ValueError: When neither `variables` nor `hdx_id=` is given,
                or a dataset key is unknown (the catalog's did-you-mean
                hint is surfaced).
        """
        # bbox / time are ignored for the query (`G2`); the facade may pass
        # `start=None` / `end=None`, so fall back to wide sentinels.
        start = start if start is not None else "1970-01-01"
        end = end if end is not None else "2100-01-01"

        self._hdx_site = hdx_site
        self._user_agent = user_agent
        self._hdx_id = hdx_id
        self._resource = resource
        self._cores = cores
        self._max_retries = max_retries

        self._catalog = Catalog()
        self._targets: list[Target] = self._resolve_targets(variables, hdx_id, resource)

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
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="all")

    def _search(self) -> list[RemoteProduct]:
        """Resolve every requested dataset and list its matching resources.

        One `Dataset.read_from_hdx` call per target (a curated dataset's
        `hdx_id` or the `hdx_id=` escape hatch, `G6`); each dataset's
        resources are filtered with :func:`match_resource` against the
        target's filter list (`G2`). bbox / time are **not** used — CKAN
        has no spatial/temporal query. Each surviving resource becomes
        one :class:`RemoteProduct` whose `metadata` carries the raw SDK
        resource handle, so :meth:`_fetch` can download without
        re-querying.

        Returns:
            list[RemoteProduct]: One product per matching resource,
                across every requested dataset. An empty list (no
                resource matched) short-circuits the fetch.

        Raises:
            ImportError: When the `[hdx]` extra is not installed.
            ValueError: When a requested `hdx_id` is not found on HDX.
        """
        try:
            from hdx.data.dataset import Dataset
        except ImportError as exc:
            raise ImportError(
                "the HDX backend needs `hdx-python-api`; install "
                "`pip install earthlens[hdx]`."
            ) from exc

        products: list[RemoteProduct] = []
        for hdx_id, filters in self._targets:
            dataset = _with_retries(
                functools.partial(Dataset.read_from_hdx, hdx_id),
                attempts=self._max_retries,
            )
            if dataset is None:
                raise ValueError(
                    f"HDX dataset {hdx_id!r} not found on the {self._hdx_site!r} "
                    "site. Check the id (or the catalog key that resolves to it)."
                )
            for resource in dataset.get_resources():
                name = resource.get("name") or ""
                fmt = resource.get("format") or ""
                if filters and not any(
                    match_resource(name, fmt, flt) for flt in filters
                ):
                    continue
                products.append(
                    RemoteProduct(
                        id=f"{hdx_id}::{name}",
                        href=resource.get("url"),
                        metadata={
                            "resource": resource,
                            "format": fmt,
                            "hdx_id": hdx_id,
                            "name": name,
                        },
                    )
                )
        logger.info(
            f"HDX search resolved {len(self._targets)} dataset(s) to "
            f"{len(products)} resource(s) to download."
        )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download every resource `_search` returned, namespaced per dataset.

        Each resource is downloaded **as-is** in its native format
        (`G4`); reading / sniffing / converting it into a pyramids type
        is the deferred `PY-D` work item, not done here. The CKAN format
        label is already recorded on the product metadata.

        Files are written into a per-dataset subdirectory
        (`root_dir / <hdx_id> / <resource-name>`) so that two requested
        datasets carrying a resource with the same file name do not
        overwrite each other. When `cores > 1` the per-resource downloads
        run on a thread pool (the work is I/O-bound HTTP), preserving the
        input order in the result.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: Local paths of every downloaded resource, in
                product order. Empty list when `products` is empty.
        """
        if self._cores > 1 and len(products) > 1:
            from joblib import Parallel, delayed

            out_paths = Parallel(n_jobs=self._cores, prefer="threads")(
                delayed(self._download_one)(product) for product in products
            )
        else:
            out_paths = [self._download_one(product) for product in products]
        logger.info(
            f"HDX download summary: {len(out_paths)} resource file(s) written "
            f"to {self.root_dir}"
        )
        return cast("list[Path]", out_paths)

    def _download_one(self, product: RemoteProduct) -> Path:
        """Download one product's resource into its per-dataset subdir.

        Args:
            product: A product from :meth:`_search`.

        Returns:
            Path: The local path of the downloaded resource file.
        """
        resource = product.metadata["resource"]
        # `hdx_id` is a CKAN slug for curated rows, but arbitrary text via
        # the `hdx_id=` escape hatch; take only its final path segment so a
        # crafted value cannot escape `root_dir`.
        subdir = Path(product.metadata["hdx_id"]).name or "dataset"
        folder = self.root_dir / subdir
        folder.mkdir(parents=True, exist_ok=True)
        _url, local_path = resource.download(folder=str(folder))
        return Path(local_path)

    def download(
        self,
        progress_bar: bool = True,
        read: bool = False,
    ) -> list[Path] | list:
        """Resolve the requested datasets and download their resources.

        Composes :meth:`_search` (resolve each dataset → filter its
        resources) and :meth:`_fetch` (download the matching files to
        `self.root_dir`). By default returns the local file paths; with
        `read=True` each downloaded resource is additionally read into
        its pyramids type via `pyramids.read_resource` (`M1`).

        Args:
            progress_bar: Accepted for signature parity with the other
                backends; the HDX SDK exposes no per-resource download
                progress hook, so this is a no-op.
            read: When `True`, read each downloaded resource into its
                pyramids type — a `Dataset` (raster), a
                `FeatureCollection` (vector), or a `DataFrame` (tabular)
                — dispatched by the recorded CKAN format label, instead
                of returning raw paths. Needs `pyramids-gis >= 0.29.0`
                (which provides `read_resource`); on an older pyramids it
                is rejected with a clear upgrade message. Defaults to
                `False` (return paths).

        Returns:
            list[Path]: Local paths of every downloaded resource, in
                dataset/resource order — when `read=False` (default).
            list: One read pyramids object per resource (a
                `Dataset` / `FeatureCollection` / `DataFrame`), in the
                same order — when `read=True`.
        """
        if not read:
            return self._api_via_search_fetch()

        read_resource = self._load_reader()
        products = self._search()
        if not products:
            return []
        paths = self._fetch(products)
        return [
            read_resource(path, fmt=product.metadata.get("format") or None)
            for path, product in zip(paths, products)
        ]

    @staticmethod
    def _load_reader():
        """Return `pyramids.read_resource`, or raise a clear upgrade error.

        The reader (the `PY-D` capability) is the `pyramids.read_resource`
        function, available in `pyramids-gis >= 0.29.0`. The package floor
        already requires that, so this normally just imports; the guard
        stays as defense for a partial / source install that predates the
        reader, surfaced as a `NotImplementedError` (mirroring how the
        Earthdata backend feature-detects its `pyramids` reducers).

        Returns:
            The `pyramids.read_resource` callable.

        Raises:
            NotImplementedError: When the installed pyramids predates the
                reader.
        """
        try:
            from pyramids import read_resource
        except ImportError as exc:
            raise NotImplementedError(
                "HDX.download(read=True) needs `pyramids.read_resource` "
                "(pyramids-gis >= 0.29.0); the installed pyramids does not "
                "provide it. Upgrade pyramids, or call download() without "
                "read= to get the resource file paths and read them yourself."
            ) from exc
        return read_resource


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
