"""Bulk-hydrate placeholder Copernicus catalog rows from a live retrieve.

Powers `curate ecmwf --fill-empty --write`: for every curated ECMWF dataset
carrying a placeholder variable (`units: unknown`, the sentinel the seed step
writes), retrieve a tiny NetCDF via `cdsapi`, read each variable's real
`nc_variable` / `units`, and splice them into the existing stanza **in place** —
the comments and ordering of the surrounding rows are preserved, only the
placeholder fields are rewritten.

Credentialed and licence-gated: the CDS retrieve sits behind
:func:`_retrieve_netcdf_vars`, an isolated seam that keeps the
stanza-rewriting core (:func:`_rewrite_stanza`) pure and offline. A dataset
whose retrieve fails (unaccepted licence, CDS outage) is skipped, never fatal —
the fill is best-effort and partial by design (one retrieve confirms the
variable it sampled).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import typer
import yaml

#: Seconds to wait for one dataset's live retrieve before abandoning it. A
#: single request stuck in the CDS queue would otherwise wedge the whole pass,
#: so each retrieve runs under this deadline and a slow one is skipped.
_DEFAULT_RETRIEVE_TIMEOUT = 180.0

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
    }
)

#: Non-bounds auxiliary variable names (exact) — viewing angles, status flags.
_AUXILIARY_NAMES = frozenset(
    {
        "sza",
        "vza",
        "saa",
        "vaa",
        "record_status",
        "pixel_count",
        "quality_flag",
        "quality_flags",
    }
)
#: Auxiliary variable name suffixes: cell bounds, counts, flags, viewing angles.
_AUXILIARY_SUFFIXES = (
    "_bnds",
    "_bounds",
    "_count",
    "_status",
    "_flag",
    "_flags",
    "_zenith_angle",
    "_azimuth_angle",
    "_covered_hours",
)

#: One curated variable sub-block: a 6-space slug line + its 8-space body.
_VARIABLE_BLOCK = re.compile(
    r"(?m)^ {6}(?P<slug>[A-Za-z0-9][^\s:]*):[ \t]*\n"
    r"(?P<body>(?:^ {8}[^\n]*\n)*)"
)
#: The placeholder sentinel a seed writes for an un-hydrated variable.
_UNKNOWN_UNITS = re.compile(r"(?m)^ {8}units:[ \t]*unknown[ \t]*$")
#: The 8-space `nc_variable:` / `units:` keys rewritten inside a var sub-block
#: (the value after the key is replaced, a single space re-inserted).
_NC_VARIABLE_LINE = re.compile(r"(?m)^( {8}nc_variable:)[^\n]*$")
_UNITS_LINE = re.compile(r"(?m)^( {8}units:)[^\n]*$")


def _retrieve_netcdf_vars(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Retrieve a tiny NetCDF for `dataset_id` and read its variable metadata.

    The credentialed seam — delegates to the ECMWF deep sampler, which builds a
    minimal request from the dataset's constraints, retrieves it via `cdsapi`
    (`~/.cdsapirc`), and reads each NetCDF variable's `long_name` / `units`.
    Kept a module-level function so the one credentialed call is isolated
    from the pure rewriting logic around it.

    Args:
        dataset_id: The Copernicus dataset id to sample.

    Returns:
        Mapping of NetCDF short name to `{long_name, units}`.
    """
    from earthlens.ecmwf.cli import _ecmwf_deep_sample

    return _ecmwf_deep_sample(dataset_id)


def _retrieve_into(dataset_id: str, box: dict[str, Any]) -> None:
    """Thread body: run the retrieve, storing its result or error in `box`.

    Stores the variable-metadata mapping under `box["meta"]` on success, or the
    raised exception under `box["error"]` — read back by :func:`_retrieve_with_timeout`
    on the calling thread. Kept module-level (no closure) so the worker is
    picklable-simple and the no-nested-functions rule holds.

    Args:
        dataset_id: The Copernicus dataset id to sample.
        box: Mutable result cell shared with the caller thread.
    """
    try:
        box["meta"] = _retrieve_netcdf_vars(dataset_id)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller thread verbatim
        box["error"] = exc


