"""Bulk-hydrate placeholder Copernicus catalog rows from a live retrieve.

Powers `curate ecmwf --fill-empty --write`: for every curated ECMWF dataset
carrying a placeholder variable (`units: unknown`, the sentinel the seed step
writes), retrieve a tiny NetCDF via `cdsapi`, read each variable's real
`nc_variable` / `units`, and splice them into the existing stanza **in place** —
the comments and ordering of the surrounding rows are preserved, only the
placeholder fields are rewritten.

Credentialed and licence-gated: the CDS retrieves sit behind
`_retrieve_variable_meta` and `_retrieve_netcdf_vars`, isolated seams that keep
the stanza-rewriting core pure and offline. A dataset whose retrieve fails
(unaccepted licence, CDS outage) is skipped, never fatal — the fill is
best-effort and resumable by design: each placeholder row is probed by name, a
dataset is abandoned after its first refusal, and the rows already filled are
written before the pass moves on.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
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

#: Fraction of a slug's meaningful tokens the shared ones must cover before
#: rule 4 treats the overlap as evidence. Half rejects the single-generic-word
#: coincidence (`land-sea-mask` against `msl`, sharing only `sea`) while keeping
#: the real one-of-two matches (`glacier-area` against `area`).
_MIN_TOKEN_COVERAGE = 0.5

#: The per-variable override key, read and written in several places.
_EXTRAS_KEY = "extras:"

#: Most retrieved variable names to name in a declined-match echo before
#: summarising the rest, so one wide product cannot flood the sweep's output.
_ECHO_MAX_NAMES = 8

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


def _probe_into(dataset_id: str, cds_variable: str, box: dict[str, Any]) -> None:
    """Thread body: probe one variable, storing its result or error in `box`.

    Catches `BaseException`, not `Exception`: anything raised in here has to
    reach the caller through the box, because a thread cannot propagate it and
    an empty box is indistinguishable from "no block serves this variable".

    Args:
        dataset_id: The Copernicus dataset id to sample.
        cds_variable: The `cds_variable` to request.
        box: Mutable result cell shared with the caller thread.
    """
    try:
        box["result"] = _retrieve_variable_meta(dataset_id, cds_variable)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller thread
        box["error"] = exc
    except BaseException as exc:
        # An interrupt has to reach the caller through the box as well: a thread
        # cannot propagate it, and an empty box is indistinguishable from "no
        # block serves this variable". Re-raised so this thread still unwinds.
        box["error"] = exc
        raise


def _probe_with_timeout(
    dataset_id: str, cds_variable: str, timeout: float | None
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Probe one variable under a deadline, so a stuck request cannot wedge the pass.

    Args:
        dataset_id: The Copernicus dataset id to sample.
        cds_variable: The `cds_variable` to request.
        timeout: Seconds to wait; `None` or `0` waits without one.

    Returns:
        The `(metadata, selectors)` pair for `cds_variable`.

    Raises:
        TimeoutError: If the probe does not finish within `timeout` seconds.

    Note:
        The worker is a daemon thread and keeps running its request after the
        deadline; the retrieve it holds is abandoned, not cancelled. At most one
        such thread is left per dataset, because the session stops probing after
        its first timeout.
    """
    if not timeout:
        return _retrieve_variable_meta(dataset_id, cds_variable)
    box: dict[str, Any] = {}
    thread = threading.Thread(
        target=_probe_into, args=(dataset_id, cds_variable, box), daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(
            f"probe of {cds_variable!r} in {dataset_id!r} exceeded {timeout:.0f}s"
        )
    if "error" in box:
        raise box["error"]
    return box.get("result") or ({}, {})


class _ProbeSession:
    """Per-variable probes for one dataset, abandoned after the first failure.

    A dataset's placeholders share one licence and one queue, so a refusal on
    the first variable will refuse the rest too. Recording the failure and
    returning empty for every later variable turns what would be N pointless
    retrieves into one, while the rows already filled in this pass are kept.

    Args:
        dataset_id: The Copernicus dataset id being hydrated.
        timeout: Per-probe deadline in seconds; `None` / `0` waits without one.

    Attributes:
        error: The failure that abandoned the session, or `None`.
        timed_out: Whether that failure was the deadline rather than the store.
        offered: Every data variable any probe in this session returned, so a
            dataset that hydrated nothing can report what it was actually given.
        answered: How many probes came back describing something. Zero means no
            constraints block names these rows at all, which is a different
            problem from a probe that answered with nothing usable.
    """

    def __init__(self, dataset_id: str, timeout: float | None) -> None:
        self.dataset_id = dataset_id
        self.timeout = timeout
        self.error: BaseException | None = None
        self.timed_out = False
        self.offered: set[str] = set()
        self.answered = 0

    def __call__(
        self, cds_variable: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Probe `cds_variable`, or return empty once the session has failed."""
        if self.error is not None:
            return {}, {}
        try:
            meta, selectors = _probe_with_timeout(
                self.dataset_id, cds_variable, self.timeout
            )
            self.offered.update(_data_variables(meta))
            self.answered += bool(meta)
            return meta, selectors
        except TimeoutError as exc:
            self.timed_out = True
            self.error = exc
        except Exception as exc:  # noqa: BLE001 — a licence-gated dataset is skipped
            self.error = exc
        return {}, {}


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


def _consume_initialism(
    rest: str,
    tokens: tuple[str, ...],
    mask: int,
    memo: dict[tuple[int, int], bool],
) -> bool:
    """Return True when `rest` splits into a leading piece of every token in `mask`.

    The memo gate. `rest` is always a suffix of the original name, so
    `(len(rest), mask)` identifies a search state exactly, and caching on it
    bounds the work at `O(2^len(tokens) * len(name) * len(tokens) *
    max_token_len)` — one state per `(suffix, mask)` pair, each scanning the
    still-unused tokens' prefixes — instead of the exponential-with-no-ceiling
    node count a plain backtracker walks when the tokens nest as prefixes of
    one another.

    Args:
        rest: The still-unconsumed tail of the NetCDF short name.
        tokens: Every slug token, indexed by bit position in `mask`.
        mask: Bits set for the tokens not yet accounted for.
        memo: Shared `(len(rest), mask) -> bool` cache for one search.

    Returns:
        True when a full assignment exists.
    """
    key = (len(rest), mask)
    cached = memo.get(key)
    if cached is None:
        cached = _search_initialism(rest, tokens, mask, memo)
        memo[key] = cached
    return cached


def _search_initialism(
    rest: str,
    tokens: tuple[str, ...],
    mask: int,
    memo: dict[tuple[int, int], bool],
) -> bool:
    """Try every still-unused token as the next piece of `rest`.

    The search is order-free, because the compressed form need not follow the
    slug's word order (`t2m` is `temperature` + `2m`), and succeeds only once
    `rest` is spent with no token left over.

    Args:
        rest: The still-unconsumed tail of the NetCDF short name.
        tokens: Every slug token, indexed by bit position in `mask`.
        mask: Bits set for the tokens not yet accounted for.
        memo: Shared cache threaded through the recursion.

    Returns:
        True when some assignment of the remaining tokens consumes `rest`.
    """
    if not rest:
        return mask == 0
    return any(
        _consume_token(rest, token, tokens, mask & ~(1 << index), memo)
        for index, token in enumerate(tokens)
        if mask & (1 << index)
    )


def _consume_token(
    rest: str,
    token: str,
    tokens: tuple[str, ...],
    mask: int,
    memo: dict[tuple[int, int], bool],
) -> bool:
    """Return True when some leading piece of `token` starts `rest` and the tail resolves.

    Every prefix length is tried, so a token may contribute one letter (`s` for
    `sea`) or all of itself (`2m`); a near-miss prefix fails because the whole
    of `rest` still has to be consumed.

    Args:
        rest: The still-unconsumed tail of the NetCDF short name.
        token: The slug token being tried at this position.
        tokens: Every slug token, indexed by bit position in `mask`.
        mask: Bits set for the tokens still unused after `token`.
        memo: Shared cache threaded through the recursion.

    Returns:
        True when `token` can start `rest` and the remainder resolves.
    """
    return any(
        rest.startswith(token[:size])
        and _consume_initialism(rest[size:], tokens, mask, memo)
        for size in range(1, len(token) + 1)
    )


def _is_initialism(name: str, tokens: set[str]) -> bool:
    """Return True when `name` is `tokens` compressed to their leading letters.

    Covers the abbreviations a plain token-overlap test rejects: `sst` for
    `sea-surface-temperature`, `t2m` for `2m-temperature`.

    On its own this says nothing about how many tokens are worth compressing.
    Given one token it reduces to "is `name` a prefix of that word", which
    reads `co` as an abbreviation of `co2`; the two-token minimum that makes
    the arm meaningful is applied by :func:`_pair_is_evidenced`, not here.

    Args:
        name: The NetCDF short name.
        tokens: The slug's meaningful tokens.

    Returns:
        True when `name` is an initialism of `tokens`.

    Examples:
        - The compressed form need not follow the slug's word order:

            ```python
            >>> from earthlens.ecmwf._hydrate import _is_initialism
            >>> _is_initialism("sst", {"sea", "surface", "temperature"})
            True
            >>> _is_initialism("t2m", {"2m", "temperature"})
            True

            ```
        - Every token must contribute, so a near-miss prefix fails:

            ```python
            >>> from earthlens.ecmwf._hydrate import _is_initialism
            >>> _is_initialism("pressure", {"precipitation"})
            False
            >>> _is_initialism("elevation", {"number", "wet", "days"})
            False

            ```
        - With a single token it is only a prefix test, which is why the
          caller requires two:

            ```python
            >>> from earthlens.ecmwf._hydrate import _is_initialism
            >>> _is_initialism("co", {"co2"})
            True

            ```
    """
    if not tokens:
        return False
    ordered = tuple(sorted(tokens))
    return _consume_initialism(name.lower(), ordered, (1 << len(ordered)) - 1, {})


def _pair_is_evidenced(slug: str, name: str, meta: dict[str, Any]) -> bool:
    """Return True when `slug` and `name` share evidence of being the same thing.

    Rule 4's guard: being the only two left over is arity, not evidence. A pair
    qualifies on either of two signals — the tokens it shares with the short
    name and `long_name` covering at least :data:`_MIN_TOKEN_COVERAGE` of the
    slug, or `name` being an initialism of the slug. Slugs reduced to nothing
    but stopwords never qualify.

    Coverage rather than mere overlap is what keeps a single generic word from
    passing as evidence: `land-sea-mask` shares only `sea` with mean sea level
    pressure, one token of three, and `sub-surface-runoff` only `surface` with
    surface net solar radiation. Measured over the curated catalog, requiring
    half the slug's tokens cuts the pairings rule 4 would get wrong from 111 to
    38, about two thirds.

    It costs the occasional real match whose names genuinely have little in
    common — a terrestrial water storage anomaly served as a liquid water
    equivalent thickness shares only `water` — and those rows keep their
    placeholder and are counted as unmatched, which is the cheaper failure.

    Coverage does not govern the initialism arm, which has no shared tokens to
    measure; that arm admits 34 of the 38 wrong pairings that remain, reading
    `msl` as m(ask) s(ea) l(and). Requiring it to consume tokens in slug order
    would remove some, but it also rejects `2m-temperature` -> `t2m`, so the
    residue is accepted and rule 4 stays a last resort behind the confident
    rules.

    The initialism arm needs at least two tokens. Compressing a single word
    leaves nothing but a prefix of it, which is far too weak to pair on: it
    would read `co` as evidence for `co2`, and `e` for `ethene`.

    The evidence is a filter, not a proof. A single shared token satisfies it,
    so two names that share a generic word can still pair; `reserved` narrows
    that where a hydrated sibling row already owns the name, but most stanzas
    reaching rule 4 have no hydrated sibling at all.

    Args:
        slug: The unmatched catalog variable slug.
        name: The unused NetCDF short name.
        meta: That variable's `{long_name, units}` metadata.

    Returns:
        True when the pairing is supported; False to keep the placeholder.

    Examples:
        - An initialism is evidence even with no shared token:

            ```python
            >>> from earthlens.ecmwf._hydrate import _pair_is_evidenced
            >>> _pair_is_evidenced("sea-surface-temperature", "sst", {})
            True

            ```
        - So are tokens shared with the variable's `long_name`, once they
          cover half the slug:

            ```python
            >>> from earthlens.ecmwf._hydrate import _pair_is_evidenced
            >>> meta = {"long_name": "Total precipitation depth"}
            >>> _pair_is_evidenced("total-precipitation", "zzz", meta)
            True

            ```
        - One generic word in common is coincidence, not evidence — `sea` is
          one of the slug's three tokens, short of the coverage bar:

            ```python
            >>> from earthlens.ecmwf._hydrate import _pair_is_evidenced
            >>> meta = {"long_name": "Mean sea level pressure"}
            >>> _pair_is_evidenced("land-sea-mask", "zzz", meta)
            False

            ```
        - The initialism arm is separate, and still reads `msl` as
          m(ask) s(ea) l(and) — coverage does not govern it:

            ```python
            >>> from earthlens.ecmwf._hydrate import _pair_is_evidenced
            >>> _pair_is_evidenced("land-sea-mask", "msl", {})
            True

            ```
        - Two unrelated names are not paired, whatever the arity:

            ```python
            >>> from earthlens.ecmwf._hydrate import _pair_is_evidenced
            >>> _pair_is_evidenced("number-of-wet-days", "elevation", {"units": "m"})
            False

            ```
    """
    tokens = _tokens(slug) - _STOPWORDS
    if not tokens:
        return False
    shared = tokens & (_tokens(name) - _STOPWORDS)
    shared |= tokens & (_tokens(str(meta.get("long_name") or "")) - _STOPWORDS)
    if len(shared) >= len(tokens) * _MIN_TOKEN_COVERAGE:
        return True
    return len(tokens) >= 2 and _is_initialism(name, tokens)


def _data_variables(
    nc_meta: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the retrieved variables a catalog row could name, aux dropped.

    The single definition of "what the matcher would consider", so the echo that
    reports a declined pairing lists exactly the variables it weighed.

    Args:
        nc_meta: The retrieved `{nc_name: {long_name, units}}` mapping.

    Returns:
        The subset that is not a coordinate, bound, or auxiliary variable.
    """
    return {name: meta for name, meta in nc_meta.items() if not _is_auxiliary(name)}


def _inline_mapping(line: str) -> dict[str, Any]:
    """Return the mapping an inline `key: {a: 1, b: 2}` line carries.

    Args:
        line: One `key: value` line whose value may be an inline mapping.

    Returns:
        The parsed mapping, empty when the value is not one.
    """
    value = line.split(":", 1)[1].strip()
    if not value.startswith("{"):
        return {}
    parsed = yaml.safe_load(value)
    return parsed if isinstance(parsed, dict) else {}


def _extras_span(lines: list[str]) -> tuple[int, int] | None:
    """Locate a variable sub-block's own `extras:` and everything nested under it.

    Args:
        lines: One variable sub-block's lines.

    Returns:
        The `(start, stop)` half-open line range covering the override, or
        `None` when the row has none.
    """
    found = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith(_EXTRAS_KEY) and _indent_of(line) == 8
        ),
        None,
    )
    if found is None:
        return None
    stop = found + 1
    while stop < len(lines) and (
        not lines[stop].strip() or _indent_of(lines[stop]) > 8
    ):
        stop += 1
    return found, stop


