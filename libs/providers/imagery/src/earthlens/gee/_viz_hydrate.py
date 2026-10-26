"""Capture `gee:visualizations` from the public EE STAC into catalog rows.

The data half of the visualization work: for every curated GEE dataset the
public Earth Engine STAC serves, read the publisher's own default renderings
(`summaries.gee:visualizations`) and splice a `visualizations:` block into the
existing stanza **in place**, leaving the comments and field order of the
surrounding rows untouched. This is `Band.classes` (`gee:classes`) done once
more for a different STAC key.

Unlike `_hydrate`, nothing here needs credentials — the STAC catalog is public.
The network read sits behind `_stac_doc`, which is monkeypatch-able, so the
parsing and stanza-rewriting core stays pure and fully testable offline.

Upstream's shape is not uniform, and the parser is written around what it
actually serves rather than an idealised schema: `min` / `max` arrive as lists
that may be shorter than `bands` (a 3-band RGB usually carries one of each),
`palette` entries are mostly bare hex digits but sometimes CSS colour names
(`"blue"`), and some assets carry only `bands` (`ESA/WorldCover/v200`) while
others add `gamma` (`USGS/SRTMGL1_003`) or `gain` (`MODIS/061/MOD13Q1`). A
`display_name` is upstream's own label and is not always apt — several
single-band Sentinel-5P rows call their one-band stretch `"RGB"` — so it is
captured verbatim rather than second-guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: STAC summaries key holding the publisher's default renderings.
_VIS_KEY = "gee:visualizations"

#: Indent of a dataset stanza's own fields inside `catalog/*.yaml`.
_FIELD_INDENT = " " * 4

#: Numeric `band_vis` keys, mapped to the `Visualization` field they fill.
_NUMERIC_KEYS = {"min": "vmin", "max": "vmax", "gamma": "gamma", "gain": "gain"}


def _as_float_list(value: Any) -> list[float]:
    """Coerce a STAC numeric entry to a list of floats, dropping what will not cast.

    Upstream writes these as JSON arrays, but a scalar appears often enough to
    be worth accepting, and a stray `null` inside an array is worth skipping
    rather than failing the whole asset over.

    Args:
        value: The raw STAC value — a list, a scalar, or `None`.

    Returns:
        The values that cast to `float`, in order.
    """
    if isinstance(value, list):
        raw = value
    elif value is None:
        raw = []
    else:
        raw = [value]
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def visualizations_from_stac(doc: Any) -> list[dict[str, Any]]:
    """Parse an asset's STAC document into `Visualization` constructor kwargs.

    Args:
        doc: The asset's STAC document, as `_stac_doc` returns it.

    Returns:
        One kwargs mapping per rendering that names at least one band, in
        upstream order. A rendering with no bands is dropped, since it cannot
        be drawn and `Visualization` requires them.

    Examples:
        - The Sentinel-2 RGB entry, with its broadcast stretch:
            ```python
            >>> doc = {
            ...     "summaries": {
            ...         "gee:visualizations": [
            ...             {
            ...                 "display_name": "RGB",
            ...                 "image_visualization": {
            ...                     "band_vis": {
            ...                         "bands": ["B4", "B3", "B2"],
            ...                         "min": [0],
            ...                         "max": [3000],
            ...                     }
            ...                 },
            ...             }
            ...         ]
            ...     }
            ... }
            >>> visualizations_from_stac(doc)
            [{'name': 'RGB', 'bands': ('B4', 'B3', 'B2'), 'vmin': (0.0,), 'vmax': (3000.0,)}]

            ```
        - An asset with no renderings, or a rendering naming no band, yields
          nothing:
            ```python
            >>> visualizations_from_stac({"summaries": {}})
            []
            >>> visualizations_from_stac(
            ...     {"summaries": {"gee:visualizations": [{"display_name": "x"}]}}
            ... )
            []

            ```
    """
    summaries = doc.get("summaries") or {} if isinstance(doc, dict) else {}
    entries = summaries.get(_VIS_KEY) or []
    parsed = (_entry_kwargs(entry, index) for index, entry in enumerate(entries))
    return [kwargs for kwargs in parsed if kwargs is not None]


def _entry_kwargs(entry: Any, index: int) -> dict[str, Any] | None:
    """Parse one `gee:visualizations` entry into `Visualization` kwargs, or `None`.

    `None` for an entry that is not a mapping or names no band, since a
    rendering with no bands cannot be drawn and `Visualization` requires them.

    Args:
        entry: One element of the `gee:visualizations` list.
        index: Its position, used to name an entry with no `display_name`.

    Returns:
        The constructor kwargs, or `None` to drop the entry.
    """
    result: dict[str, Any] | None = None
    band_vis = {}
    if isinstance(entry, dict):
        band_vis = (entry.get("image_visualization") or {}).get("band_vis") or {}
    bands = tuple(str(b) for b in (band_vis.get("bands") or []) if b)
    if bands:
        kwargs: dict[str, Any] = {
            "name": str(entry.get("display_name") or f"Visualization {index + 1}"),
            "bands": bands,
        }
        for stac_key, field in _NUMERIC_KEYS.items():
            values = _as_float_list(band_vis.get(stac_key))
            if values:
                kwargs[field] = tuple(values)
        palette = tuple(
            str(c).lstrip("#") for c in (band_vis.get("palette") or []) if c
        )
        if palette:
            kwargs["palette"] = palette
        result = kwargs
    return result


def _scalar(value: float) -> str:
    """Render a float as the shortest YAML scalar that round-trips it."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def visualization_lines(entries: list[dict[str, Any]]) -> list[str]:
    """Render parsed renderings as the stanza's `visualizations:` YAML lines.

    Flow sequences are used for the numeric and band lists because that is how
    the surrounding catalog already writes short lists (`bbox: [-180, -56, …]`),
    and a quoted `name` because a display name routinely carries a colon
    (`"Precipitation (mm/hr)"`) or starts with a digit.

    **Band ids are quoted too**, for the reason `_hydrate._splice_bands` quotes
    its band keys: a band id is not always a YAML string. The band of
    `NASA/JPL/global_forest_canopy_height_2005` is literally `1`, which a bare
    flow entry parses as an integer and the loader then refuses against
    `tuple[str, ...]`. Quoting every id is simpler than predicting which ones
    YAML will reinterpret, and the band map above already does the same.

    Args:
        entries: The kwargs mappings `visualizations_from_stac` returned.

    Returns:
        The YAML lines, indented for a dataset stanza, or `[]` for no entries.

    Examples:
        - One RGB rendering:
            ```python
            >>> for line in visualization_lines(
            ...     [{"name": "RGB", "bands": ("B4", "B3"), "vmin": (0.0,), "vmax": (3000.0,)}]
            ... ):
            ...     print(line)
                visualizations:
                  - name: "RGB"
                    bands: ["B4", "B3"]
                    vmin: [0]
                    vmax: [3000]

            ```
    """
    lines: list[str] = []
    if entries:
        lines.append(f"{_FIELD_INDENT}visualizations:")
        for entry in entries:
            lines.append(f'{_FIELD_INDENT}  - name: "{entry["name"]}"')
            bands = ", ".join(f'"{b}"' for b in entry["bands"])
            lines.append(f"{_FIELD_INDENT}    bands: [{bands}]")
            for field in ("vmin", "vmax", "gamma", "gain"):
                values = entry.get(field)
                if values:
                    rendered = ", ".join(_scalar(v) for v in values)
                    lines.append(f"{_FIELD_INDENT}    {field}: [{rendered}]")
            palette = entry.get("palette")
            if palette:
                rendered = ", ".join(f'"{c}"' for c in palette)
                lines.append(f"{_FIELD_INDENT}    palette: [{rendered}]")
    return lines