def _retrieve_with_timeout(
    dataset_id: str, timeout: float | None
) -> dict[str, dict[str, Any]]:
    """Retrieve `dataset_id`'s variable metadata, abandoning it past `timeout`.

    Runs the blocking `cdsapi` retrieve in a daemon thread and waits at most
    `timeout` seconds. A request stuck in the CDS queue is abandoned — its
    daemon thread is left to die at process exit — instead of wedging the whole
    bulk pass; the retrieve builds its own `cdsapi.Client` and temp dir, so an
    abandoned one never clashes with the next. A falsy `timeout` waits with no
    deadline (the original un-bounded behaviour, used by the offline tests).

    Args:
        dataset_id: The Copernicus dataset id to sample.
        timeout: Seconds to wait, or `None` / `0` to wait without a deadline.

    Returns:
        Mapping of NetCDF short name to `{long_name, units}`.

    Raises:
        TimeoutError: If the retrieve does not finish within `timeout` seconds.
    """
    if not timeout:
        return _retrieve_netcdf_vars(dataset_id)
    box: dict[str, Any] = {}
    thread = threading.Thread(
        target=_retrieve_into, args=(dataset_id, box), daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"retrieve for {dataset_id!r} exceeded {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("meta") or {}


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
    """Return True for a coordinate / bounds / auxiliary NetCDF variable.

    These are never a data variable, so they must not be matched to a catalog
    slug (a wrong `nc_variable` silently mis-extracts at `aggregate=` time).
    Covers the explicit :data:`_COORD_NAMES` and :data:`_AUXILIARY_NAMES`, the
    observation-count prefix `nobs` / `n_obs`, and every :data:`_AUXILIARY_SUFFIXES`
    tail — cell bounds (`lat_bnds`), counts (`pixel_count`), status/quality flags,
    and solar/sensor viewing angles (`SZA`, `sensor_zenith_angle`).
    """
    lower = name.lower()
    return (
        lower in _COORD_NAMES
        or lower in _AUXILIARY_NAMES
        or lower.startswith(("nobs", "n_obs"))
        or lower.endswith(_AUXILIARY_SUFFIXES)
    )


def _assign_unique_subset(
    placeholders: list[str],
    candidates: dict[str, dict[str, Any]],
    chosen: dict[str, str],
    used: set[str],
) -> None:
    """Assign each slug that token-subset-matches EXACTLY ONE unused candidate.

    Iterated to a fixpoint: assigning a specific slug (`sea-surface-temperature`
    → `sst`) frees a shorter one (`temperature`) to become unique (→ `t`). A slug
    that stays ambiguous is never guessed. Mutates `chosen` / `used` in place.

    Args:
        placeholders: The still-`unknown` variable slugs, in catalog order.
        candidates: The `{nc_name: meta}` data variables (aux already dropped).
        chosen: The `slug -> nc_name` map so far (mutated in place).
        used: The set of already-claimed NetCDF names (mutated in place).
    """
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


#: Function words that carry no identifying signal. Dropped before the rule 4
#: overlap tests so `number-of-wet-days` cannot pair with a variable on `of`.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "per",
        "the",
        "to",
        "with",
    }
)


def _consume_initialism(rest: str, remaining: list[str]) -> bool:
    """Return True when `rest` splits into a leading piece of every `remaining` token.

    Backtracking search over token order, because the compressed form need not
    follow the slug's word order (`t2m` is `temperature` + `2m`). Succeeds only
    when `rest` is fully consumed AND every token contributed, so a near-miss
    prefix (`pressure` against `precipitation`) fails.

    Args:
        rest: The still-unconsumed tail of the NetCDF short name.
        remaining: The slug tokens not yet accounted for.

    Returns:
        True when a full assignment exists.
    """
    if not rest:
        return not remaining
    for index, token in enumerate(remaining):
        for size in range(1, len(token) + 1):
            if rest.startswith(token[:size]) and _consume_initialism(
                rest[size:], remaining[:index] + remaining[index + 1 :]
            ):
                return True
    return False


def _is_initialism(name: str, tokens: set[str]) -> bool:
    """Return True when `name` is `tokens` compressed to their leading letters.

    Covers the abbreviations a plain token-overlap test rejects: `sst` for
    `sea-surface-temperature`, `t2m` for `2m-temperature`.

    Args:
        name: The NetCDF short name.
        tokens: The slug's meaningful tokens.

    Returns:
        True when `name` is an initialism of `tokens`.
    """
    return bool(tokens) and _consume_initialism(name.lower(), sorted(tokens))


def _pair_is_evidenced(slug: str, name: str, meta: dict[str, Any]) -> bool:
    """Return True when `slug` and `name` share evidence of being the same thing.

    Rule 4's guard: being the only two left over is arity, not evidence. A pair
    qualifies on any one of three signals, cheapest first — a shared token with
    the short name, a shared token with its `long_name`, or `name` being an
    initialism of the slug. Slugs reduced to nothing but stopwords never
    qualify.

    Args:
        slug: The unmatched catalog variable slug.
        name: The unused NetCDF short name.
        meta: That variable's `{long_name, units}` metadata.

    Returns:
        True when the pairing is supported; False to keep the placeholder.
    """
    tokens = _tokens(slug) - _STOPWORDS
    if not tokens:
        return False
    if tokens & (_tokens(name) - _STOPWORDS):
        return True
    if tokens & (_tokens(str(meta.get("long_name") or "")) - _STOPWORDS):
        return True
    return _is_initialism(name, tokens)


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
       exactly one unused data variable (the single-variable retrieve), and
       only when the two carry evidence of being the same quantity
       (:func:`_pair_is_evidenced`) — arity alone is not a match.

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

    _assign_unique_subset(placeholders, candidates, chosen, used)

    unmatched = [slug for slug in placeholders if slug not in chosen]
    unused = [name for name in candidates if name not in used]
    # `all` is a pseudo-slug standing for "every variable this dataset serves",
    # so it never resembles a real name and is always the lone unmatched slug —
    # rule 4 would pair it with whatever single variable survived the auxiliary
    # filter, which is how a precipitation CDR acquired a coverage counter.
    #
    # Being the last two standing is arity, not evidence, so the pair must also
    # look like the same quantity. A plain token-overlap test was rejected for
    # dropping the abbreviations rule 4 gets right; `_pair_is_evidenced` keeps
    # them via the initialism arm (`sea-surface-temperature` -> `sst`).
    if (
        len(unmatched) == 1
        and len(unused) == 1
        and unmatched[0] != "all"
        and _pair_is_evidenced(unmatched[0], unused[0], candidates[unused[0]])
    ):
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
        rf"(?m)(^ {{6}}{re.escape(slug)}:[ \t]*\n(?:^ {{8}}[^\n]*\n)*)"
    )
    match = var_pat.search(block)
    if not match:
        return block
    sub = match.group(1)
    sub = _NC_VARIABLE_LINE.sub(lambda mo: f"{mo.group(1)} {nc_name}", sub, count=1)
    sub = _UNITS_LINE.sub(
        lambda mo: f"{mo.group(1)} {_yaml_value(units)}", sub, count=1
    )
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