def _existing_override(
    lines: list[str], span: tuple[int, int]
) -> dict[str, Any] | None:
    """Read the override a row already carries, whichever shape it is written in.

    Args:
        lines: One variable sub-block's lines.
        span: The `(start, stop)` range :func:`_extras_span` found.

    Returns:
        The parsed override, or `None` when the value is not a mapping at all
        and therefore must not be merged into or replaced.
    """
    head = lines[span[0]]
    if head.strip() == _EXTRAS_KEY:
        return _mapping_under(lines, span[0])
    inline = _inline_mapping(head)
    return inline or None


def _fill_variable_extras(block: str, slug: str, override: dict[str, Any]) -> str:
    """Write a per-variable `extras:` override into one variable sub-block.

    Merges into an existing override rather than replacing it, so a selector a
    maintainer set by hand survives unless the probe contradicts it. Whatever
    shape that override is written in — a block mapping, a block sequence value,
    or an inline mapping — it is parsed and re-emitted as one canonical block.
    Editing it line by line instead is what breaks: removing a superseded key's
    line leaves its `- item` continuation lines orphaned, and appending beside an
    inline mapping leaves the row with two `extras:` keys. Both produce a shard
    the catalog cannot load.

    An inline value that is not a mapping at all (`extras: [a, b]`, `extras:
    null`) is left untouched: it cannot be merged into, and replacing it would
    delete what a maintainer wrote.

    Comments inside a variable's own `extras:` do not survive the rewrite. That
    block is machine-managed; the dataset-level `extras:` where the catalog keeps
    its explanatory comments is never touched.

    Args:
        block: One dataset stanza's body text.
        slug: The variable sub-block to edit.
        override: Selector keys and values to record.

    Returns:
        The stanza body with the override written; unchanged when `slug` is
        absent, `override` is empty, or the row's existing value cannot be read.
    """
    if not override:
        return block
    for match in _VARIABLE_BLOCK.finditer(block):
        if match.group("slug") != slug:
            continue
        lines = match.group("body").splitlines(keepends=True)
        span = _extras_span(lines)
        merged: dict[str, Any] = {}
        head: list[str]
        tail: list[str]
        if span is None:
            head, tail = lines, []
        else:
            existing = _existing_override(lines, span)
            if existing is None:
                return block
            merged = existing
            head, tail = lines[: span[0]], lines[span[1] :]
        merged.update(override)
        rendered = [f"        {_EXTRAS_KEY}\n"] + [
            f"          {key}: {_yaml_inline_list(value)}\n"
            for key, value in merged.items()
        ]
        body = "".join(head + rendered + tail)
        return block[: match.start("body")] + body + block[match.end("body") :]
    return block


