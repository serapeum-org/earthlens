"""Catalog-tooling handlers for the ERDDAP backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). ERDDAP is a protocol spoken by
many independent servers, so the live "available" set is the union of each
curated `server_url`'s `allDatasets` table; every read is public.
"""

from __future__ import annotations

import re
from typing import Any, cast

from earthlens.cli.toolkit import (
    COVERAGE_BUCKETS,
    get_json,
    get_text,
    index_writer,
)

#: Persists a live dataset-id fetch into the bundled `available_datasets:` block.
writer = index_writer("available_datasets")

#: A `.dds` variable declaration: `<Type> <name>` followed by `[` (a dimensioned
#: grid/array) or `;` (a scalar/table column). Captures the name and skips the
#: `Dataset`/`Sequence`/`GRID`/`ARRAY`/`MAPS`/`}` structural lines.
_DDS_VARIABLE = re.compile(r"^\s*[A-Za-z]\w*\s+([A-Za-z]\w*)\s*[\[;]", re.MULTILINE)


def _dataset_ids(server_url: str) -> list[str]:
    """List every dataset id one ERDDAP server publishes.

    Reads the server's synthetic `allDatasets` table via tabledap JSON, asking
    only for the `datasetID` column; the `allDatasets` meta-row is dropped.

    Args:
        server_url: An ERDDAP base URL (a trailing slash is tolerated).

    Returns:
        The server's dataset ids, sorted and de-duplicated.
    """
    base = server_url.rstrip("/")
    body = get_json(f"{base}/tabledap/allDatasets.json?datasetID")
    rows = body["table"]["rows"]
    return sorted({row[0] for row in rows if row[0] != "allDatasets"})


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every dataset on each ERDDAP server the catalog curates from.

    The live "available" set is the union of the `allDatasets` table of each
    distinct `server_url` the curated rows reference, grouped per server so the
    diff shows which server a new id came from. Fails fast if any curated server
    is unreachable (a partial crawl under `--write` would desync the index).

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `{server_url: [dataset_id, …]}` for every distinct curated server.
    """
    servers = sorted({row.server_url for row in catalog.datasets.values()})
    return {server: _dataset_ids(server) for server in servers}


def variables_for(record: Any) -> set[str]:
    """Return the variable names an ERDDAP dataset serves live.

    Fetches the dataset's `.dds` (Dataset Descriptor Structure) and parses the
    declared names — the data columns of a tabledap `Sequence` and the grid
    variables plus dimensions of a griddap `Grid`. The catalog audit diffs a
    curated `variables:` list against this set to flag a variable the server
    stopped serving or re-cased (e.g. `wtmp` -> `WTMP`), which the id-level audit
    cannot see. Registered as the `variable_lister` role for `erddap`.

    Args:
        record: A curated ERDDAP `Dataset` row, carrying `server_url`,
            `protocol`, and `dataset_id`.

    Returns:
        The set of variable names the server declares for the dataset. This
        rests on ERDDAP destination names being `[A-Za-z][A-Za-z0-9_]*` (the
        `.dds` grammar the regex matches), so the set is a superset of the data
        variables and a curated name absent from it is real drift, while extra
        dimension names never cause a false positive. A variable the `.dds`
        grammar cannot express (e.g. a DAP structure type) is a known limitation
        — it would read as drift, not be silently mis-parsed.

    Raises:
        ValueError: If the response is not a DDS — a 200 carrying a maintenance
            or interstitial page — so the audit reports an errored fetch instead
            of parsing an empty set and flagging every curated variable as drift.
    """
    base = record.server_url.rstrip("/")
    url = f"{base}/{record.protocol}/{record.dataset_id}.dds"
    dds = get_text(url)
    if "Dataset {" not in dds:
        raise ValueError(f"{url} did not return a DDS")
    return set(_DDS_VARIABLE.findall(dds))


def _structures(catalog: Any) -> dict[str, str]:
    """Map every dataset id to its `dataStructure` across the curated servers.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `{dataset_id: "grid" | "table"}` across every curated server.
    """
    servers = sorted({row.server_url for row in catalog.datasets.values()})
    structures: dict[str, str] = {}
    for server in servers:
        base = server.rstrip("/")
        body = get_json(f"{base}/tabledap/allDatasets.json?datasetID,dataStructure")
        for dataset_id, structure in body["table"]["rows"]:
            if dataset_id != "allDatasets":
                structures[dataset_id] = structure
    return structures


def _classify(dataset_id: str, structure: str | None, curated: set[str]) -> str:
    """Bucket one ERDDAP dataset id by curation status and data structure.

    Args:
        dataset_id: The ERDDAP dataset id.
        structure: Its `dataStructure` (`"grid"` / `"table"`), or `None`.
        curated: The set of already-curated dataset ids.

    Returns:
        The bucket name (one of `COVERAGE_BUCKETS`).
    """
    if dataset_id in curated:
        return "DONE"
    if dataset_id.lower().startswith("test"):
        return "thin"
    if structure == "grid":
        return "addressable"
    if structure == "table":
        return "table"
    return "missing"


def coverage(catalog: Any) -> tuple[dict[str, int], list[str]]:
    """Classify every `available_datasets:` id of the ERDDAP catalog.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `(counts, todo)` — per-bucket counts and the sorted `addressable`
        (griddap) ids not yet curated.

    Raises:
        ValueError: If the `available_datasets:` index is empty.
    """
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    if not available:
        raise ValueError(
            "available_datasets: is empty — run `refresh erddap --write` first"
        )
    curated = set(catalog.datasets)
    structures = _structures(catalog)
    buckets: dict[str, list[str]] = {}
    for dataset_id in available:
        bucket = _classify(dataset_id, structures.get(dataset_id), curated)
        buckets.setdefault(bucket, []).append(dataset_id)
    counts = {bucket: len(buckets.get(bucket, [])) for bucket in COVERAGE_BUCKETS}
    return counts, sorted(buckets.get("addressable", []))


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Lint each ERDDAP row beyond the model's load-time checks.

    Flags a non-`http(s)` `server_url`, a griddap row with empty `dim_names`,
    and a `flux_variables` entry not among the row's default `variables`.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        if not row.server_url.startswith(("http://", "https://")):
            issues.append(f"{key}: server_url {row.server_url!r} is not an http(s) URL")
        if row.protocol == "griddap" and not row.dim_names:
            issues.append(
                f"{key}: griddap row has empty `dim_names` (no axes to subset)"
            )
        unknown_flux = [v for v in row.flux_variables if v not in row.variables]
        if unknown_flux:
            issues.append(
                f"{key}: flux_variables {unknown_flux} not in the row's default "
                f"variables {row.variables} (likely a typo)"
            )
    return len(catalog.datasets), issues


def _info_rows(server_url: str, dataset_id: str) -> list[list[Any]]:
    """Return one ERDDAP dataset's `/info` table rows.

    Args:
        server_url: The ERDDAP base URL (a trailing slash is tolerated).
        dataset_id: The dataset id on that server.

    Returns:
        The `table.rows` list from the `/info` JSON.
    """
    base = server_url.rstrip("/")
    body = get_json(f"{base}/info/{dataset_id}/index.json")
    return cast("list[list[Any]]", body["table"]["rows"])


def _global_attr(rows: list[list[Any]], name: str) -> str:
    """Return one `NC_GLOBAL` attribute value from `/info` rows (`""` if absent)."""
    for row in rows:
        if row[1] == "NC_GLOBAL" and row[2] == name:
            return str(row[4])
    return ""


def emitter(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an ERDDAP `datasets:` row from a server's `/info` metadata.

    The presence of grid `dimension` rows decides `protocol` (`griddap` if
    dimensioned, else `tabledap`), the `variable` rows give the default variable
    set, and the `NC_GLOBAL` `title` / `license` attributes fill the human
    metadata. The server is `--server` if given, else discovered by trying each
    curated `server_url`.

    Args:
        catalog: The loaded ERDDAP `Catalog` (its curated `server_url`s seed the
            server search).
        upstream_id: The ERDDAP dataset id to seed from.
        **opts: `server` (an explicit ERDDAP base URL to look the id up on).

    Returns:
        The seeded row.

    Raises:
        ValueError: If the id is not found on `--server` or any curated server.
    """
    server = opts.get("server")
    candidates = (
        [server]
        if server
        else sorted({row.server_url for row in catalog.datasets.values()})
    )
    rows: list[list[Any]] | None = None
    found_server = ""
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            rows = _info_rows(candidate, upstream_id)
            found_server = candidate
            break
        except Exception as exc:  # noqa: BLE001 — try the next server, report at end
            last_exc = exc
    if rows is None:
        raise ValueError(
            f"{upstream_id!r} not found on any known ERDDAP server "
            f"{candidates} (pass --server <url> to point elsewhere): {last_exc}"
        )
    dim_names = [row[1] for row in rows if row[0] == "dimension"]
    variables = [row[1] for row in rows if row[0] == "variable"]
    protocol = "griddap" if dim_names else "tabledap"
    row: dict[str, Any] = {
        "server_url": found_server,
        "dataset_id": upstream_id,
        "protocol": protocol,
    }
    if protocol == "griddap":
        row["dim_names"] = dim_names
    row["variables"] = variables
    row["title"] = _global_attr(rows, "title")
    row["license_note"] = _global_attr(rows, "license")
    return row