def splice_visualizations(block: str, entries: list[dict[str, Any]]) -> str:
    """Insert or replace a stanza's `visualizations:` block.

    The block is placed immediately before `bands:`, which keeps the
    dataset-level fields together and leaves the (much longer) band map last,
    as every hand-written stanza already has it. An existing
    `visualizations:` block is replaced rather than duplicated, so the
    hydration is idempotent and safe to re-run.

    Args:
        block: The dataset's stanza text, from its `  <asset id>:` line to the
            line before the next stanza.
        entries: The renderings to write; `[]` removes an existing block and
            otherwise leaves the stanza untouched.

    Returns:
        The stanza with the block spliced in, or unchanged when there is
        nothing to write and nothing to remove.

    Examples:
        - Inserted ahead of `bands:`:
            ```python
            >>> stanza = '  A/B:\\n    title: t\\n    bands:\\n      b1:\\n        min: 0\\n'
            >>> print(splice_visualizations(stanza, [{"name": "RGB", "bands": ("b1",)}]))
              A/B:
                title: t
                visualizations:
                  - name: "RGB"
                    bands: ["b1"]
                bands:
                  b1:
                    min: 0
            <BLANKLINE>

            ```
        - Re-running replaces rather than duplicates:
            ```python
            >>> once = splice_visualizations(stanza, [{"name": "RGB", "bands": ("b1",)}])
            >>> twice = splice_visualizations(once, [{"name": "Grey", "bands": ("b1",)}])
            >>> twice.count("visualizations:")
            1
            >>> 'name: "Grey"' in twice
            True

            ```
        - Nothing to write leaves the stanza alone:
            ```python
            >>> splice_visualizations(stanza, []) == stanza
            True

            ```
    """
    # `(?m)` only -- deliberately **not** `(?s)`. With DOTALL the `.*\n` below
    # matches across newlines and the removal eats the rest of the stanza, band
    # map included.
    existing = re.compile(
        rf"(?m)^{_FIELD_INDENT}visualizations:[ \t]*\n"
        rf"(?:{_FIELD_INDENT}[ \t]+\S.*\n)*"
    )
    stripped = existing.sub("", block, count=1)
    if not entries:
        result = stripped
    else:
        rendered = "\n".join(visualization_lines(entries)) + "\n"
        anchor = re.compile(rf"(?m)^{_FIELD_INDENT}bands:")
        match = anchor.search(stripped)
        if match:
            result = stripped[: match.start()] + rendered + stripped[match.start() :]
        else:
            result = stripped.rstrip("\n") + "\n" + rendered
    return result


