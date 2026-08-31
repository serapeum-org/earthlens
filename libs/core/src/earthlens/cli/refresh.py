"""Live upstream-index refresh — the one *online* CLI operation.

Every other CLI command is strictly offline: it reads only the bundled
catalog YAML. `refresh` is the deliberate exception (the L4 design item):
it makes live HTTP requests to a provider's public API to fetch its
*current* list of datasets / collections, and diffs that against the
bundled `available_datasets` index so the user can see what has appeared
or disappeared upstream.

Only providers with a public listing endpoint (or public SDK call, or
anonymous FTP tree) have a refresher wired up in :data:`_REFRESHERS`; every
other provider reports `unsupported` so `refresh all` degrades gracefully
instead of failing. The live ids are diffed against the bundled index that
fits the provider — usually `available_datasets`, but a backend whose
refresh axis differs (Overture's `available_releases`, CHC's `ftp_bases`
paths, or radar / firms / fdsn whose `datasets` map *is* the index) resolves
its own via :func:`_bundled_ids`.

The `--write` half (:data:`_WRITERS`) persists a live fetch back into the
bundled informational index. For the sharded `_index.yaml` providers (and
HDX's gzipped sidecar) it rewrites the in-file block; for the providers
whose `available_*` attribute is *computed* from the curated rows at load
time (openaq, worldpop, usgs_water) it instead writes the full live universe
to a sibling `available_*.yaml` the runtime does not load (the maintainer /
docs artefact the tools used to produce). Only the few backends with no
machine-writable index at all (chc's curated slugs, fdsn / firms whose
`datasets` map *is* the catalog) stay read-only under `--write`.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import requests
import yaml

from earthlens._cli_tooling import config_table, dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog

#: HTTP timeout (seconds) for a single live-listing request.
_TIMEOUT = 30

#: Cap on STAC `/collections` pages followed via `rel="next"` — a guard
#: against a misbehaving endpoint paginating forever.
_MAX_PAGES = 50


@dataclass
class RefreshOutcome:
    """The result of refreshing one provider against its live index.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"` (live index fetched), `"unsupported"` (no live
            endpoint wired up), or `"error"` (the request failed).
        detail: A human-readable note — the failure reason for `"error"` /
            `"unsupported"`, empty for `"ok"`.
        live_count: Number of distinct ids the live endpoint returned.
        bundled_count: Number of ids in the bundled `available_datasets`.
        new_ids: Ids present live but absent from the bundled index.
        removed_ids: Ids in the bundled index but absent live.
        written: Path of the bundled catalog file rewritten under
            `--write`, or `""` when nothing was written.
    """

    provider: str
    status: str
    detail: str = ""
    live_count: int = 0
    bundled_count: int = 0
    new_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    written: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Project the outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A successful outcome carries the diff counts and id lists:

                ```python
                >>> from earthlens.cli.refresh import RefreshOutcome
                >>> outcome = RefreshOutcome(
                ...     "stac", "ok", live_count=3, bundled_count=2,
                ...     new_ids=["c"], removed_ids=[],
                ... )
                >>> outcome.to_dict()["status"]
                'ok'
                >>> outcome.to_dict()["new_ids"]
                ['c']

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "live_count": self.live_count,
            "bundled_count": self.bundled_count,
            "new_ids": self.new_ids,
            "removed_ids": self.removed_ids,
            "written": self.written,
        }


def _get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET `url` and return the parsed JSON body (raising on HTTP error).

    Args:
        url: The endpoint to fetch.
        headers: Optional request headers (e.g. an `X-API-Key` for a
            credentialed provider).
        params: Optional query parameters.

    Returns:
        The parsed JSON body. Typed as a mapping for the common case; the
        CADS `form.json` is sometimes a top-level list, which its one caller
        (the ecmwf emitter) narrows with an `isinstance` check.
    """
    response = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


def _redact(text: str, secret: str) -> str:
    """Mask `secret` (e.g. an API key) wherever it appears in `text`.

    Used to scrub a credential out of an error message before it is surfaced
    in an outcome `detail` — some providers (FIRMS) carry the key in the
    request URL, which `requests` echoes verbatim in `HTTPError`.

    Args:
        text: The message that may contain the secret.
        secret: The secret to mask (a no-op when empty).

    Returns:
        `text` with every occurrence of `secret` replaced by `***`.

    Examples:
        - A key embedded in a URL is masked:

            ```python
            >>> from earthlens.cli.refresh import _redact
            >>> _redact("for url: https://x/csv/SEKRET/all", "SEKRET")
            'for url: https://x/csv/***/all'

            ```
        - An empty secret leaves the text untouched:

            ```python
            >>> from earthlens.cli.refresh import _redact
            >>> _redact("nothing to hide", "")
            'nothing to hide'

            ```
    """
    return text.replace(secret, "***") if secret else text