def _hydrate_one(
    dataset_id: str,
    prefix: str,
    catalog_dir: Path,
    file_text: dict[Path, str],
    timeout: float | None,
) -> str:
    """Hydrate one placeholder dataset in place; return its outcome tag.

    Retrieves the dataset under `timeout`, matches the sampled variables into
    its shard, and — on a real change — updates `file_text` and writes the shard
    to disk immediately. Progress is echoed. Never raises for a licence-gated,
    unreachable, timed-out, or unmatchable dataset: those return a skip tag.

    Args:
        dataset_id: The Copernicus dataset id to hydrate.
        prefix: The `[k/total] id` label echoed with the outcome.
        catalog_dir: The `catalog/` shard directory to locate the stanza in.
        file_text: The per-shard text cache, updated in place on a hydration.
        timeout: Per-dataset retrieve deadline; `None` / `0` waits without one.

    Returns:
        One of `"hydrated"`, `"timed_out"`, or `"skipped"`.
    """
    try:
        nc_meta: dict[str, dict[str, Any]] | None = _retrieve_with_timeout(
            dataset_id, timeout
        )
    except TimeoutError:
        typer.echo(f"{prefix}: timed out, skipped")
        return "timed_out"
    except Exception:  # noqa: BLE001 — a licence-gated / unreachable dataset is skipped
        nc_meta = None
    path = _find_file_for_dataset(catalog_dir, dataset_id) if nc_meta else None
    if not nc_meta or path is None:
        typer.echo(f"{prefix}: skipped")
        return "skipped"
    if path not in file_text:
        file_text[path] = path.read_text(encoding="utf-8")
    new_text = _rewrite_stanza(file_text[path], dataset_id, nc_meta)
    if new_text == file_text[path]:
        typer.echo(f"{prefix}: skipped")
        return "skipped"
    file_text[path] = new_text
    path.write_text(new_text, encoding="utf-8")
    typer.echo(f"{prefix}: hydrated -> {path.name}")
    return "hydrated"


def bulk_hydrate_empty(
    limit: int | None = None,
    timeout: float | None = _DEFAULT_RETRIEVE_TIMEOUT,
) -> dict[str, Any]:
    """Fill every placeholder curated ECMWF row from a live retrieve, in place.

    Loads the curated catalog, finds datasets with any `units: unknown`
    variable, retrieves a tiny NetCDF per dataset, and rewrites the stanza in
    its per-family shard (preserving the rest). Hardened for the full-catalog
    sweep: each retrieve runs under `timeout` so one request stuck in the CDS
    queue is skipped rather than wedging the pass, and each hydrated shard is
    **written the moment it changes** so an interrupted run keeps the progress
    it already made. A dataset whose retrieve fails, times out, or whose stanza
    cannot be matched is skipped — never fatal. Progress is echoed per dataset.

    Args:
        limit: Only hydrate the first `limit` placeholder datasets (alphabetical).
        timeout: Per-dataset retrieve deadline in seconds; `None` / `0` waits
            without a deadline (the offline-test path).

    Returns:
        A summary `{candidates, hydrated, skipped, timed_out, filled}` mapping.
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

    total = len(empty)
    file_text: dict[Path, str] = {}
    hydrated = 0
    skipped = 0
    timed_out = 0
    filled: list[str] = []
    for index, dataset_id in enumerate(empty, start=1):
        prefix = f"[{index}/{total}] {dataset_id}"
        outcome = _hydrate_one(dataset_id, prefix, catalog_dir, file_text, timeout)
        if outcome == "hydrated":
            hydrated += 1
            filled.append(dataset_id)
        elif outcome == "timed_out":
            timed_out += 1
            skipped += 1
        else:
            skipped += 1

    clear_catalog_cache()
    return {
        "candidates": total,
        "hydrated": hydrated,
        "skipped": skipped,
        "timed_out": timed_out,
        "filled": filled,
    }
