"""Bulk-hydrate placeholder Copernicus catalog rows from a live retrieve.

Powers `curate ecmwf --fill-empty --write`: for every curated ECMWF dataset
carrying a placeholder variable (`units: unknown`, the sentinel the seed step
writes), retrieve a tiny NetCDF via `cdsapi`, read each variable's real
`nc_variable` / `units`, and splice them into the existing stanza **in place** —
the comments and ordering of the surrounding rows are preserved, only the
placeholder fields are rewritten.

Credentialed and licence-gated: the CDS retrieve sits behind
:func:`_retrieve_netcdf_vars` (monkeypatch-able), so the stanza-rewriting core
(:func:`_rewrite_stanza`) stays pure and fully testable offline. A dataset
whose retrieve fails (unaccepted licence, CDS outage) is skipped, never fatal —
the fill is best-effort and partial by design (one retrieve confirms the
variable it sampled).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

#: NetCDF coordinate / auxiliary variable names never matched to a data row.
_COORD_NAMES = frozenset(
    {
        "latitude",
        "longitude",
        "lat",
        "lon",
        "time",
        "valid_time",
        "forecast_period",
        "forecast_reference_time",
        "number",
        "expver",
        "realization",
        "step",
        "depth",
        "level",
        "pressure_level",
        "surface",
        "heightAboveGround",
        "x",
        "y",
        "spatial_ref",
        "crs",
        "bnds",
        "time_bnds",
    }
)

#: One curated variable sub-block: a 6-space slug line + its 8-space body.
_VARIABLE_BLOCK = re.compile(
    r"(?m)^      (?P<slug>[A-Za-z0-9][^\s:]*):[ \t]*\n"
    r"(?P<body>(?:^        [^\n]*\n?)*)"
)
#: The placeholder sentinel a seed writes for an un-hydrated variable.
_UNKNOWN_UNITS = re.compile(r"(?m)^        units:[ \t]*unknown[ \t]*$")
#: The 8-space `nc_variable:` / `units:` lines rewritten inside a var sub-block.
_NC_VARIABLE_LINE = re.compile(r"(?m)^(        nc_variable:[ \t]*).*$")
_UNITS_LINE = re.compile(r"(?m)^(        units:[ \t]*).*$")


def _retrieve_netcdf_vars(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Retrieve a tiny NetCDF for `dataset_id` and read its variable metadata.

    The credentialed seam — delegates to the ECMWF deep sampler, which builds a
    minimal request from the dataset's constraints, retrieves it via `cdsapi`
    (`~/.cdsapirc`), and reads each NetCDF variable's `long_name` / `units`.
    Wrapped as a module-level function so tests can monkeypatch it offline.

    Args:
        dataset_id: The Copernicus dataset id to sample.

    Returns:
        Mapping of NetCDF short name to `{long_name, units}`.
    """
    from earthlens.cli.curate import _ecmwf_deep_sample

    return _ecmwf_deep_sample(dataset_id)


def _tokens(text: str) -> set[str]:
    """Return the lowercased alphanumeric word tokens of a slug / long name."""
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def _yaml_value(value: str) -> str:
    """Render a string as the scalar YAML would emit after `key: ` (quoting as needed)."""
    dumped: str = yaml.safe_dump(
        {"x": value}, allow_unicode=True, default_flow_style=False
    )
    return dumped[len("x: ") :].rstrip("\n")


def _is_auxiliary(name: str) -> bool:
    """Return True for a coordinate / cell-bounds / auxiliary NetCDF variable.

    These are never a data variable, so they must not be matched to a catalog
    slug. Covers the explicit :data:`_COORD_NAMES` plus any `*_bnds` / `*_bounds`
    cell-bounds variable (e.g. `lat_bnds`, `time_bounds`).
    """
    lower = name.lower()
    return lower in _COORD_NAMES or lower.endswith(("_bnds", "_bounds"))


def _match_variables(
    placeholders: list[str], nc_meta: dict[str, dict[str, Any]]
) -> dict[str, tuple[str, str]]:
    """Confidently map each placeholder slug to a retrieved `(nc_name, units)`.

    A slug is hydrated only when it **confidently** identifies a data variable —
    a wrong `nc_variable` is worse than the `unknown` placeholder (it silently
    mis-extracts at `aggregate=` time). In order:

    1. Coordinate / bounds / auxiliary variables (see :func:`_is_auxiliary`) are
       dropped from the candidate set.
    2. Exact short-name equality — the slug's `cds_variable` form
       (`-`→`_`) equals a NetCDF variable name.
    3. Token-subset — the slug's tokens are a subset of a variable's
       `long_name` tokens.
    4. The **unambiguous** leftover case only: exactly one unmatched slug and
       exactly one unused data variable (the single-variable retrieve).

    Any slug that stays unmatched keeps its placeholder — there is no arbitrary
    order-based pairing among multiple candidates.

    Args:
        placeholders: The still-`unknown` variable slugs, in catalog order.
        nc_meta: The retrieved `{nc_name: {long_name, units}}` mapping.

    Returns:
        Mapping of slug to the `(nc_variable, units)` to write — only for the
        slugs that matched confidently.
    """
    candidates = {
        name: meta for name, meta in nc_meta.items() if not _is_auxiliary(name)
    }
    chosen: dict[str, str] = {}
    used: set[str] = set()

    for slug in placeholders:
        cds = slug.replace("-", "_")
        if cds in candidates and cds not in used:
            chosen[slug] = cds
            used.add(cds)

    # Token-subset, but only when the slug matches EXACTLY ONE unused candidate,
    # iterated to a fixpoint: assigning a specific slug (`sea-surface-temperature`
    # → `sst`) frees a shorter one (`temperature`) to become unique (→ `t`). A
    # slug that stays ambiguous is never guessed.
    progress = True
    while progress:
        progress = False
        for slug in placeholders:
            if slug in chosen:
                continue
            slug_tokens = _tokens(slug)
            if not slug_tokens:
                continue
            matches = [
                name
                for name, meta in candidates.items()
                if name not in used
                and slug_tokens <= _tokens(str(meta.get("long_name") or ""))
            ]
            if len(matches) == 1:
                chosen[slug] = matches[0]
                used.add(matches[0])
                progress = True

    unmatched = [slug for slug in placeholders if slug not in chosen]
    unused = [name for name in candidates if name not in used]
    if len(unmatched) == 1 and len(unused) == 1:
        chosen[unmatched[0]] = unused[0]

    return {
        slug: (name, str(candidates[name].get("units") or ""))
        for slug, name in chosen.items()
    }