def _index_path(info: BackendInfo) -> Path:
    """Return the bundled index file a provider's `--write` rewrites.

    Resolves the catalog's `CATALOG_PATH`: a sharded layout (a directory)
    keeps its informational index in `_index.yaml`; a single-file layout
    *is* the catalog file itself.

    Args:
        info: The backend whose catalog path to resolve.

    Returns:
        The `_index.yaml` under a sharded `catalog/` directory, or the
        single `<pkg>_data_catalog.yaml` file.
    """
    base = importlib.import_module(f"{info.module}.catalog").CATALOG_PATH
    return cast("Path", base / "_index.yaml" if base.is_dir() else base)


def _replace_index_block(path: Path, block_key: str, payload: Any) -> None:
    """Replace exactly one top-level block of a YAML index in place.

    Rewrites the `{block_key}:` block (from its key line up to the next
    column-zero key, or end of file) with `payload`, leaving every other
    block byte-for-byte intact — including the header comments above the
    block and any comment / blank lines that sit immediately above the next
    block (those belong to *it* and are preserved, not swallowed). This is
    what lets a provider whose `_index.yaml` holds more than one block
    (e.g. openEO's `available_collections:` *and* `available_processes:`)
    be rewritten without disturbing the sibling block or its comments.

    Args:
        path: The YAML index file to rewrite.
        block_key: The top-level key whose block is replaced.
        payload: The new value for `block_key` (a flat list, or a grouped
            mapping for backends that persist their index grouped).

    Raises:
        ValueError: If `path` has no `{block_key}:` block.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{block_key}:")),
        None,
    )
    if start is None:
        raise ValueError(f"no {block_key}: block in {path}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*:", lines[j]):
            end = j
            break
    # Comment / blank lines immediately above the next block belong to *it*,
    # not to the block being replaced — back the cut up over them so they are
    # preserved rather than swallowed into the rewritten span.
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    block = yaml.safe_dump(
        {block_key: payload},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    path.write_text("".join(lines[:start]) + block + "".join(lines[end:]), "utf-8")


def _index_writer(
    block_key: str, *, grouped: bool = False
) -> Callable[[BackendInfo, dict[str, list[str]]], str]:
    """Build a writer that persists a live fetch into a YAML index block.

    The returned writer flattens the grouped live fetch (or keeps it
    grouped, for backends that persist their index per-group) and splices
    it into the provider's `_index.yaml` via :func:`_replace_index_block`.

    Args:
        block_key: The index block to rewrite (`"available_datasets"` or
            `"available_collections"`).
        grouped: When `True`, persist the per-group mapping verbatim;
            when `False`, persist the flat sorted union.

    Returns:
        A `(info, grouped_ids) -> written_path` writer for `_WRITERS`.
    """

    def writer(info: BackendInfo, grouped_ids: dict[str, list[str]]) -> str:
        """Rewrite `info`'s index block from the live ids; return the file path."""
        path = _index_path(info)
        payload = grouped_ids if grouped else _flatten(grouped_ids)
        _replace_index_block(path, block_key, payload)
        return str(path)

    return writer


def _write_sibling_index(info: BackendInfo, filename: str, payload: Any) -> str:
    """Write an informational `available_*` index file next to the catalog.

    For the computed-index providers (openaq / worldpop / usgs_water) whose
    `available_*` attribute is derived from the curated rows at load time:
    `--write` persists the *full* live universe to a sibling YAML the
    runtime does not load (the maintainer / docs artefact the tools wrote).

    Args:
        info: The backend whose catalog directory receives the sibling.
        filename: The sibling file name (e.g. `available_parameters.yaml`).
        payload: The mapping to dump (already keyed by its block name).

    Returns:
        The path of the sibling index written.
    """
    path = _index_path(info).parent / filename
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def _get_text(url: str) -> str:
    """GET `url` and return the response body as text (raising on HTTP error)."""
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