def _hydrate_stanza_per_variable(
    text: str,
    dataset_id: str,
    probe: Callable[[str], tuple[dict[str, dict[str, Any]], dict[str, Any]]],
) -> tuple[str, list[str], list[str]]:
    """Fill every placeholder of one stanza, probing each variable on its own.

    One probe per placeholder is what lets a multi-variable dataset finish: the
    single-request sampler only ever reveals the variable its constraints list
    first, so a stanza with several placeholders could never be completed. Each
    probe asks for one named variable, so a lone data variable coming back
    identifies that row outright rather than being matched by name similarity,
    and the serving block's selectors are recorded as a per-variable override
    when they differ from the stanza's defaults.

    Names bound by rows filled earlier in the same pass are withheld from the
    leftover rule, so a guess cannot land on a name another row already holds.
    The confident rules may still share one — a short name legitimately serves
    several rows where CARRA repeats it across level families.

    Args:
        text: The full per-family catalog shard text.
        dataset_id: The dataset id whose stanza to hydrate.
        probe: Callable taking a `cds_variable` and returning the
            `(metadata, selectors)` pair :func:`_retrieve_variable_meta` returns.

    Returns:
        A `(text, filled, declined)` triple — the rewritten shard text, the
        slugs hydrated, and the slugs left as placeholders.
    """
    match = _stanza_match(text, dataset_id)
    if match is None:
        return text, [], []
    block = match.group(1)
    placeholders = _placeholder_slugs(block)
    if not placeholders:
        return text, [], []
    cds_names = _slug_cds_variables(block)
    dataset_extras = _dataset_extras(block)
    new_block = block
    filled: list[str] = []
    declined: list[str] = []
    for slug in placeholders:
        cds_variable = cds_names.get(slug)
        if cds_variable is None:
            declined.append(slug)
            continue
        meta, selectors = probe(cds_variable)
        chosen = _choose_for_slug(slug, meta, _claimed_nc_names(new_block))
        if chosen is None:
            declined.append(slug)
            continue
        name, units = chosen
        new_block = _fill_variable(new_block, slug, name, units)
        new_block = _fill_variable_extras(
            new_block, slug, _selector_override(selectors, dataset_extras)
        )
        filled.append(slug)
    if new_block == block:
        return text, filled, declined
    rewritten = (
        text[: match.start()] + f"  {dataset_id}:\n" + new_block + text[match.end() :]
    )
    return rewritten, filled, declined