def _placeholder_slugs(block: str) -> list[str]:
    """Return the slugs of variable sub-blocks still carrying `units: unknown`."""
    return [
        match.group("slug")
        for match in _VARIABLE_BLOCK.finditer(block)
        if _UNKNOWN_UNITS.search(match.group("body"))
    ]


def _fill_variable(block: str, slug: str, nc_name: str, units: str) -> str:
    """Rewrite one variable sub-block's `nc_variable:` / `units:` lines in place."""
    var_pat = re.compile(
        rf"(?m)(^      {re.escape(slug)}:[ \t]*\n(?:^        [^\n]*\n?)*)"
    )
    match = var_pat.search(block)
    if not match:
        return block
    sub = match.group(1)
    sub = _NC_VARIABLE_LINE.sub(lambda mo: mo.group(1) + nc_name, sub, count=1)
    sub = _UNITS_LINE.sub(lambda mo: mo.group(1) + _yaml_value(units), sub, count=1)
    return block[: match.start()] + sub + block[match.end() :]


def _rewrite_stanza(
    text: str, dataset_id: str, nc_meta: dict[str, dict[str, Any]]
) -> str:
    """Splice hydrated `nc_variable` / `units` into one dataset's stanza.

    Args:
        text: The full per-family catalog shard text.
        dataset_id: The dataset id whose stanza to rewrite.
        nc_meta: The retrieved `{nc_name: {long_name, units}}` mapping.

    Returns:
        The shard text with `dataset_id`'s placeholder variables filled in; the
        input is returned unchanged when the stanza, its placeholders, or a
        usable match are absent.
    """
    pattern = re.compile(
        rf"(?ms)^  {re.escape(dataset_id)}:\n(.*?)(?=^  [A-Za-z0-9/_.-]+:|\Z)"
    )
    match = pattern.search(text)
    if not match:
        return text
    block = match.group(1)
    placeholders = _placeholder_slugs(block)
    if not placeholders:
        return text
    assignments = _match_variables(placeholders, nc_meta)
    if not assignments:
        return text
    new_block = block
    for slug, (nc_name, units) in assignments.items():
        new_block = _fill_variable(new_block, slug, nc_name, units)
    if new_block == block:
        return text
    return (
        text[: match.start()] + f"  {dataset_id}:\n" + new_block + text[match.end() :]
    )


def _find_file_for_dataset(catalog_dir: Path, dataset_id: str) -> Path | None:
    """Return the per-family `catalog/*.yaml` shard holding `dataset_id`'s stanza."""
    head = re.compile(rf"(?m)^  {re.escape(dataset_id)}:\s*$")
    for path in sorted(catalog_dir.glob("*.yaml")):
        if path.name == "_index.yaml":
            continue
        if head.search(path.read_text(encoding="utf-8")):
            return path
    return None


def bulk_hydrate_empty(limit: int | None = None) -> dict[str, Any]:
    """Fill every placeholder curated ECMWF row from a live retrieve, in place.

    Loads the curated catalog, finds datasets with any `units: unknown`
    variable, retrieves a tiny NetCDF per dataset, and rewrites the stanza in
    its per-family shard (preserving the rest). A dataset whose retrieve fails
    or whose stanza cannot be matched is skipped — never fatal.

    Args:
        limit: Only hydrate the first `limit` placeholder datasets (alphabetical).

    Returns:
        A summary `{candidates, hydrated, skipped, filled}` mapping.
    """
    from earthlens.ecmwf import Catalog
    from earthlens.ecmwf.catalog import CATALOG_PATH, clear_catalog_cache

    catalog_dir = Path(CATALOG_PATH)
    catalog = Catalog()
    empty = sorted(
        key
        for key, ds in catalog.datasets.items()
        if any(var.units == "unknown" for var in ds.variables.values())
    )
    if limit:
        empty = empty[:limit]

    file_text: dict[Path, str] = {}
    dirty: set[Path] = set()
    hydrated = 0
    skipped = 0
    filled: list[str] = []
    for dataset_id in empty:
        try:
            nc_meta = _retrieve_netcdf_vars(dataset_id)
        except Exception:  # noqa: BLE001 — a licence-gated / unreachable dataset is skipped
            nc_meta = None
        path = _find_file_for_dataset(catalog_dir, dataset_id) if nc_meta else None
        if not nc_meta or path is None:
            skipped += 1
            continue
        if path not in file_text:
            file_text[path] = path.read_text(encoding="utf-8")
        new_text = _rewrite_stanza(file_text[path], dataset_id, nc_meta)
        if new_text != file_text[path]:
            file_text[path] = new_text
            dirty.add(path)
            hydrated += 1
            filled.append(dataset_id)
        else:
            skipped += 1

    for path in dirty:
        path.write_text(file_text[path], encoding="utf-8")
    clear_catalog_cache()
    return {
        "candidates": len(empty),
        "hydrated": hydrated,
        "skipped": skipped,
        "filled": filled,
    }