#: Provider id -> a callable regenerating a bundled GIS artefact (not an
#: `available_*` index). Surfaced by `refresh <provider> --tiles`.
_TILE_REGENS: dict[str, Callable[[], tuple[str, int]]] = dispatch_table("tile_regen")


#: Provider id -> a callable taking the loaded catalog and returning its
#: live ids grouped (e.g. per STAC endpoint). Public providers need no
#: credentials; credentialed ones (openaq, firms) read their key from the env.
_REFRESHERS: dict[str, Callable[[Any], dict[str, list[str]]]] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **dispatch_table("refresher"),
}

#: Provider id -> a callable that persists a grouped live fetch back into
#: the bundled catalog (the `--write` half). A subset of `_REFRESHERS`:
#: providers whose informational index is computed from the curated rows at
#: load time (openaq, worldpop, usgs_water) have no on-disk block to rewrite
#: and intentionally report "live read only" instead.
_WRITERS: dict[str, Callable[[BackendInfo, dict[str, list[str]]], str]] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **dispatch_table("writer"),
}


def _flatten(grouped: dict[str, list[str]]) -> list[str]:
    """Flatten grouped live ids into one sorted, de-duplicated list.

    Args:
        grouped: A mapping of group name to its id list.

    Returns:
        The sorted union of every group's ids.

    Examples:
        - Ids are unioned and de-duplicated across groups:

            ```python
            >>> from earthlens.cli.refresh import _flatten
            >>> _flatten({"a": ["x", "y"], "b": ["y", "z"]})
            ['x', 'y', 'z']

            ```
    """
    return sorted({ident for ids in grouped.values() for ident in ids})


def supported_providers() -> list[str]:
    """Return the provider ids that have a live refresher wired up.

    Returns:
        The sorted provider ids `refresh` can fetch live.

    Examples:
        - The wired-up ids come back as a sorted list:

            ```python
            >>> from earthlens.cli.refresh import supported_providers
            >>> ids = supported_providers()
            >>> ids == sorted(ids)
            True

            ```
    """
    return sorted(_REFRESHERS)


def _diff(
    live: list[str], bundled: Iterable[str]
) -> tuple[int, int, list[str], list[str]]:
    """Compare a live id list to the bundled index.

    Args:
        live: Ids returned by the live endpoint.
        bundled: Ids from the bundled `available_datasets`.

    Returns:
        `(live_count, bundled_count, new_ids, removed_ids)` where `new_ids`
        are live-only and `removed_ids` are bundled-only, both sorted.

    Examples:
        - One new id appears upstream and one has disappeared:

            ```python
            >>> from earthlens.cli.refresh import _diff
            >>> _diff(["a", "b", "c"], ["a", "b", "x"])
            (3, 3, ['c'], ['x'])

            ```
    """
    live_set = set(live)
    bundled_set = {str(item) for item in bundled}
    return (
        len(live_set),
        len(bundled_set),
        sorted(live_set - bundled_set),
        sorted(bundled_set - live_set),
    )


