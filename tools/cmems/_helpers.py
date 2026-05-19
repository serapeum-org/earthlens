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

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

CATALOG_PATH: Path = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "earthlens"
    / "cmems"
    / "cmems_data_catalog.yaml"
)


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
    """
    sorted_prefixes = sorted(_DOMAIN_PREFIXES, key=lambda kv: -len(kv[0]))
    for prefix, domain in sorted_prefixes:
        if product_id.startswith(prefix):
            return domain
    return "global"


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


def emit_dataset_stanza(product: Any, dataset: Any) -> str:
    """Emit a ready-to-paste `datasets.<dataset_id>:` YAML stanza.

    Maps one `(product, dataset)` pair from
    `copernicusmarine.describe()` onto the catalog schema in
    `cmems_data_catalog.yaml`. `cadence` is inferred from the
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

    lines: list[str] = [
        f"  {ds_id}:",
        f"    product: {product.product_id}",
        f"    title: {_yaml_scalar(title)}",
        f"    cadence: {cadence}",
        f"    domain: {domain}",
        "    temporal:",
        "      start: null            # TODO: pin from CMEMS portal / probe_cmems_netcdf.py",
        "      end: null              # null = NRT (rolling)",
    ]
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


_AVAILABLE_PRODUCTS_RE = re.compile(
    r"^available_products:\n(?:[ \t]+.*(?:\n|$)|\n)+",
    re.MULTILINE,
)


def render_available_products_block(product_ids: Iterable[str]) -> str:
    """Render the `available_products:` YAML block.

    The block is a flat alphabetised list of product ids — no grouping
    headers because the CMEMS thematic prefixes are themselves the
    grouping (`GLOBAL_*`, `MEDSEA_*`, …) and re-sorting under explicit
    `# ----- <header> -----` comments would only duplicate that
    structure. Ends with a single trailing newline.

    Args:
        product_ids: Any iterable of product_id strings; sorted before
            rendering so the output is deterministic.

    Returns:
        The block as a string, ready to splice via
            :func:`splice_available_products`.
    """
    sorted_ids = sorted(set(product_ids))
    lines = ["available_products:"]
    lines.extend(f"  - {pid}" for pid in sorted_ids)
    return "\n".join(lines) + "\n"


def splice_available_products(text: str, new_block: str) -> str:
    """Replace the `available_products:` block in `text` with `new_block`.

    Preserves everything outside the block — leading comments, the
    `datasets:` block below, schema-header lines. Raises if the YAML
    has no `available_products:` key (a structural assertion: the
    bundled catalog must always have one, even if empty).

    Args:
        text: Full current contents of `cmems_data_catalog.yaml`.
        new_block: Output of :func:`render_available_products_block`.

    Returns:
        The updated YAML text.

    Raises:
        ValueError: If `text` has no top-level `available_products:`
            block.
    """
    m = _AVAILABLE_PRODUCTS_RE.search(text)
    if not m:
        raise ValueError(
            "could not find an `available_products:` block in the YAML — "
            "expected a list-of-strings entry between the schema header "
            "and the `datasets:` map"
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