def _stac_doc(asset_id: str) -> dict[str, Any] | None:
    """Fetch an asset's public STAC document, or `None` when it is not served.

    Args:
        asset_id: The Earth Engine asset id.

    Returns:
        The STAC document, or `None` for an asset the public tree does not
        carry (every community `projects/...` id) or a failed read.
    """
    from earthlens.gee.cli import _gee_stac_or_none

    return _gee_stac_or_none(asset_id)


def _stanza_bounds(text: str, asset_id: str) -> tuple[int, int] | None:
    """Locate one dataset stanza's character span inside a catalog file."""
    head = re.compile(rf"(?m)^ {{2}}{re.escape(asset_id)}:[ \t]*$")
    match = head.search(text)
    bounds: tuple[int, int] | None = None
    if match is not None:
        nxt = re.compile(r"(?m)^ {2}[A-Za-z0-9/_.-]+:[ \t]*$")
        following = nxt.search(text, match.end())
        bounds = match.start(), (following.start() if following else len(text))
    return bounds


def bulk_hydrate_visualizations(
    limit: int | None = None, *, write: bool = False
) -> dict[str, Any]:
    """Capture `gee:visualizations` for every curated row the public STAC serves.

    Walks the loaded catalog, reads each asset's public STAC document, and
    splices the renderings into its stanza. Idempotent: a row that already
    carries a block has it replaced with the freshly read one, so re-running
    after an upstream change converges rather than accumulating.

    Args:
        limit: Stop after this many curated datasets, or `None` for all of
            them. Useful for a smoke run before committing to ~1100 reads.
        write: Whether to write the files back. `False` reports what would
            change without touching the tree.

    Returns:
        A report with `considered`, `with_visualizations`, `without`,
        `unserved` (no public STAC document), `files` (the per-file change
        counts) and `wrote`.
    """
    from earthlens.gee.catalog import CATALOG_PATH, Catalog

    catalog_dir = Path(CATALOG_PATH)
    catalog_dir = catalog_dir if catalog_dir.is_dir() else catalog_dir.parent
    asset_ids = list(Catalog().datasets)
    if limit is not None:
        asset_ids = asset_ids[:limit]

    per_file: dict[Path, str] = {}
    counts: dict[str, int] = {}
    with_visualizations = 0
    without = 0
    unserved = 0
    for asset_id in asset_ids:
        doc = _stac_doc(asset_id)
        if doc is None:
            unserved += 1
            continue
        entries = visualizations_from_stac(doc)
        if not entries:
            without += 1
            continue
        path = _find_catalog_file(catalog_dir, asset_id)
        if path is None:
            continue
        text = per_file.get(path) or path.read_text(encoding="utf-8")
        bounds = _stanza_bounds(text, asset_id)
        if bounds is None:
            continue
        start, end = bounds
        spliced = splice_visualizations(text[start:end], entries)
        per_file[path] = text[:start] + spliced + text[end:]
        counts[path.name] = counts.get(path.name, 0) + 1
        with_visualizations += 1

    if write:
        for path, text in per_file.items():
            path.write_text(text, encoding="utf-8")
    return {
        "considered": len(asset_ids),
        "with_visualizations": with_visualizations,
        "without": without,
        "unserved": unserved,
        "files": counts,
        "wrote": write,
    }


def _find_catalog_file(catalog_dir: Path, asset_id: str) -> Path | None:
    """Return the per-family `catalog/*.yaml` file holding `asset_id`'s stanza."""
    from earthlens.gee._hydrate import _find_file_for_asset

    return _find_file_for_asset(catalog_dir, asset_id)