@dataclass
class AuditOutcome:
    """The result of auditing one provider's curated catalog against live.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no live endpoint), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        live_count: Number of distinct ids served live.
        curated_count: Number of curated upstream ids checked.
        broken: Curated upstream ids no longer served live (actionable drift).
        untracked: Live ids absent from the bundled index (informational).
        variable_status: `"ok"` (variables were checked live), `"unsupported"`
            (the provider cannot enumerate a dataset's variables), or `"error"`
            (a variable fetch failed). Never a false `"ok"`.
        variable_drift: Curated `"<dataset>:<variable>"` pairs the provider no
            longer serves (a removed or re-cased variable) — actionable drift.
        variable_detail: Failure reason when `variable_status == "error"` (names
            the datasets whose variable fetch failed), else empty.
    """

    provider: str
    status: str
    detail: str = ""
    live_count: int = 0
    curated_count: int = 0
    broken: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    variable_status: str = "unsupported"
    variable_drift: list[str] = field(default_factory=list)
    variable_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Project the audit outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A drifted dataset shows up under `broken`:

                ```python
                >>> from earthlens.cli.refresh import AuditOutcome
                >>> AuditOutcome("stac", "ok", broken=["gone"]).to_dict()["broken"]
                ['gone']

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "live_count": self.live_count,
            "curated_count": self.curated_count,
            "broken": self.broken,
            "untracked": self.untracked,
            "variable_status": self.variable_status,
            "variable_drift": self.variable_drift,
            "variable_detail": self.variable_detail,
        }


def _curated_collection_ids(catalog: Any) -> list[str]:
    """Return the upstream `collection_id`s a catalog's records curate.

    Used by `audit` for backends whose curated keys are logical aliases
    (e.g. `sentinel-2-l2a`) distinct from the upstream id the provider
    actually serves (e.g. `SENTINEL2_L2A`), which lives in `collection_id`.
    """
    return sorted(
        {
            cid
            for record in catalog.datasets.values()
            if (cid := getattr(record, "collection_id", None))
        }
    )


def _curated_attr_ids(attr: str) -> Callable[[Any], list[str]]:
    """Build a curated-id resolver that reads `attr` off each record.

    Args:
        attr: The record attribute holding the upstream id (e.g. `"hdx_id"`,
            `"short_name"`).

    Returns:
        A function mapping a catalog to its sorted, de-duplicated upstream ids.
    """

    def resolver(catalog: Any) -> list[str]:
        """Return the catalog's sorted, de-duplicated `attr` upstream ids."""
        return sorted(
            {
                value
                for record in catalog.datasets.values()
                if (value := getattr(record, attr, None))
            }
        )

    return resolver


#: Provider id -> a callable returning the upstream ids the catalog curates
#: (for the `audit` drift check). Falls back to the dataset keys otherwise.
def _biodiversity_curated_ids(catalog: Any) -> list[str]:
    """Return the curated `available_datasets` index a cluster catalog tracks.

    Used by gbif / obis / wdpa / iucn — their refresh axis is the curated
    index plus the friendly aliases combined, which is what `_*_grouped`
    above also returns, so audit reports zero drift on a clean catalog.

    Args:
        catalog: The loaded cluster `Catalog`.

    Returns:
        The combined sorted set of `available_datasets` + `datasets` keys.
    """
    return sorted(set(catalog.available_datasets) | set(catalog.datasets))


_CURATED_IDS: dict[str, Callable[[Any], list[str]]] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **dispatch_table("curated_ids"),
}

#: Provider id -> a callable that, given one curated catalog record, returns the
#: set of variable names the provider serves for it live. A provider publishes
#: `variable_lister` only when its listing endpoint enumerates a dataset's
#: variables (e.g. erddap's `.dds`); providers without one report
#: `variable_status="unsupported"`, so the audit never claims false coverage.
_VARIABLE_LISTERS: dict[str, Callable[[Any], set[str]]] = {
    **dispatch_table("variable_lister"),
}

#: Provider id -> the catalog attribute holding its persisted informational
#: index. Defaults to `available_datasets`; Overture's refreshable axis is
#: its date-stamped `available_releases`, NWM's is its `available_configurations`.
_INDEX_ATTR: dict[str, str] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **config_table("index_attr"),
}

#: Provider id -> a callable computing the bundled ids to diff against, for
#: backends whose refresh axis is neither `available_datasets` nor a simple
#: attribute. CHC diffs the live FTP tree against its `ftp_bases` paths (not
#: the hand-curated `available_datasets:` slugs the diff cannot derive).
_BUNDLED_IDS: dict[str, Callable[[Any], list[str]]] = {
    # Discovered handlers only; core names no backend.
    **dispatch_table("bundled_ids"),
}


def _bundled_ids(catalog: Any, provider: str) -> list[str]:
    """Return the bundled ids a provider's live fetch is diffed against.

    Resolution order: an explicit `_BUNDLED_IDS` resolver (a computed axis
    such as CHC's `ftp_bases`); else the persisted informational index
    (`available_datasets`, or Overture's `available_releases`); else, for a
    backend whose `datasets` map *is* the index (radar / firms / fdsn), the
    curated ids (`_CURATED_IDS`) or, failing that, the dataset keys.

    Args:
        catalog: The loaded provider catalog.
        provider: The canonical provider id.

    Returns:
        The id list to diff the live fetch against.
    """
    custom = _BUNDLED_IDS.get(provider)
    if custom:
        return custom(catalog)
    persisted = [
        str(value)
        for value in getattr(
            catalog, _INDEX_ATTR.get(provider, "available_datasets"), []
        )
        or []
    ]
    if persisted:
        return persisted
    resolver = _CURATED_IDS.get(provider)
    return resolver(catalog) if resolver else [str(key) for key in catalog.datasets]


def _audit_variables(
    catalog: Any, provider: str, live: set[str] | None = None
) -> tuple[str, list[str], str]:
    """Diff each curated row's `variables` against what the provider serves live.

    The per-dataset fetch is guarded individually: one dataset's `.dds` failure
    (a transient blip, or a removed dataset whose `.dds` now 404s) is recorded
    but does not discard the drift already found for the other datasets, so a
    real re-casing is never lost because a sibling fetch failed.

    Args:
        catalog: The loaded provider `Catalog`.
        provider: Canonical provider id.
        live: The set of dataset ids the provider serves live, when known. A
            curated id absent from it is already reported as id-level `broken`
            drift, so it is skipped here — re-fetching a retired dataset's `.dds`
            only wastes a request and manufactures a phantom variable-audit error
            for what is really id drift. When `None`, every curated row is
            audited (the identity of a live id matches the catalog key only for
            providers whose keys are their served ids, currently the sole
            `variable_lister` provider, erddap).

    Returns:
        A `(variable_status, variable_drift, variable_detail)` triple.
        `variable_status` is `"unsupported"` when the provider has no
        variable-lister, `"error"` when any dataset's fetch failed (even so, the
        drift found for the datasets that did resolve is still returned), else
        `"ok"`; `variable_drift` holds the sorted `"<dataset>:<variable>"` pairs
        the provider no longer serves; `variable_detail` names the failed
        datasets (empty unless `variable_status == "error"`).
    """
    lister = _VARIABLE_LISTERS.get(provider)
    if lister is None:
        return "unsupported", [], ""
    drift: list[str] = []
    errors: list[str] = []
    for key, record in catalog.datasets.items():
        if live is not None and key not in live:
            continue
        curated = getattr(record, "variables", None) or []
        if not curated:
            continue
        try:
            served = lister(record)
        except Exception as exc:  # noqa: BLE001
            # A per-record fetch failure is reported, never raised, so one
            # dataset's `.dds` blip cannot discard the drift found for the rest.
            errors.append(f"{key}: {exc}")
            continue
        drift.extend(f"{key}:{name}" for name in curated if name not in served)
    status = "error" if errors else "ok"
    return status, sorted(drift), "; ".join(errors)


def audit_one(info: BackendInfo) -> AuditOutcome:
    """Audit a provider's curated catalog against its live index.

    Flags `broken` curated upstream ids the provider no longer serves (the
    actionable drift a `--strict` CI gate fails on) and, informationally,
    `untracked` live ids missing from the bundled index. Reuses the same
    live refresher as :func:`refresh_one`; providers without one report
    `"unsupported"`, and fetch failures report `"error"` — never raises.

    Args:
        info: The backend to audit.

    Returns:
        The :class:`AuditOutcome` for `info`.
    """
    lister = _REFRESHERS.get(info.provider)
    if lister is None:
        return AuditOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no public live-listing endpoint wired up",
        )
    try:
        catalog = load_catalog(info)
        grouped = lister(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return AuditOutcome(provider=info.provider, status="error", detail=str(exc))

    live = set(_flatten(grouped))
    curated_fn = _CURATED_IDS.get(info.provider)
    curated = set(curated_fn(catalog)) if curated_fn else set(catalog.datasets)
    index_attr = _INDEX_ATTR.get(info.provider, "available_datasets")
    available = {str(ident) for ident in getattr(catalog, index_attr, [])}
    variable_status, variable_drift, variable_detail = _audit_variables(
        catalog, info.provider, live
    )
    return AuditOutcome(
        provider=info.provider,
        status="ok",
        live_count=len(live),
        curated_count=len(curated),
        broken=sorted(curated - live),
        # Untracked = live ids earthlens tracks nowhere — neither curated nor
        # in the available index (so a provider whose index lives elsewhere,
        # like openaq's `parameters`, doesn't report its curated rows as drift).
        untracked=sorted(live - available - curated),
        variable_status=variable_status,
        variable_drift=variable_drift,
        variable_detail=variable_detail,
    )


#: The fixed coverage buckets a curation-coverage classifier reports, in
#: display order.
_COVERAGE_BUCKETS = ("DONE", "addressable", "thin", "table", "missing")


@dataclass
class CoverageOutcome:
    """The result of classifying a provider's available universe for curation.

    Distinct from :class:`AuditOutcome` (drift of curated-vs-live): coverage
    answers "of everything the provider exposes, how much is curated, and
    what is worth curating next".

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no classifier), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        counts: Per-bucket counts (see :data:`_COVERAGE_BUCKETS`).
        todo: The `addressable`-but-not-yet-curated ids worth curating next.
    """

    provider: str
    status: str
    detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    todo: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Project the coverage outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The per-bucket counts ride along under `counts`:

                ```python
                >>> from earthlens.cli.refresh import CoverageOutcome
                >>> CoverageOutcome("gee", "ok", counts={"DONE": 3}).to_dict()["counts"]
                {'DONE': 3}

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "counts": self.counts,
            "todo": self.todo,
        }