def _choose_for_slug(
    slug: str,
    meta: dict[str, dict[str, Any]],
    reserved: frozenset[str],
) -> tuple[str, str] | None:
    """Pick the NetCDF variable a single-variable probe identifies for `slug`.

    When the probe asked for one variable and one data variable came back, the
    correspondence is established by the request rather than inferred from the
    names, so no evidence check is needed. Anything else falls back to the
    ordinary matcher, which declines rather than guess.

    Args:
        slug: The placeholder slug being hydrated.
        meta: The probe's `{nc_name: {long_name, units}}` mapping.
        reserved: Lowercased names already bound by hydrated rows.

    Returns:
        The `(nc_variable, units)` to write, or `None` to keep the placeholder.
    """
    candidates = {
        name: info
        for name, info in _data_variables(meta).items()
        if name.lower() not in reserved
    }
    if len(candidates) == 1:
        name, info = next(iter(candidates.items()))
        return name, str(info.get("units") or "")
    matched = _match_variables([slug], meta, reserved)
    return matched.get(slug)


def _retrieve_variable_meta(
    dataset_id: str, cds_variable: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Probe one variable of a dataset; the second credentialed seam.

    Mirrors :func:`_retrieve_netcdf_vars` but asks for a named variable, so a
    dataset with several placeholders can be finished one row at a time instead
    of only ever revealing whichever variable its constraints list first.

    Args:
        dataset_id: The Copernicus dataset id to sample.
        cds_variable: The `cds_variable` to request.

    Returns:
        A `(metadata, selectors)` pair as :func:`_ecmwf_deep_sample_variable`
        returns them.
    """
    from earthlens.ecmwf.cli import _ecmwf_deep_sample_variable

    return _ecmwf_deep_sample_variable(dataset_id, cds_variable)


def _slug_cds_variables(block: str) -> dict[str, str]:
    """Map each variable slug in a stanza to its `cds_variable`.

    A probe has to ask for the request-side name, which is not derivable from
    the slug — `average-river-discharge-in-the-last-24-hours` is a slug whose
    `cds_variable` differs from a naive de-hyphenation in several families.

    Args:
        block: One dataset stanza's body text.

    Returns:
        Mapping of slug to `cds_variable`, omitting rows that declare none.
    """
    names: dict[str, str] = {}
    for match in _VARIABLE_BLOCK.finditer(block):
        for line in match.group("body").splitlines():
            stripped = line.strip()
            if stripped.startswith("cds_variable:"):
                value = _scalar_after_key(line)
                if value:
                    names[match.group("slug")] = value
                break
    return names


def _indent_of(line: str) -> int:
    """Return the number of leading spaces on `line`."""
    return len(line) - len(line.lstrip(" "))


def _mapping_under(lines: list[str], start: int) -> dict[str, Any]:
    """Read the mapping nested under `lines[start]` by parsing it as YAML.

    The region is dedented and handed to the parser rather than scanned line by
    line, so a nested mapping stays nested, a block sequence stays a list, and a
    quoted value containing a `#` is not mistaken for a comment. Scanning was
    tried and gets all three wrong — the last of them by raising out of the
    sweep rather than returning something wrong.

    Args:
        lines: The stanza's lines.
        start: Index of the parent key line.

    Returns:
        The parsed mapping; empty when the region is absent or unparseable.
    """
    parent = _indent_of(lines[start])
    region = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent_of(line) <= parent:
            break
        region.append(line)
    if not any(line.strip() for line in region):
        return {}
    indent = min(
        (_indent_of(line) for line in region if line.strip()), default=parent + 1
    )
    # Callers split with and without `keepends`, so re-join explicitly rather
    # than trusting the lines to carry their own terminators.
    dedented = chr(10).join(
        (line[indent:] if len(line) > indent else line.lstrip(" ")).rstrip(chr(10))
        for line in region
    )
    try:
        parsed = yaml.safe_load(dedented)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dataset_extras(block: str) -> dict[str, str]:
    """Return the stanza's dataset-level `extras:` mapping, as parsed values.

    Reads either spelling the catalog allows — a block mapping under `extras:`
    or an inline one on the key's own line — so a selector is compared by value
    rather than by how it happens to be written.

    Args:
        block: One dataset stanza's body text.

    Returns:
        Mapping of extra key to its parsed value; empty when the stanza has no
        dataset-level `extras:`.
    """
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(_EXTRAS_KEY) and _indent_of(line) == 4:
            if line.strip() != _EXTRAS_KEY:
                return _inline_mapping(line)
            return _mapping_under(lines, index)
    return {}


def _selector_override(
    selectors: dict[str, Any], dataset_extras: dict[str, str]
) -> dict[str, Any]:
    """Return the selectors a variable needs that the stanza does not already set.

    Only a selector the dataset-level `extras:` disagrees with is worth writing
    as a per-variable override; anything the stanza already sets identically
    would be noise. This is what turns the GloFAS `timespan` split — discharge
    under `time_mean`, snow depth under `instantaneous` — into a recorded row
    override instead of a hand-written comment.

    Args:
        selectors: The serving constraints block's selectors for this variable.
        dataset_extras: The stanza's dataset-level `extras:` mapping, as parsed
            values so that re-spelling a list does not read as a disagreement.

    Returns:
        The subset of `selectors` that differs from `dataset_extras`, keyed the
        same way; empty when the dataset defaults already cover the variable.

    Examples:
        - A selector the stanza sets differently is worth recording:

            ```python
            >>> from earthlens.ecmwf._hydrate import _selector_override
            >>> _selector_override(
            ...     {"timespan": ["instantaneous"]}, {"timespan": ["time_mean"]}
            ... )
            {'timespan': ['instantaneous']}

            ```
        - One the stanza already agrees with would be noise. Both sides are
          parsed values, so re-spelling the stanza's own list does not make it
          look like a disagreement:

            ```python
            >>> from earthlens.ecmwf._hydrate import _selector_override
            >>> _selector_override(
            ...     {"timespan": ["time_mean"]}, {"timespan": ["time_mean"]}
            ... )
            {}

            ```
        - And one the stanza never declares is not this row's business:

            ```python
            >>> from earthlens.ecmwf._hydrate import _selector_override
            >>> _selector_override({"hyear": ["2020"]}, {"timespan": ["time_mean"]})
            {}

            ```
    """
    override: dict[str, Any] = {}
    for key, value in selectors.items():
        if key not in dataset_extras:
            continue
        if dataset_extras[key] != value:
            override[key] = value
    return override


def _yaml_inline_list(value: Any) -> str:
    """Render a selector value as the inline YAML the catalog writes.

    Dumped by the YAML emitter, not by `str`, so a value that needs quoting
    survives the round trip. Rendering `["yes"]` as `[yes]` reads back as a
    boolean, `["1.0"]` as a float, and `["value #hash"]` does not parse at all.

    Args:
        value: The selector value from a constraints block.

    Returns:
        The inline YAML text for `value`.

    Examples:
        - A selector list becomes an inline sequence, matching the shipped rows:

            ```python
            >>> from earthlens.ecmwf._hydrate import _yaml_inline_list
            >>> _yaml_inline_list(["instantaneous"])
            '[instantaneous]'

            ```
        - A value that YAML would otherwise re-read as another type is quoted:

            ```python
            >>> from earthlens.ecmwf._hydrate import _yaml_inline_list
            >>> _yaml_inline_list(["yes"])
            "['yes']"

            ```
    """
    dumped = str(
        yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6)
    ).strip()
    if dumped.endswith("..."):
        dumped = dumped[: -len("...")].strip()
    return dumped


def _folded_match(
    cds: str, candidates: dict[str, dict[str, Any]], used: set[str]
) -> str | None:
    """Return the one candidate whose name differs from `cds` only in case.

    A catalog slug is always lowercase while a NetCDF short name keeps whatever
    case the producer chose, so a leaf CDR offers `FAPAR` and `LAI` against
    slugs `fapar` and `lai`. Those are the same name, but exact equality does
    not see it, and the row stays a placeholder for a difference that carries
    no meaning.

    Case is all that may differ: a name reached this way is the requested one
    spelled differently, not a near neighbour, so `FAPAR_ERR` and `FAPAR_QFLAG`
    stay out of reach. Two candidates that fold together are ambiguous and both
    are declined, because guessing between them is the mis-extraction this
    matcher exists to avoid.

    Args:
        cds: The slug's `cds_variable` form.
        candidates: The retrieved data variables.
        used: Names already claimed in this pass.

    Returns:
        The single case-insensitive match, or None.
    """
    folded = [
        name for name in candidates if name not in used and name.lower() == cds.lower()
    ]
    return folded[0] if len(folded) == 1 else None


def _match_variables(
    placeholders: list[str],
    nc_meta: dict[str, dict[str, Any]],
    reserved: frozenset[str] = frozenset(),
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
       exactly one unused data variable (the single-variable retrieve), the
       variable not already claimed by a hydrated row, and only when the two
       carry evidence of being the same quantity (`_pair_is_evidenced`) —
       arity alone is not a match.

    Any slug that stays unmatched keeps its placeholder — there is no arbitrary
    order-based pairing among multiple candidates.

    Args:
        placeholders: The still-`unknown` variable slugs, in catalog order.
        nc_meta: The retrieved `{nc_name: {long_name, units}}` mapping.
        reserved: Lowercased NetCDF names already written into the stanza's
            hydrated rows.
            Rule 4 will not hand one of these to a second slug; the confident
            rules still may, because one short name legitimately serves several
            rows of the same dataset (CARRA repeats a name across level
            families). The asymmetry is deliberate: reaching a repeated name by
            exact match or `long_name` is evidence it belongs to both rows,
            while reaching it by the leftover rule is a guess, and a guess
            landing on a name another row already holds is the corruption this
            rule was tightened to stop. The cost is that a legitimately
            repeated name reachable ONLY by rule 4 stays a placeholder, to be
            curated by hand.

    Returns:
        Mapping of slug to the `(nc_variable, units)` to write — only for the
        slugs that matched confidently.
    """
    candidates = _data_variables(nc_meta)
    chosen: dict[str, str] = {}
    used: set[str] = set()

    for slug in placeholders:
        cds = slug.replace("-", "_")
        if cds in candidates and cds not in used:
            chosen[slug] = cds
            used.add(cds)
            continue
        folded = _folded_match(cds, candidates, used)
        if folded is not None:
            chosen[slug] = folded
            used.add(folded)

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
    # dropping the abbreviations rule 4 gets right; the initialism arm keeps
    # them (`sea-surface-temperature` -> `sst`). It resolves the common shapes,
    # not abbreviation in general — a selective contraction like `msl` or `u10`
    # still fails it, and such a slug simply keeps its placeholder.
    #
    # `reserved` narrows the damage where a hydrated sibling row already owns
    # the name, but it is not a general guard: of the stanzas this rule can
    # reach in the shipped catalog, most have no hydrated sibling at all and so
    # reserve nothing. The evidence check is what stands between a placeholder
    # and a wrong name there, and one shared generic word satisfies it. Rule 4
    # is a last resort behind the confident rules for exactly that reason.
    if (
        len(unmatched) == 1
        and len(unused) == 1
        and unmatched[0] != "all"
        and unused[0].lower() not in reserved
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


def _claimed_nc_names(block: str) -> frozenset[str]:
    """Return the `nc_variable` values already bound by the stanza's hydrated rows.

    A placeholder row carries a seeded `nc_variable` too, so only sub-blocks
    that have lost the `units: unknown` sentinel count as having claimed a name.
    Values are returned lowercased, because NetCDF short names vary in case
    across products (`SST` and `sst` name one variable) and reservation has to
    match the way the rest of this module compares names.

    Args:
        block: One dataset stanza's body text.

    Returns:
        The lowercased NetCDF short names a hydrated row of this stanza uses.
    """
    claimed = set()
    for match in _VARIABLE_BLOCK.finditer(block):
        body = match.group("body")
        if _UNKNOWN_UNITS.search(body):
            continue
        line = _NC_VARIABLE_LINE.search(body)
        if line is None:
            continue
        value = _scalar_after_key(body[line.start() : line.end()])
        if value:
            claimed.add(value.lower())
    return frozenset(claimed)


def _scalar_after_key(line: str) -> str:
    """Return the plain scalar a `key: value` line carries, or `""` for none.

    Handles the two shapes the catalog emits — a bare scalar with an optional
    trailing `#` comment, and a quoted scalar — and rejects the YAML nulls, so
    an empty or `null` `nc_variable` never reserves a name.

    Args:
        line: One `key: value` line, without its newline.

    Returns:
        The unquoted, comment-free value, or `""` when there is none.
    """
    raw = line.split(":", 1)[1].strip()
    if raw[:1] in tuple('"\''):
        quote = raw[0]
        end = raw.find(quote, 1)
        raw = raw[1:end] if end > 0 else raw.lstrip(quote)
    else:
        # A YAML inline comment needs whitespace before the `#`.
        raw = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    return "" if raw.lower() in {"", "null", "~"} else raw


def _fill_variable(block: str, slug: str, nc_name: str, units: str) -> str:
    """Rewrite one variable sub-block's `nc_variable:` / `units:` lines in place.

    Both values are written through the YAML emitter. A NetCDF short name is not
    safe to interpolate raw: nitrogen monoxide is `no`, which YAML reads back as
    the boolean `False` and the catalog then refuses to load.
    """
    var_pat = re.compile(
        rf"(?m)(^ {{6}}{re.escape(slug)}:[ \t]*\n(?:^ {{8}}[^\n]*\n)*)"
    )
    match = var_pat.search(block)
    if not match:
        return block
    sub = match.group(1)
    sub = _NC_VARIABLE_LINE.sub(
        lambda mo: f"{mo.group(1)} {_yaml_value(nc_name)}", sub, count=1
    )
    sub = _UNITS_LINE.sub(
        lambda mo: f"{mo.group(1)} {_yaml_value(units)}", sub, count=1
    )
    return block[: match.start()] + sub + block[match.end() :]


def _stanza_match(text: str, dataset_id: str) -> re.Match[str] | None:
    """Return the match spanning one dataset stanza, or None when it is absent.

    Args:
        text: The full per-family catalog shard text.
        dataset_id: The dataset id whose stanza to isolate.

    Returns:
        The `re.Match` spanning the stanza, or `None` if `dataset_id` is absent.
        Group 1 is the stanza body; the span is where a rewrite splices back.
    """
    pattern = re.compile(
        rf"(?ms)^  {re.escape(dataset_id)}:\n(.*?)(?=^  [A-Za-z0-9/_.-]+:|\Z)"
    )
    return pattern.search(text)


def _stanza_outcome(text: str, dataset_id: str) -> str | None:
    """Return why a stanza cannot be hydrated, or None when it has work to do.

    Lets the caller tell a shard that never held the dataset, or a stanza with
    nothing left to fill, apart from one the matcher looked at and declined.

    Args:
        text: The full per-family catalog shard text.
        dataset_id: The dataset id to inspect.

    Returns:
        `"skipped"` when the stanza is missing or holds no placeholder, else
        `None`.
    """
    found = _stanza_match(text, dataset_id)
    if found is None or not _placeholder_slugs(found.group(1)):
        return "skipped"
    return None


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
    match = _stanza_match(text, dataset_id)
    if match is None:
        return text
    block = match.group(1)
    placeholders = _placeholder_slugs(block)
    if not placeholders:
        return text
    assignments = _match_variables(placeholders, nc_meta, _claimed_nc_names(block))
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


def _hydrate_stanza_whole(
    text: str, dataset_id: str, session: _ProbeSession
) -> tuple[str, list[str], list[str]]:
    """Hydrate a stanza from ONE whole-dataset probe, by name rather than by request.

    Runs for whatever the per-variable pass declined. A row it could not place
    is one no constraints block names — a product that does not partition by
    variable has no block to look a row up in, and a stale request-side name has
    none either — and a plain probe can still describe the product. Matching
    here is by name, so it goes through the ordinary evidence rules rather than
    trusting a lone result the way a named request may.

    Args:
        text: The full per-family catalog shard text.
        dataset_id: The dataset id whose stanza to hydrate.
        session: The probe session, reused so a failure is recorded once.

    Returns:
        A `(text, filled, declined)` triple, as
        :func:`_hydrate_stanza_per_variable` returns.
    """
    try:
        nc_meta = _retrieve_with_timeout(dataset_id, session.timeout)
    except TimeoutError as exc:
        session.timed_out = True
        session.error = exc
        return text, [], []
    except Exception as exc:  # noqa: BLE001 — a licence-gated dataset is skipped
        session.error = exc
        return text, [], []
    session.offered.update(_data_variables(nc_meta))
    match = _stanza_match(text, dataset_id)
    placeholders = _placeholder_slugs(match.group(1)) if match else []
    new_text = _rewrite_stanza(text, dataset_id, nc_meta)
    rewritten = _stanza_match(new_text, dataset_id)
    if new_text == text or rewritten is None:
        return text, [], placeholders
    remaining = _placeholder_slugs(rewritten.group(1))
    filled = [slug for slug in placeholders if slug not in remaining]
    return new_text, filled, remaining


def _declined_detail(session: _ProbeSession, declined: list[str]) -> str:
    """Describe why a dataset that retrieved cleanly still hydrated nothing.

    Args:
        session: The probe session that ran, carrying what the store offered.
        declined: The slugs left as placeholders.

    Returns:
        A phrase naming either the missing data variables or the declined rows.
    """
    if not session.offered:
        return "no data variables, only coordinates and auxiliaries"
    offered = sorted(session.offered)
    shown = ", ".join(offered[:_ECHO_MAX_NAMES])
    extra = len(offered) - _ECHO_MAX_NAMES
    listed = f"{shown}, +{extra} more" if extra > 0 else shown
    return f"no confident match for {', '.join(declined)} (offered: {listed})"


def _parses_as_yaml(text: str) -> bool:
    """Return True when `text` still loads, duplicate keys included.

    The guard between a splicing bug and a broken catalog. Rewrites are spliced
    into shard text rather than round-tripped through a YAML emitter, so a bad
    edit produces a file that only fails later, when something tries to load the
    family — by which time the sweep has moved on.

    Goes through the catalog's own public loader, because the failure this is
    most likely to catch is a duplicated key, which a permissive parser accepts
    by silently keeping the last one. That loader takes a path, so the candidate
    text is parsed from a scratch file — a cost of nothing beside the network
    retrieve that produced it, and it keeps a provider off core's internals.

    Args:
        text: The rewritten shard text.

    Returns:
        True when the text is loadable as the catalog loads it.
    """
    import tempfile

    from earthlens.base.yaml_loader import load_yaml_strict

    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "shard.yaml"
        probe.write_text(text, encoding="utf-8")
        try:
            load_yaml_strict(probe)
        except Exception:  # noqa: BLE001 — any parse failure means do not write
            return False
    return True


def _written_rows_survive(text: str, dataset_id: str, filled: list[str]) -> bool:
    """Return True when every row this pass filled reads back as it was written.

    :func:`_parses_as_yaml` proves the shard still loads, which is a weaker claim
    than it looks: a hydrated value can be perfectly valid YAML and still be the
    wrong *type* coming back. Nitrogen monoxide's short name is `no`, which YAML
    1.1 resolves to the boolean `False` — the file parses, and the catalog then
    refuses to build the row. Writing that value strands every dataset swept
    after it, because each one reloads the shard the previous one poisoned.

    So the values are read back the way the catalog will read them and required
    to still be strings. Only the rows named in `filled` are examined: a shard
    may carry unrelated imperfections that predate the sweep, and refusing to
    write because of one would cost hydration for a defect this pass did not
    cause.

    Args:
        text: The rewritten shard text.
        dataset_id: The dataset whose stanza was rewritten.
        filled: Slugs of the variable rows filled in this pass.

    Returns:
        True when each filled row carries string `nc_variable` and `units`.
    """
    import tempfile

    from earthlens.base.yaml_loader import load_yaml_strict

    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "shard.yaml"
        probe.write_text(text, encoding="utf-8")
        try:
            data = load_yaml_strict(probe)
        except Exception:  # noqa: BLE001 - the parse guard reports this case
            return False
    variables = ((data or {}).get("datasets", {}).get(dataset_id, {}) or {}).get(
        "variables"
    ) or {}
    for slug in filled:
        row = variables.get(slug)
        if not isinstance(row, dict):
            return False
        for key in ("nc_variable", "units"):
            if not isinstance(row.get(key), str):
                return False
    return True


def _hydrated_detail(session: _ProbeSession, declined: list[str]) -> str:
    """Describe what a hydrated dataset left behind, and why it stopped.

    A partial fill that hit a deadline reads the same as one whose rows the
    store declined, unless the reason is said out loud — and only the first is
    worth re-running, which matters because the sweep is built to be resumed.

    Args:
        session: The probe session that ran.
        declined: The slugs left as placeholders.

    Returns:
        A suffix for the per-dataset echo, empty when everything was filled.
    """
    left = f", {len(declined)} left" if declined else ""
    if session.timed_out:
        return f"{left} (timed out; re-run to continue)"
    if session.error is not None:
        return f"{left} ({type(session.error).__name__}; re-run to continue)"
    return left


def _hydrate_one(
    dataset_id: str,
    prefix: str,
    catalog_dir: Path,
    file_text: dict[Path, str],
    timeout: float | None,
) -> str:
    """Hydrate one placeholder dataset in place; return its outcome tag.

    Probes each placeholder row by name under `timeout`, writes what it can
    identify into the shard and — on a real change that still parses — updates
    `file_text` and writes to disk immediately. Progress is echoed, naming the
    reason when a dataset stopped early. Never raises for a licence-gated,
    unreachable, timed-out, or unmatchable dataset: those return a skip tag.

    Args:
        dataset_id: The Copernicus dataset id to hydrate.
        prefix: The `[k/total] id` label echoed with the outcome.
        catalog_dir: The `catalog/` shard directory to locate the stanza in.
        file_text: The per-shard text cache, updated in place on a hydration.
        timeout: Per-probe retrieve deadline; `None` / `0` waits without one.

    Returns:
        One of `"hydrated"`, `"partial"`, `"unmatched"`, `"timed_out"`, or
        `"skipped"`. `"partial"` is a `"hydrated"` that stopped early, so the
        operator can see which datasets a re-run would carry further.
        `"unmatched"` is reserved for the retrieve that worked against a stanza
        with real placeholders and still hydrated nothing — the operator can
        curate that row by hand. A missing stanza, or one whose placeholders
        have all been filled already, is a `"skipped"` like a licence or network
        failure: there was nothing for the matcher to decline.
    """
    path = _find_file_for_dataset(catalog_dir, dataset_id)
    if path is None:
        typer.echo(f"{prefix}: skipped")
        return "skipped"
    if path not in file_text:
        file_text[path] = path.read_text(encoding="utf-8")
    blocked = _stanza_outcome(file_text[path], dataset_id)
    if blocked is not None:
        typer.echo(f"{prefix}: {blocked}")
        return blocked
    session = _ProbeSession(dataset_id, timeout)
    new_text, filled, declined = _hydrate_stanza_per_variable(
        file_text[path], dataset_id, session
    )
    if declined and session.error is None:
        # A row no constraints block names is declined outright by the
        # per-variable pass: that is how a product with no variable dimension
        # looks (obs4mips CO2/CH4 partition by nothing the slug can be found
        # under), and also how a stale request-side name looks. One
        # whole-dataset probe can still describe those rows by name, so it runs
        # whenever anything was declined — not only when the whole stanza was,
        # which would strand such a row the moment a sibling answered.
        recovered, also_filled, declined = _hydrate_stanza_whole(
            new_text, dataset_id, session
        )
        if also_filled:
            new_text = recovered
            filled = filled + also_filled
    if session.timed_out and not filled:
        typer.echo(f"{prefix}: timed out, skipped")
        return "timed_out"
    if new_text == file_text[path]:
        if session.error is not None:
            typer.echo(f"{prefix}: skipped ({type(session.error).__name__})")
            return "skipped"
        typer.echo(f"{prefix}: retrieved, {_declined_detail(session, declined)}")
        return "unmatched"
    if not _parses_as_yaml(new_text):
        # The rewrite is spliced into shard text, so a splicing bug would put an
        # unloadable family file on disk and only surface later, far from here.
        # Refusing to write costs one dataset's hydration; writing costs the shard.
        typer.echo(f"{prefix}: rewrite did not parse, shard left untouched")
        return "unmatched"
    if not _written_rows_survive(new_text, dataset_id, filled):
        # A value can be valid YAML and still come back the wrong type -- `no`
        # reads as the boolean False -- which loads here and breaks the catalog
        # for every dataset swept after this one. Checked before the write.
        typer.echo(f"{prefix}: rewrite did not reload, shard left untouched")
        return "unmatched"
    file_text[path] = new_text
    path.write_text(new_text, encoding="utf-8")
    typer.echo(
        f"{prefix}: hydrated {len(filled)}"
        f"{_hydrated_detail(session, declined)} -> {path.name}"
    )
    return "partial" if session.error is not None else "hydrated"


def _take_rows(datasets: list[str], rows: dict[str, int], limit: int) -> list[str]:
    """Take whole datasets until `limit` placeholder rows are accounted for.

    The sweep costs one retrieve per row, so a limit that counts datasets does
    not bound the work an operator is agreeing to. A dataset is never split,
    because half a hydrated stanza is not a useful stopping point — so the
    first one is always taken and the count acts as a floor.

    Args:
        datasets: Candidate dataset ids, in the order they will be swept.
        rows: Placeholder-row count per dataset id.
        limit: How many rows to take on.

    Returns:
        The leading datasets whose rows reach `limit`.
    """
    taken: list[str] = []
    budget = 0
    for name in datasets:
        taken.append(name)
        budget += rows.get(name, 0)
        if budget >= limit:
            break
    return taken


def bulk_hydrate_empty(
    limit: int | None = None,
    timeout: float | None = _DEFAULT_RETRIEVE_TIMEOUT,
) -> dict[str, Any]:
    """Fill every placeholder curated ECMWF row from a live retrieve, in place.

    Loads the curated catalog, finds datasets with any `units: unknown`
    variable, retrieves a tiny NetCDF per placeholder variable, and rewrites the stanza in
    its per-family shard (preserving the rest). Hardened for the full-catalog
    sweep: each retrieve runs under `timeout` so one request stuck in the CDS
    queue is skipped rather than wedging the pass, and each hydrated shard is
    **written the moment it changes** so an interrupted run keeps the progress
    it already made. A dataset whose retrieve fails, times out, or whose stanza
    cannot be matched is skipped — never fatal. Progress is echoed per dataset,
    one line summarising that dataset's per-variable probes.

    Args:
        limit: Stop once this many placeholder **rows** have been taken on,
            in alphabetical dataset order. Rows rather than datasets because
            the sweep issues one retrieve per row: the widest dataset holds 82
            of them, so a dataset-counting limit of 1 could mean 82 queued
            requests. A dataset is always taken whole, so the count is a floor
            — the first dataset is never split.
        timeout: Per-probe retrieve deadline in seconds; `None` / `0` waits
            without a deadline (the offline-test path).

    Returns:
        A summary
        `{candidates, hydrated, skipped, timed_out, unmatched, partial, filled}`
        mapping. `unmatched` counts the retrieves that succeeded and still
        hydrated nothing; those are also counted in `skipped`, which stays the
        total of everything not hydrated. `partial` counts the datasets that
        hydrated some rows and then stopped on a deadline or a refusal — they
        are counted in `hydrated` too, and they are the ones a re-run continues.
    """
    from earthlens.ecmwf import Catalog
    from earthlens.ecmwf.catalog import CATALOG_PATH, clear_catalog_cache

    catalog_dir = Path(CATALOG_PATH)
    catalog = Catalog()
    rows = {
        key: sum(var.units == "unknown" for var in ds.variables.values())
        for key, ds in catalog.datasets.items()
    }
    empty = sorted(key for key, count in rows.items() if count)
    if limit:
        empty = _take_rows(empty, rows, limit)

    total = len(empty)
    file_text: dict[Path, str] = {}
    hydrated = 0
    skipped = 0
    timed_out = 0
    unmatched = 0
    partial = 0
    filled: list[str] = []
    for index, dataset_id in enumerate(empty, start=1):
        prefix = f"[{index}/{total}] {dataset_id}"
        outcome = _hydrate_one(dataset_id, prefix, catalog_dir, file_text, timeout)
        if outcome in {"hydrated", "partial"}:
            hydrated += 1
            filled.append(dataset_id)
            if outcome == "partial":
                partial += 1
        elif outcome == "unmatched":
            unmatched += 1
            skipped += 1
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
        "unmatched": unmatched,
        "partial": partial,
        "filled": filled,
    }
