"""Stanza emitters — author a curated `datasets:` row from one upstream id.

The authoring companion to :mod:`earthlens.cli.curate` (which probes a
dataset's schema). Where `probe` prints the per-band/asset *schema seed*,
`curate` (this module) prints a ready-to-paste curated `datasets:` row:
it fetches one upstream id's metadata and transcribes the fields the
catalog's pydantic row model curates, inferring `output_kind` / `format`
/ band metadata where it can. The output is a **seed** — the maintainer
vets it before pasting into the per-family catalog file. This is the CLI
port of the `tools/*/refresh_*.py` `add-*` subcommands.

Like `refresh` / `probe`, only providers whose row can be seeded from a
public (or env-credentialed) source have an emitter wired up; others
report `unsupported`. Adding one is a single entry in :data:`_EMITTERS`.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from earthlens._cli_tooling import config_table, dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog


@dataclass
class StanzaResult:
    """A curated `datasets:` row authored for one upstream id.

    Attributes:
        provider: Canonical provider id.
        key: The friendly catalog key the row is filed under.
        upstream_id: The upstream id the row was seeded from.
        status: `"ok"`, `"unsupported"` (no emitter), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        row: The seeded row fields (empty unless `status == "ok"`).
    """

    provider: str
    key: str
    upstream_id: str
    status: str
    detail: str = ""
    row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The seeded row is nested under `row`:

                ```python
                >>> from earthlens.cli.stanza import StanzaResult
                >>> StanzaResult(
                ...     "usgs_water", "discharge", "00060", "ok",
                ...     row={"code": "00060"},
                ... ).to_dict()["row"]["code"]
                '00060'

                ```
        """
        return {
            "provider": self.provider,
            "key": self.key,
            "upstream_id": self.upstream_id,
            "status": self.status,
            "detail": self.detail,
            "row": self.row,
        }

    def to_yaml(self) -> str:
        """Render the row as a paste-ready `datasets:` YAML fragment.

        Returns:
            The `datasets: {key: row}` block, or `""` when no row was seeded.

        Examples:
            - A seeded row renders under `datasets:`:

                ```python
                >>> from earthlens.cli.stanza import StanzaResult
                >>> print(StanzaResult(
                ...     "usgs_water", "discharge", "00060", "ok",
                ...     row={"code": "00060"},
                ... ).to_yaml().strip())
                datasets:
                  discharge:
                    code: '00060'

                ```
        """
        if not self.row:
            return ""
        return cast(
            "str",
            yaml.safe_dump(
                {"datasets": {self.key: self.row}},
                sort_keys=False,
                allow_unicode=True,
            ),
        )


# --------------------------------------------------------------------------- #
# usgs_water — a pure friendly-name -> parameter-code row (no fetch).
# --------------------------------------------------------------------------- #


#: Provider id -> a callable taking the loaded catalog, the upstream id, and
#: per-provider keyword options, returning the seeded curated row.
_EMITTERS: dict[str, Callable[..., dict[str, Any]]] = {
    # Wholly discovery-driven: merged from each provider's `earthlens.cli` table.
    **dispatch_table("emitter"),
}


def supported_providers() -> list[str]:
    """Return the provider ids that have a stanza emitter wired up.

    Returns:
        The sorted provider ids `curate` can author a row for.

    Examples:
        - The wired-up ids come back as a sorted list:

            ```python
            >>> from earthlens.cli.stanza import supported_providers
            >>> ids = supported_providers()
            >>> ids == sorted(ids)
            True

            ```
    """
    return sorted(_EMITTERS)