#: Provider id -> a callable returning `(counts, todo)` for `audit --coverage`.
#: Only providers with a discoverable available-universe distinct from their
#: curated rows (gee's STAC index, erddap's `allDatasets` crawl) qualify.
_COVERAGE: dict[str, Callable[[Any], tuple[dict[str, int], list[str]]]] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **dispatch_table("coverage"),
}


def coverage_one(info: BackendInfo) -> CoverageOutcome:
    """Classify a provider's available universe by curation status.

    Powers `audit --coverage`: walks the provider's `available_*` index and
    buckets each id (already curated / worth curating / out of scope / gone).
    Providers without a classifier report `"unsupported"`; fetch failures
    report `"error"` — never raises.

    Args:
        info: The backend to classify.

    Returns:
        The :class:`CoverageOutcome` for `info`.
    """
    classifier = _COVERAGE.get(info.provider)
    if classifier is None:
        return CoverageOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no curation-coverage classifier wired up for this provider",
        )
    try:
        catalog = load_catalog(info)
        counts, todo = classifier(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return CoverageOutcome(provider=info.provider, status="error", detail=str(exc))
    return CoverageOutcome(
        provider=info.provider, status="ok", counts=counts, todo=todo
    )


def refresh_one(info: BackendInfo, write: bool = False) -> RefreshOutcome:
    """Refresh one provider's live index, diff it, and optionally persist it.

    A provider with no registered refresher returns an `"unsupported"`
    outcome; any error fetching / parsing / writing returns an `"error"`
    outcome — neither raises, so `refresh all` never aborts.

    Args:
        info: The backend to refresh.
        write: When `True`, rewrite the bundled `available_*` index from the
            live fetch (providers without a writer report it in `detail`).
            As a seatbelt against a transient outage blanking a populated
            index, a write whose live fetch returned **no ids** is refused
            (the skip is reported in `detail`, not treated as an error).

    Returns:
        The :class:`RefreshOutcome` for `info`.
    """
    lister = _REFRESHERS.get(info.provider)
    if lister is None:
        return RefreshOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no public live-listing endpoint wired up",
        )
    try:
        catalog = load_catalog(info)
        grouped = lister(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return RefreshOutcome(provider=info.provider, status="error", detail=str(exc))

    live = _flatten(grouped)
    bundled = _bundled_ids(catalog, info.provider)
    live_count, bundled_count, new_ids, removed_ids = _diff(live, bundled)

    written = ""
    detail = ""
    if write:
        writer = _WRITERS.get(info.provider)
        if writer is None:
            detail = "live read only; --write is not supported for this provider"
        elif not live:
            # Seatbelt: an empty live fetch (transient outage, unexpected body,
            # an SDK returning nothing) must never overwrite a populated bundled
            # index with `[]`. Refuse the write and report it instead.
            detail = (
                "live fetch returned 0 ids; refusing to overwrite the index "
                "(re-run when the source is reachable, or edit by hand)"
            )
        else:
            try:
                written = writer(info, grouped)
            except Exception as exc:  # noqa: BLE001 — write failures are reported
                return RefreshOutcome(
                    provider=info.provider,
                    status="error",
                    detail=f"write failed: {exc}",
                    live_count=live_count,
                    bundled_count=bundled_count,
                    new_ids=new_ids,
                    removed_ids=removed_ids,
                )

    return RefreshOutcome(
        provider=info.provider,
        status="ok",
        detail=detail,
        live_count=live_count,
        bundled_count=bundled_count,
        new_ids=new_ids,
        removed_ids=removed_ids,
        written=written,
    )
