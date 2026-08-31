"""Facet -> `zstore` resolver over the CMIP6 consolidated-stores CSV.

The Pangeo CMIP6 ARCO index is a single flat CSV — one row per Zarr store,
keyed by the CMIP6 facets (`source_id`, `experiment_id`, `variable_id`,
`table_id`, `member_id`, `grid_label`, `version`, ...) with the store URI in the
`zstore` column. :class:`StoreResolver` fetches and caches that CSV (with
`requests`, read with `pandas` — both core), then filters it by a requested
facet tuple to the matching `zstore` URI(s).

The resolver is deliberately stateless and injectable: pass a pre-loaded
`frame=` or a local `cache_path=` to run with **no network**. On a miss it raises
a `ValueError` that names the facet which eliminated every row and lists the
values that *were* available — so a typo in a model or scenario name is easy to
fix.

No `intake-esm` (it would drag in `xarray`); no `xarray` / `zarr` / `gcsfs`
here — this module only resolves URIs. Opening the store is
:mod:`earthlens.cmip6.accessor`'s job (pyramids).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from earthlens.base.http import HttpClient
from earthlens.config import cache_dir

if TYPE_CHECKING:
    import pandas as pd

#: The request facets the resolver filters on, in precedence order. This — not
#: the catalog's `facet_columns:` block (which merely documents the CSV schema) —
#: is the single source of truth for *which* facets `resolve()` filters and in
#: *what* order. `version` is handled specially (`latest` picks the newest), so
#: it is not listed here.
_FILTER_FACETS = (
    "activity_id",
    "source_id",
    "experiment_id",
    "variable_id",
    "table_id",
    "member_id",
    "grid_label",
)

#: Identity facets that make a store unique apart from its `version` — used to
#: reduce a `version="latest"` request to the newest publication per store.
_IDENTITY_FACETS = (
    "activity_id",
    "institution_id",
    "source_id",
    "experiment_id",
    "member_id",
    "table_id",
    "variable_id",
    "grid_label",
)


def default_cache_path() -> Path:
    """Return the default on-disk location for the cached CSV.

    Resolved from the shared earthlens cache directory (`set_cache_dir()` /
    `EARTHLENS_CACHE`). The CMIP6 CSV lands under a `cmip6/` subdirectory.

    Returns:
        Path: `<cache_dir()>/cmip6/pangeo-cmip6.csv`.
    """
    return cache_dir() / "cmip6" / "pangeo-cmip6.csv"


@dataclass(frozen=True)
class ResolvedStore:
    """One CMIP6 Zarr store resolved from a facet tuple.

    Carries the `zstore` URI plus the facet values that identify it, so the
    backend can name the output file and log the provenance without re-querying
    the CSV.

    Attributes:
        zstore: The `gs://cmip6/...` store URI (ends in `/`).
        source_id: Model that produced the store.
        experiment_id: Scenario / diagnostic experiment.
        variable_id: The CMIP6 variable.
        table_id: The MIP table (realm x cadence).
        member_id: The variant label (`r1i1p1f1`).
        grid_label: Grid label (`gn` native, `gr` regridded, ...).
        version: Data-publication version (an integer date, as a string).
        activity_id: The MIP the experiment belongs to.

    Examples:
        - Build one directly:
            ```python
            >>> from earthlens.cmip6.resolver import ResolvedStore
            >>> s = ResolvedStore(
            ...     zstore="gs://cmip6/CMIP6/ScenarioMIP/.../tas/gn/v20190101/",
            ...     source_id="CanESM5", experiment_id="ssp585", variable_id="tas",
            ...     table_id="Amon", member_id="r1i1p1f1", grid_label="gn",
            ...     version="20190101", activity_id="ScenarioMIP",
            ... )
            >>> s.variable_id
            'tas'

            ```
    """

    zstore: str
    source_id: str
    experiment_id: str
    variable_id: str
    table_id: str
    member_id: str
    grid_label: str
    version: str
    activity_id: str = ""

    @property
    def slug(self) -> str:
        """A filesystem-safe stem identifying this store.

        Returns:
            str: `<source>_<experiment>_<variable>_<table>_<member>_<grid>` with
                any path separators removed.
        """
        parts = [
            self.source_id,
            self.experiment_id,
            self.variable_id,
            self.table_id,
            self.member_id,
            self.grid_label,
        ]
        return "_".join(str(p).replace("/", "-") for p in parts if p)


class StoreResolver:
    """Resolve CMIP6 facet tuples to `zstore` URIs over the consolidated CSV.

    Fetches + caches the CSV once, then filters it per request. Construct with
    the catalog's `csv_url` + `facet_columns`; inject a `frame=` or `cache_path=`
    to run offline.

    Args:
        csv_url: URL of the consolidated-stores CSV.
        facet_columns: The CSV facet column names (from the catalog), kept as
            schema documentation; `resolve()` filters on :data:`_FILTER_FACETS`,
            not on this list.
        cache_path: Where to cache the downloaded CSV. Defaults to
            :func:`default_cache_path`.
        frame: A pre-loaded `DataFrame` to use verbatim, skipping all I/O.
        timeout: Per-request network timeout, in seconds.
    """

    def __init__(
        self,
        csv_url: str,
        facet_columns: list[str],
        *,
        cache_path: Path | str | None = None,
        frame: pd.DataFrame | None = None,
        timeout: float = 120.0,
    ):
        self.csv_url = csv_url
        self.facet_columns = list(facet_columns)
        self.cache_path = (
            Path(cache_path) if cache_path is not None else default_cache_path()
        )
        self.timeout = timeout
        self._frame = frame

    @property
    def frame(self) -> pd.DataFrame:
        """The consolidated-stores table, loaded + cached on first access.

        Returns:
            pandas.DataFrame: The full store index.
        """
        if self._frame is None:
            self._frame = self._load()
        return self._frame

    def _load(self) -> pd.DataFrame:
        """Read the CSV into a `DataFrame`, downloading + caching it if needed.

        Returns:
            pandas.DataFrame: The parsed CSV.
        """
        import pandas as pd

        path = self._ensure_csv()
        return pd.read_csv(path, low_memory=False)

    def _ensure_csv(self) -> Path:
        """Return the cached CSV path, downloading it once if absent.

        Streams through :class:`~earthlens.base.http.HttpClient`'s atomic
        `download` (temp `.part` file + rename, cleanup on failure), then
        enforces the non-empty invariant: a zero-byte transfer unlinks the
        cache and raises rather than caching a useless file.

        Returns:
            Path: The local cache path (guaranteed to exist and be non-empty).

        Raises:
            requests.RequestException: On a transport error or a non-2xx
                status from the download (`HttpClient.download` calls
                `raise_for_status` — this covers `HTTPError` for a 4xx/5xx
                on the CSV, `ConnectionError` for a network failure, and
                `Timeout` if the transfer stalls past `self.timeout`).
            OSError: If the atomic rename of the `.part` temp to
                `cache_path` fails.
            RuntimeError: When the download succeeds but yields a
                zero-byte CSV (the cache is unlinked before raising).
        """
        if self.cache_path.exists() and self.cache_path.stat().st_size > 0:
            return self.cache_path
        client = HttpClient(
            timeout=self.timeout,
            max_retries=0,
            status_forcelist=(),
            raise_for_status=True,
        )
        client.download(
            self.csv_url,
            self.cache_path,
            chunk=1 << 20,
            atomic=True,
            progress=False,
        )
        if self.cache_path.stat().st_size == 0:
            self.cache_path.unlink(missing_ok=True)
            raise RuntimeError(f"downloaded an empty CSV from {self.csv_url}")
        return self.cache_path

    def resolve(
        self,
        *,
        source_id: str,
        experiment_id: str,
        variable_id: str,
        table_id: str,
        member_id: str | None = None,
        grid_label: str | None = None,
        version: str = "latest",
        activity_id: str | None = None,
    ) -> list[ResolvedStore]:
        """Resolve a facet tuple to the matching `zstore` store(s).

        Filters the CSV by every pinned facet (unset facets fan out), then
        reduces `version="latest"` to the newest publication per store. Returns
        one :class:`ResolvedStore` per surviving row.

        Args:
            source_id: Model (required).
            experiment_id: Scenario / experiment (required).
            variable_id: Variable (required).
            table_id: MIP table (required).
            member_id: Variant label; `None` fans out over all members.
            grid_label: Grid label; `None` fans out over all grids.
            version: `"latest"` (newest per store) or an explicit version
                string.
            activity_id: MIP; `None` leaves it unconstrained.

        Returns:
            list[ResolvedStore]: One entry per matching store. For
                `version="latest"` the entries are ordered by their identity
                facets; for an explicit version they follow CSV row order.

        Raises:
            ValueError: If no store matches; the message names the facet that
                eliminated every row and lists the values that were available.
        """
        requested = {
            "activity_id": activity_id,
            "source_id": source_id,
            "experiment_id": experiment_id,
            "variable_id": variable_id,
            "table_id": table_id,
            "member_id": member_id,
            "grid_label": grid_label,
        }
        frame = self.frame
        for facet in _FILTER_FACETS:
            value = requested.get(facet)
            if value is None or facet not in frame.columns:
                continue
            narrowed = frame[frame[facet].astype(str) == str(value)]
            if narrowed.empty:
                available = sorted(frame[facet].dropna().astype(str).unique())
                raise ValueError(
                    f"no CMIP6 store matches {facet}={value!r} for the requested "
                    f"facets so far ({self._describe(requested, facet)}); "
                    f"{self._available_hint(facet, value, available)}"
                )
            frame = narrowed
        frame = self._select_version(frame, version)
        return [self._row_to_store(row) for _, row in frame.iterrows()]

    def _select_version(self, frame: pd.DataFrame, version: str) -> pd.DataFrame:
        """Apply the version policy — `latest` (newest per store) or exact.

        Args:
            frame: The facet-filtered frame.
            version: `"latest"` or an explicit version string.

        Returns:
            pandas.DataFrame: The frame reduced to the chosen version(s),
                sorted by the store slug facets.

        Raises:
            ValueError: If an explicit `version` matches nothing.
        """
        if "version" not in frame.columns:
            return frame
        if str(version).lower() != "latest":
            narrowed = frame[frame["version"].astype(str) == str(version)]
            if narrowed.empty:
                available = sorted(frame["version"].dropna().astype(str).unique())
                raise ValueError(
                    f"no CMIP6 store matches version={version!r}; "
                    f"available versions: {available}."
                )
            return narrowed
        keys = [f for f in _IDENTITY_FACETS if f in frame.columns]
        ordered = frame.sort_values("version", ascending=False)
        if keys:
            ordered = ordered.drop_duplicates(subset=keys, keep="first")
            ordered = ordered.sort_values(keys)
        return ordered

    @staticmethod
    def _row_to_store(row: Any) -> ResolvedStore:
        """Build a :class:`ResolvedStore` from one CSV row.

        Args:
            row: A `pandas.Series` for one store.

        Returns:
            ResolvedStore: The typed store descriptor.
        """
        return ResolvedStore(
            zstore=str(row["zstore"]),
            source_id=str(row.get("source_id", "")),
            experiment_id=str(row.get("experiment_id", "")),
            variable_id=str(row.get("variable_id", "")),
            table_id=str(row.get("table_id", "")),
            member_id=str(row.get("member_id", "")),
            grid_label=str(row.get("grid_label", "")),
            version=str(row.get("version", "")),
            activity_id=str(row.get("activity_id", "")),
        )

    @staticmethod
    def _describe(requested: dict[str, str | None], up_to: str) -> str:
        """Summarise the facets pinned before the one that failed.

        Args:
            requested: The full requested-facet mapping.
            up_to: The facet that eliminated every row.

        Returns:
            str: A `k=v` list of the facets applied before `up_to`, or
                `"no prior facets"` when it was the first.
        """
        applied = []
        for facet in _FILTER_FACETS:
            if facet == up_to:
                break
            value = requested.get(facet)
            if value is not None:
                applied.append(f"{facet}={value}")
        return ", ".join(applied) if applied else "no prior facets"

    @staticmethod
    def _available_hint(
        facet: str, value: str, available: list[str], limit: int = 20
    ) -> str:
        """Build a concise "available values" hint with a did-you-mean.

        Keeps the miss message readable on a high-cardinality facet (a
        `source_id` / `variable_id` miss can leave 100s of candidates) by
        capping the listed values and adding the closest match as a
        did-you-mean, mirroring the catalog's `difflib` lookups.

        Args:
            facet: The facet that eliminated every row.
            value: The requested (unmatched) value.
            available: The sorted values that were still available.
            limit: Maximum number of values to list before truncating.

        Returns:
            str: `available {facet}: [v1, …][, +K more]. Did you mean 'x'?`.
        """
        import difflib

        close = difflib.get_close_matches(str(value), available, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        shown = available[:limit]
        tail = f", +{len(available) - limit} more" if len(available) > limit else ""
        return f"available {facet}: {shown}{tail}.{hint}"
