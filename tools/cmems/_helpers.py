"""Shared helpers for the CMEMS bulk-curation scripts.

Single home for the bundled-catalog path, the cadence / domain
inference rules, the `(version, part, service, variable)` walker over
:func:`copernicusmarine.describe` results, the canonical stanza
emitter, and the YAML splice helpers used by `refresh_cmems_catalog`,
the future `probe_cmems_netcdf`, and the future
`audit_cmems_datasets`. Centralising these keeps the three tools in
lock-step on the YAML shape they read and write.

Not part of the installed package — `sys.path.insert` against
`Path(__file__).parent` before importing.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

CATALOG_DIR: Path = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "earthlens"
    / "cmems"
    / "catalog"
)

# The merged-index file inside CATALOG_DIR (mirrors GEE's _index.yaml).
INDEX_PATH: Path = CATALOG_DIR / "_index.yaml"

# Back-compat alias: callers that imported CATALOG_PATH (the old
# single-file constant) now get the catalog directory, which is what
# earthlens.cmems.catalog.CATALOG_PATH also points at.
CATALOG_PATH: Path = CATALOG_DIR


_CADENCE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-climatology", "climatology"),
    ("_PT1H-i", "hourly"),
    ("_PT1H-m", "hourly"),
    ("_PT1H", "hourly"),
    ("_PT6H-i", "6hourly"),
    ("_PT6H-m", "6hourly"),
    ("_PT6H", "6hourly"),
    ("_P1D-m", "daily"),
    ("_P1D-i", "daily"),
    ("_P1D", "daily"),
    ("_P7D-m", "weekly"),
    ("_P7D", "weekly"),
    ("_P1M-m", "monthly"),
    ("_P1M-i", "monthly"),
    ("_P1M", "monthly"),
    ("_P1Y-m", "annual"),
    ("_P1Y", "annual"),
    ("_static", "irregular"),
)


def cadence_for_dataset_id(dataset_id: str) -> str:
    """Map a CMEMS dataset_id suffix to its earthlens cadence label.

    CMEMS encodes cadence in the dataset_id suffix per its naming
    convention — `_P1D-m` is a daily mean, `_P1M-m` a monthly mean,
    `_PT1H-i` an hourly instantaneous, `_P1Y-m` an annual mean. The
    earthlens catalog uses coarser labels (`daily`, `monthly`, `hourly`,
    …); this function performs that mapping. Anything that does not
    match the known suffix set falls back to `"irregular"`, leaving the
    maintainer to set the label by hand.

    Args:
        dataset_id: The full CMEMS dataset_id, e.g.
            `"cmems_mod_glo_phy_my_0.083deg_P1D-m"`.

    Returns:
        One of `"hourly"`, `"6hourly"`, `"daily"`, `"weekly"`,
            `"monthly"`, `"annual"`, `"climatology"`, or `"irregular"`.

    Examples:
        - GLORYS daily mean -> `"daily"`:
            ```python
            >>> cadence_for_dataset_id("cmems_mod_glo_phy_my_0.083deg_P1D-m")
            'daily'

            ```
        - A `-climatology_P1M-m` suffix wins over the bare `_P1M-m`:
            ```python
            >>> cadence_for_dataset_id("cmems_mod_glo_phy_my_0.083deg-climatology_P1M-m")
            'climatology'

            ```
        - An id with no recognised suffix falls back to `"irregular"`:
            ```python
            >>> cadence_for_dataset_id("some_static_dataset_no_suffix")
            'irregular'

            ```
    """
    for suffix, cadence in _CADENCE_SUFFIXES:
        if suffix in dataset_id:
            return cadence
    return "irregular"


_DOMAIN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("GLOBAL_", "global"),
    ("MEDSEA_", "mediterranean"),
    ("BLKSEA_", "black-sea"),
    ("BALTICSEA_", "baltic-sea"),
    ("ARCTIC_", "arctic"),
    ("IBI_", "ibi"),
    ("NWSHELF_", "nw-shelf"),
    ("SEAICE_", "polar"),
    ("OMI_", "indicator"),
    ("INSITU_", "global"),
    ("SST_GLO_", "global"),
    ("SST_MED_", "mediterranean"),
    ("SST_BAL_", "baltic-sea"),
    ("SST_BS_", "black-sea"),
    ("SST_ARC_", "arctic"),
    ("SST_NWS_", "nw-shelf"),
    ("SST_IBI_", "ibi"),
    ("SEALEVEL_GLO_", "global"),
    ("SEALEVEL_EUR_", "ibi"),
    ("OCEANCOLOUR_GLO_", "global"),
    ("OCEANCOLOUR_MED_", "mediterranean"),
    ("OCEANCOLOUR_BAL_", "baltic-sea"),
    ("OCEANCOLOUR_BS_", "black-sea"),
    ("OCEANCOLOUR_ARC_", "arctic"),
    ("OCEANCOLOUR_NWS_", "nw-shelf"),
    ("OCEANCOLOUR_IBI_", "ibi"),
    ("WAVE_GLO_", "global"),
    ("MULTIOBS_GLO_", "global"),
)


def domain_for_product_id(product_id: str) -> str:
    """Map a CMEMS product_id prefix to its earthlens domain label.

    The product_id prefix encodes the geographic basin: `GLOBAL_`,
    `MEDSEA_`, `BLKSEA_`, `BALTICSEA_`, `ARCTIC_`, `IBI_`, `NWSHELF_`,
    plus thematic prefixes like `SST_GLO_` (global SST), `OCEANCOLOUR_
    MED_` (Mediterranean ocean colour), etc. that compose a thematic
    layer with a region segment. This function picks the longest
    matching prefix.

    Args:
        product_id: The CMEMS product_id, e.g.
            `"GLOBAL_MULTIYEAR_PHY_001_030"`.

    Returns:
        One of `"global"`, `"mediterranean"`, `"black-sea"`,
            `"baltic-sea"`, `"arctic"`, `"ibi"`, `"nw-shelf"`,
            `"polar"`, or `"indicator"`. Unknown prefixes fall back to
            `"global"`.

    Examples:
        - A global physics product routes to `"global"`:
            ```python
            >>> domain_for_product_id("GLOBAL_MULTIYEAR_PHY_001_030")
            'global'

            ```
        - A composite prefix like `SST_GLO_` resolves to its region segment:
            ```python
            >>> domain_for_product_id("SST_GLO_SST_L4_NRT_OBSERVATIONS_010_001")
            'global'

            ```
        - An unrecognised prefix falls back to `"global"`:
            ```python
            >>> domain_for_product_id("UNKNOWN_PREFIX_X")
            'global'

            ```
    """
    sorted_prefixes = sorted(_DOMAIN_PREFIXES, key=lambda kv: -len(kv[0]))
    for prefix, domain in sorted_prefixes:
        if product_id.startswith(prefix):
            return domain
    return "global"


# Thematic sub-buckets for the oversized `global` domain. The global
# basin alone holds ~424 datasets — too many for one balanced file —
# so it is split by the thematic token in the product_id. Checked in
# order; first substring match wins. Anything unmatched lands in
# `global-other`.
_GLOBAL_THEMES: tuple[tuple[str, str], ...] = (
    ("_BGC", "global-biogeochem"),
    ("OCEANCOLOUR", "global-biogeochem"),
    ("_WAV", "global-wave"),
    ("WIND_", "global-wind"),
    ("SST_", "global-sst"),
    ("SEALEVEL", "global-sealevel"),
    ("INSITU_", "global-observations"),
    ("MULTIOBS", "global-observations"),
    ("_PHY", "global-physics"),
)


def catalog_file_for(product_id: str) -> str:
    """Return the per-file stem that owns a dataset from `product_id`.

    Routes a dataset to its catalog file under
    `src/earthlens/cmems/catalog/`. Most domains map 1:1 onto a single
    file (the domain label from :func:`domain_for_product_id`); the
    `global` domain is sub-split by theme because it alone carries
    ~424 datasets, which would otherwise dwarf every other file (the
    same balance the GEE catalog keeps via its per-category split).

    Args:
        product_id: The CMEMS product_id, e.g.
            `"GLOBAL_MULTIYEAR_PHY_001_030"`.

    Returns:
        A filename stem (no `.yaml`): one of the regional domains
            (`"mediterranean"`, `"arctic"`, …) or a global theme
            (`"global-physics"`, `"global-biogeochem"`, `"global-sst"`,
            `"global-sealevel"`, `"global-wave"`, `"global-wind"`,
            `"global-observations"`, `"global-other"`).

    Examples:
        - Global physics routes to its theme file:
            ```python
            >>> catalog_file_for("GLOBAL_MULTIYEAR_PHY_001_030")
            'global-physics'

            ```
        - Global SST routes to the SST theme:
            ```python
            >>> catalog_file_for("SST_GLO_SST_L4_NRT_OBSERVATIONS_010_001")
            'global-sst'

            ```
        - A regional product keeps its plain domain file:
            ```python
            >>> catalog_file_for("MEDSEA_MULTIYEAR_PHY_006_004")
            'mediterranean'

            ```
    """
    domain = domain_for_product_id(product_id)
    if domain != "global":
        return domain
    for token, theme in _GLOBAL_THEMES:
        if token in product_id:
            return theme
    return "global-other"


def humanize_standard_name(standard_name: str | None) -> str:
    """Convert a CF standard_name to a human-readable long_name.

    The toolbox surfaces `standard_name` (snake_case CF identifier;
    `sea_water_potential_temperature`); the earthlens catalog's
    `long_name:` field expects a human-readable string
    (`Sea water potential temperature`). The mapping is `replace("_",
    " ").capitalize()` — empirically it reproduces every existing
    curated `long_name:` in `cmems_data_catalog.yaml`.

    Args:
        standard_name: The CF standard_name from
            `CopernicusMarineVariable.standard_name`, or `None`.

    Returns:
        The capitalised, space-separated form, or `""` if input is
            falsy.

    Examples:
        - CF standard_name -> humanised long_name:
            ```python
            >>> humanize_standard_name("sea_water_potential_temperature")
            'Sea water potential temperature'

            ```
        - Single-word names are simply capitalised:
            ```python
            >>> humanize_standard_name("chlorophyll")
            'Chlorophyll'

            ```
        - Falsy input maps to an empty string (does not raise):
            ```python
            >>> humanize_standard_name(None)
            ''
            >>> humanize_standard_name("")
            ''

            ```
    """
    if not standard_name:
        return ""
    return standard_name.replace("_", " ").capitalize()


def _yaml_scalar(value: str) -> str:
    """Render `value` as a single-line YAML scalar, quoted only when needed.

    Delegates to `yaml.safe_dump` so units strings like `"1e-3"`,
    `"%"`, or `"1"` (which YAML would otherwise parse as a float, a
    metachar, or a boolean alias) round-trip safely while plain CF
    units like `degrees_C` or `m s-1` stay bare. Drops the
    `\\n...\\n` document trailer PyYAML appends when dumping a
    top-level scalar.

    Args:
        value: The string to render.

    Returns:
        The YAML scalar form, without a trailing newline.
    """
    text = "" if value is None else str(value)
    return yaml.safe_dump(
        text, default_flow_style=False, allow_unicode=True
    ).split("\n", 1)[0]


def walk_variables(dataset: Any) -> list[Any]:
    """Return the variable list off the first service that exposes any.

    CMEMS surfaces the same variable set across every service of a
    `(version, part)` pair — `original-files`, `wmts`,
    `arco-geo-series`, `arco-time-series` all carry identical
    `short_name` / `units` / `standard_name` triples — so picking the
    first non-empty one is sufficient for catalog metadata. Returns the
    variables sorted by `short_name` for stable output.

    Args:
        dataset: A `CopernicusMarineDataset` from
            `copernicusmarine.describe()`.

    Returns:
        Sorted list of `CopernicusMarineVariable` instances. Empty if
            no service on any version/part exposes variables.
    """
    for version in dataset.versions:
        for part in version.parts:
            for service in part.services:
                if service.variables:
                    return sorted(service.variables, key=lambda v: v.short_name)
    return []


# Multipliers from a CF time-unit word to seconds.
_TIME_UNIT_SECONDS: dict[str, float] = {
    "milliseconds": 1e-3,
    "millisecond": 1e-3,
    "ms": 1e-3,
    "seconds": 1.0,
    "second": 1.0,
    "secs": 1.0,
    "sec": 1.0,
    "s": 1.0,
    "minutes": 60.0,
    "minute": 60.0,
    "min": 60.0,
    "hours": 3600.0,
    "hour": 3600.0,
    "hr": 3600.0,
    "h": 3600.0,
    "days": 86400.0,
    "day": 86400.0,
    "d": 86400.0,
}

_TIME_UNIT_RE = re.compile(r"\s*(\w+)\s+since\s+(\d{4}-\d{2}-\d{2})")


def _parse_time_unit(unit: str | None) -> tuple[float, _dt.datetime] | None:
    """Parse a CF `"<unit> since <date>"` string into `(seconds, epoch)`.

    Args:
        unit: A CF time-coordinate unit string, e.g.
            `"milliseconds since 1970-01-01 00:00:00Z"` or
            `"hours since 1950-01-01"`.

    Returns:
        `(seconds_per_unit, epoch_datetime)` when the string matches,
            else `None`.

    Examples:
        - An hours-since-1950 unit gives the scale + epoch:
            ```python
            >>> scale, epoch = _parse_time_unit("hours since 1950-01-01")
            >>> scale
            3600.0
            >>> epoch.year, epoch.month, epoch.day
            (1950, 1, 1)

            ```
        - Milliseconds and a trailing timezone marker still parse:
            ```python
            >>> scale, epoch = _parse_time_unit("milliseconds since 1970-01-01 00:00:00Z")
            >>> scale
            0.001
            >>> epoch.year
            1970

            ```
        - Empty or unrecognised-unit strings return None:
            ```python
            >>> _parse_time_unit("") is None
            True
            >>> _parse_time_unit("parsecs since 1970-01-01") is None
            True

            ```
    """
    if not unit:
        return None
    m = _TIME_UNIT_RE.match(unit)
    if not m:
        return None
    scale = _TIME_UNIT_SECONDS.get(m.group(1).lower())
    if scale is None:
        return None
    epoch = _dt.datetime.strptime(m.group(2), "%Y-%m-%d").replace(
        tzinfo=_dt.timezone.utc
    )
    return scale, epoch


def temporal_bounds(dataset: Any) -> tuple[str | None, str | None]:
    """Read a dataset's time-coverage bounds from `describe()`.

    The `original-files` / `wmts` services carry no coordinate
    metadata, but the arco services (`arco-geo-series` /
    `arco-time-series`) expose each variable's `coordinates` with a
    `time` entry carrying `minimum_value` / `maximum_value` plus a CF
    `coordinate_unit`. This walks to the first such `time` coordinate
    and converts the numeric min/max to `YYYY-MM-DD` strings, so the
    catalog's `temporal.start` can be pinned without a live
    `subset()` probe.

    Args:
        dataset: A `CopernicusMarineDataset` from
            `copernicusmarine.describe()`.

    Returns:
        `(start_iso, end_iso)`. `start_iso` is the earliest date the
            dataset covers; `end_iso` is the latest. Either is `None`
            when no `time` coordinate is found or its unit can't be
            parsed (static fields, malformed metadata).

    Examples:
        - Read the coverage start of a curated dataset from the live
          toolbox (needs the network + `copernicusmarine`, so skipped
          under doctest):
            ```python
            >>> import copernicusmarine as cm  # doctest: +SKIP
            >>> resp = cm.describe(  # doctest: +SKIP
            ...     dataset_id="cmems_mod_glo_phy_my_0.083deg_P1D-m",
            ...     disable_progress_bar=True,
            ... )
            >>> dataset = resp.products[0].datasets[0]  # doctest: +SKIP
            >>> start, end = temporal_bounds(dataset)  # doctest: +SKIP
            >>> start  # doctest: +SKIP
            '1993-01-01'

            ```

        The conversion logic is unit-tested against lightweight
        coordinate stand-ins in
        `tests/cmems/tools/test_helpers.py::TestTemporalBounds` (no
        network); a `time` coordinate of `"hours since 1950-01-01"`
        with `minimum_value=0`, `maximum_value=24` yields
        `("1950-01-01", "1950-01-02")`, and a dataset whose variables
        carry no `time` coordinate yields `(None, None)`.
    """
    for version in dataset.versions:
        for part in version.parts:
            for service in part.services:
                for var in service.variables:
                    for coord in getattr(var, "coordinates", None) or []:
                        if getattr(coord, "coordinate_id", None) != "time":
                            continue
                        parsed = _parse_time_unit(
                            getattr(coord, "coordinate_unit", None)
                        )
                        mn = getattr(coord, "minimum_value", None)
                        mx = getattr(coord, "maximum_value", None)
                        if parsed is None or mn is None:
                            continue
                        scale, epoch = parsed
                        start = (
                            epoch + _dt.timedelta(seconds=mn * scale)
                        ).date().isoformat()
                        end = (
                            (epoch + _dt.timedelta(seconds=mx * scale))
                            .date()
                            .isoformat()
                            if mx is not None
                            else None
                        )
                        return start, end
    return None, None


def emit_dataset_stanza(product: Any, dataset: Any) -> str:
    """Emit a ready-to-paste `datasets.<dataset_id>:` YAML stanza.

    Maps one `(product, dataset)` pair from
    `copernicusmarine.describe()` onto the per-domain catalog schema
    (`catalog/<domain>.yaml`). `cadence` is inferred from the
    dataset_id suffix; `domain` from the product_id prefix. Temporal
    bounds are emitted as `null` with a TODO comment because the
    toolbox's `released_date` is the release timestamp of the dataset's
    arco copy, not the start of the underlying data — pinning the
    actual start requires reading the time coordinate from a real
    subset() response (the C3 probe).

    Args:
        product: A `CopernicusMarineProduct` whose `datasets` list
            contains `dataset`.
        dataset: A `CopernicusMarineDataset` to render.

    Returns:
        The stanza as a string, starting with `  <dataset_id>:` and
            ending with a newline. Ready to concatenate after the
            existing `datasets:` block.
    """
    ds_id = dataset.dataset_id
    title = dataset.dataset_name or product.title or ds_id
    cadence = cadence_for_dataset_id(ds_id)
    domain = domain_for_product_id(product.product_id)
    variables = walk_variables(dataset)
    start, _end = temporal_bounds(dataset)

    lines: list[str] = [
        f"  {ds_id}:",
        f"    product: {product.product_id}",
        f"    title: {_yaml_scalar(title)}",
        f"    cadence: {cadence}",
        f"    domain: {domain}",
        "    temporal:",
    ]
    if start:
        lines.append(f"      start: {start}")
    else:
        lines.append(
            "      start: null            # TODO: no time coord in describe()"
        )
    # `end` stays null: the arco max is "latest available today" and
    # would go stale for rolling/NRT products. null = NRT / rolling.
    lines.append("      end: null              # null = NRT / rolling")
    if variables:
        lines.append("    variables:")
        for var in variables:
            lines.append(f"      {var.short_name}:")
            lines.append(f"        units: {_yaml_scalar(var.units)}")
            std = getattr(var, "standard_name", None)
            long_name = humanize_standard_name(std)
            if long_name:
                lines.append(f"        long_name: {_yaml_scalar(long_name)}")
            else:
                lines.append(
                    f"        long_name: ''            # TODO: standard_name missing on describe()"
                )
    else:
        lines.append("    variables: {}    # TODO: no variables on describe(); verify dataset id")
    return "\n".join(lines) + "\n"


_AVAILABLE_DATASETS_RE = re.compile(
    r"^available_datasets:\n(?:[ \t]+.*(?:\n|$)|\n)+",
    re.MULTILINE,
)


def render_available_datasets_block(dataset_ids: Iterable[str]) -> str:
    """Render the `available_datasets:` YAML block for `_index.yaml`.

    The block is a flat alphabetised list of dataset ids — the full
    `copernicusmarine.describe()` index, of which the curated
    `datasets:` map across the per-domain files is a subset. Ends with
    a single trailing newline.

    Args:
        dataset_ids: Any iterable of dataset_id strings; sorted +
            de-duplicated before rendering so the output is
            deterministic.

    Returns:
        The block as a string, ready to splice via
            :func:`splice_available_datasets`.

    Examples:
        - Render and inspect the block:
            ```python
            >>> block = render_available_datasets_block(["ds_b", "ds_a"])
            >>> print(block, end="")
            available_datasets:
              - ds_a
              - ds_b

            ```
        - Duplicate inputs are collapsed:
            ```python
            >>> render_available_datasets_block(["x", "x", "y"]).count("- x")
            1

            ```
        - Empty input yields just the header line:
            ```python
            >>> render_available_datasets_block([])
            'available_datasets:\\n'

            ```
    """
    sorted_ids = sorted(set(dataset_ids))
    lines = ["available_datasets:"]
    lines.extend(f"  - {ds_id}" for ds_id in sorted_ids)
    return "\n".join(lines) + "\n"


def splice_available_datasets(text: str, new_block: str) -> str:
    """Replace the `available_datasets:` block in `_index.yaml` text.

    Preserves everything outside the block — the leading comment
    header in `_index.yaml`. Raises if the text has no
    `available_datasets:` key (a structural assertion: `_index.yaml`
    must always have one, even if empty).

    Args:
        text: Full current contents of `catalog/_index.yaml`.
        new_block: Output of :func:`render_available_datasets_block`.

    Returns:
        The updated text.

    Raises:
        ValueError: If `text` has no top-level `available_datasets:`
            block.
    """
    m = _AVAILABLE_DATASETS_RE.search(text)
    if not m:
        raise ValueError(
            "could not find an `available_datasets:` block in _index.yaml — "
            "expected a list-of-strings entry after the comment header"
        )
    return text[: m.start()] + new_block + text[m.end() :]


DATASET_STANZA_RE = re.compile(
    r"^  (?P<ds_id>[A-Za-z0-9_./\-]+):\s*$",
    re.MULTILINE,
)


def find_dataset_stanza_span(text: str, dataset_id: str) -> tuple[int, int] | None:
    """Return `(start, end)` byte offsets of `dataset_id`'s stanza, or `None`.

    `start` is the position of the `  <dataset_id>:` line; `end` is
    the position of the next sibling stanza head, or end of input.

    Args:
        text: Full catalog YAML.
        dataset_id: The CMEMS dataset_id to locate.

    Returns:
        Byte-offset tuple, or `None` when no stanza head matches.
    """
    key = re.escape(dataset_id)
    head = re.search(rf"^  {key}:\s*$", text, re.MULTILINE)
    if not head:
        return None
    next_head = DATASET_STANZA_RE.search(text, head.end())
    end = next_head.start() if next_head else len(text)
    return head.start(), end


def append_stanzas_to_datasets_block(text: str, stanzas: str) -> str:
    """Append `stanzas` to the tail of the `datasets:` block in `text`.

    The `datasets:` block runs from its header line to end of file —
    there is no further top-level key after it in the CMEMS YAML — so
    appending is simply "trim trailing whitespace, emit a blank line,
    then the new stanzas". The caller is responsible for ensuring
    `stanzas` is already in canonical 2-space-indented form (use
    :func:`emit_dataset_stanza` to produce it, or :func:`compact_text`
    to normalise).

    Args:
        text: Full catalog YAML.
        stanzas: One or more canonical dataset stanzas concatenated.

    Returns:
        The updated YAML text. Always ends with a single newline.

    Raises:
        ValueError: If `text` has no `datasets:` header.
    """
    if not re.search(r"^datasets:\s*$", text, re.MULTILINE):
        raise ValueError("YAML has no top-level `datasets:` header")
    body = text.rstrip("\n") + "\n\n" + stanzas.lstrip("\n").rstrip("\n") + "\n"
    return body


def compact_text(raw_text: str) -> str:
    """Normalise raw `refresh --with-datasets` output for in-place pasting.

    The stanza emitter and the splice helpers tolerate slightly messy
    input (extra blank lines, scratch `# ----` separators between
    stanzas, stray `# product:` headers, CRLF line endings) — this
    pass strips those so the YAML written into the catalog matches the
    style of the curated rows already there.

    Operations applied:

    * normalise line endings (CRLF / CR → LF),
    * drop scratch `# ----` and `# product:` annotation lines,
    * trim trailing whitespace per line,
    * collapse three-or-more blank lines to one blank line,
    * leave each stanza's body untouched (so canonical output from
      :func:`emit_dataset_stanza` round-trips unchanged).

    Args:
        raw_text: One or more stanzas as emitted by
            `refresh --with-datasets`, possibly concatenated with
            scratch markers.

    Returns:
        Cleaned text ending in a single newline.

    Examples:
        - Scratch markers and blank-run collapse:
            ```python
            >>> raw = "# ---- paste under `datasets:` ----\\n  ds-a:\\n\\n\\n\\n  ds-b:\\n"
            >>> out = compact_text(raw)
            >>> "# ---- paste" in out
            False
            >>> "ds-a" in out and "ds-b" in out
            True

            ```
        - CRLF line endings normalise to LF:
            ```python
            >>> "\\r" in compact_text("  ds-x:\\r\\n    product: P\\r\\n")
            False

            ```
        - Empty input still ends with a single newline:
            ```python
            >>> compact_text("")
            '\\n'

            ```
    """
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in raw_text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("# ----"):
            continue
        if stripped.startswith("# product:"):
            continue
        kept.append(line.rstrip())
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"