def emit_stanza(
    info: BackendInfo,
    upstream_id: str,
    *,
    key: str | None = None,
    minimal: bool = False,
    **opts: Any,
) -> StanzaResult:
    """Author a curated `datasets:` row for one upstream id.

    A provider with no emitter returns `"unsupported"`; any fetch / parse
    failure returns `"error"` — neither raises.

    Args:
        info: The backend the dataset belongs to.
        upstream_id: The upstream id to seed the row from.
        key: The friendly catalog key (defaults to `upstream_id`).
        minimal: Emit a placeholder row without a live fetch where the
            emitter supports it (e.g. GEE's empty-bands stanza).
        **opts: Per-provider options (e.g. earthdata `version` /
            `cmr_provider`, usgs `name` / `units` / `services`).

    Returns:
        The :class:`StanzaResult`.
    """
    resolved_key = key or upstream_id
    emitter = _EMITTERS.get(info.provider)
    if emitter is None:
        return StanzaResult(
            provider=info.provider,
            key=resolved_key,
            upstream_id=upstream_id,
            status="unsupported",
            detail="no stanza emitter wired up for this provider",
        )
    try:
        catalog = load_catalog(info)
        row = emitter(catalog, upstream_id, key=resolved_key, minimal=minimal, **opts)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return StanzaResult(
            provider=info.provider,
            key=resolved_key,
            upstream_id=upstream_id,
            status="error",
            detail=str(exc),
        )
    return StanzaResult(
        provider=info.provider,
        key=resolved_key,
        upstream_id=upstream_id,
        status="ok",
        row=row,
    )


#: Provider id -> the YAML block its curated rows live under.
_STANZA_BLOCK: dict[str, str] = config_table("stanza_block")


def _append_to_block(path: Path, block: str, key: str, row: dict[str, Any]) -> None:
    """Append a `{key: row}` entry under `block:` in `path`, preserving the rest.

    Splices the new entry in at the end of the existing `block:` block (or
    appends a fresh `block:` at end of file), leaving every other line —
    header comments, sibling blocks, the other rows — byte-for-byte intact.

    Args:
        path: The catalog YAML file to edit.
        block: The top-level block the row belongs under (`datasets` /
            `parameters`).
        key: The friendly catalog key for the new row.
        row: The seeded row fields.

    Raises:
        ValueError: If `key` is already curated in `path`.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed = (yaml.safe_load(text) or {}) if text.strip() else {}
    if key in (parsed.get(block) or {}):
        raise ValueError(f"{key!r} is already curated in {path.name}")
    dumped = yaml.safe_dump({key: row}, sort_keys=False, allow_unicode=True)
    entry = "".join(
        ("  " + line if line.strip() else line)
        for line in dumped.splitlines(keepends=True)
    )
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{block}:")), None
    )
    if start is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        path.write_text(f"{prefix}{block}:\n{entry}", encoding="utf-8")
        return
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*:", lines[j]):
            end = j
            break
    lines.insert(end, entry)
    path.write_text("".join(lines), encoding="utf-8")


def write_stanza(info: BackendInfo, result: StanzaResult, target: str | None) -> str:
    """Insert a seeded row into the curated catalog file (the `--write` half).

    Appends `result.row` under the provider's block (`parameters:` for
    usgs_water, else `datasets:`). Sharded catalogs need a `target` file
    stem (the per-family file under `catalog/`); single-file catalogs
    ignore it.

    Args:
        info: The backend the row belongs to.
        result: The `ok` :class:`StanzaResult` to persist.
        target: The per-family file stem (sharded catalogs only).

    Returns:
        The path of the catalog file written.

    Raises:
        ValueError: If a sharded catalog is missing `target`, or the key is
            already curated.
    """
    base = importlib.import_module(f"{info.module}.catalog").CATALOG_PATH
    block = _STANZA_BLOCK.get(info.provider, "datasets")
    if base.is_dir():
        if not target:
            # A provider that ships a categoriser (upstream_id, title -> shard
            # stem) auto-picks its per-family file; discovered via `earthlens.cli`.
            categoriser = dispatch_table("categoriser").get(info.provider)
            if categoriser is not None:
                target = categoriser(
                    result.upstream_id, str(result.row.get("title", ""))
                )
        if not target:
            raise ValueError(
                f"{info.provider} has a sharded catalog; pass --target <file-stem> "
                "(the per-family file under catalog/) to write the row"
            )
        path = base / f"{target}.yaml"
    else:
        path = base
    _append_to_block(path, block, result.key, result.row)
    return str(path)
