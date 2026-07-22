"""The `earthlens providers …` command group.

Inspect the backend registry itself: which providers exist, the aliases
and pip extra for each, and — with `--check` — whether the optional SDK is
importable and how many datasets the bundled catalog carries.
"""

from __future__ import annotations

import json

import typer
from earthlens.cli.adapter import BackendInfo, list_backends, load_catalog
from earthlens.cli.render import out_console
from earthlens.earthlens import EarthLens
from rich.table import Table

#: Typer sub-application mounted at `earthlens providers`.
providers_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the earthlens provider backend registry.",
)


@providers_app.callback()
def providers() -> None:
    """Inspect the earthlens provider backend registry."""


def _sdk_available(info: BackendInfo) -> tuple[bool, str]:
    """Probe whether a backend's SDK imports (resolves its class).

    Args:
        info: The backend to probe.

    Returns:
        A `(available, detail)` pair: `detail` is empty on success, else a
        one-line reason (typically the missing SDK).
    """
    try:
        EarthLens.DataSources[info.aliases[0]]
    except Exception as exc:  # noqa: BLE001 — any import failure means "unavailable"
        return False, str(exc)
    return True, ""


def _dataset_count(info: BackendInfo) -> int | None:
    """Return the bundled catalog's dataset count, or None if it won't load."""
    try:
        return len(load_catalog(info).datasets)
    except Exception:  # noqa: BLE001 — count is best-effort
        return None


@providers_app.command("list")
def list_providers(
    check: bool = typer.Option(
        False,
        "--check",
        help="Probe each backend's SDK import and catalog size (slower).",
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit a JSON array (for piping)."
    ),
) -> None:
    """List the provider backends and how to install them.

    Shows each provider's id, registry aliases and pip extra. With
    `--check`, additionally imports each backend to report whether its
    optional SDK is available and how many datasets its catalog carries
    (this imports every backend, so it is noticeably slower).
    """
    infos = list_backends()
    records: list[dict[str, object]] = []
    for info in infos:
        record: dict[str, object] = {
            "provider": info.provider,
            "aliases": [a for a in info.aliases if a != info.provider],
            "extra": info.extra,
        }
        if check:
            available, detail = _sdk_available(info)
            record["sdk_available"] = available
            record["sdk_detail"] = detail
            record["datasets"] = _dataset_count(info)
        records.append(record)

    if json_output:
        typer.echo(json.dumps(records, indent=2))
        return

    table = Table(header_style="bold", show_lines=False)
    table.add_column("PROVIDER", overflow="fold")
    table.add_column("ALIASES", overflow="fold")
    table.add_column("EXTRA", overflow="fold")
    if check:
        table.add_column("SDK", overflow="fold")
        table.add_column("DATASETS", justify="right")
    for record in records:
        aliases = ", ".join(record["aliases"])
        cells = [record["provider"], aliases, record["extra"] or "-"]
        if check:
            cells.append("ok" if record["sdk_available"] else "missing")
            count = record["datasets"]
            cells.append("-" if count is None else str(count))
        table.add_row(*cells)
    out_console().print(table)
