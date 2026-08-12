"""Entry-point discovery for the provider CLI-tooling tables.

The sibling of `earthlens._backends`, for the catalog-tooling half of the CLI
(`refresh` / `audit` / `probe` / `curate` / `validate`). Those commands are the
only part of the CLI that is provider-specific — the `where` / `search` /
`list` / `show` path already reaches every backend reflectively through
`earthlens.cli.adapter.load_catalog`.

Like `_backends`, this module is deliberately **import-light**: it holds only
the `importlib.metadata` lookup and a lazy target resolver, and imports no
provider package. Core defines the mechanism and the spec shape but owns no
table — each provider distribution publishes its own slice
(`earthlens._<theme>_cli:CLI_TOOLING`) under the `earthlens.cli` entry-point
group, so core's CLI names no backend and depends on no provider distribution.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

from loguru import logger

#: Group under which a provider distribution publishes its CLI-tooling table.
#: Each entry point resolves to a `dict[str, CliToolingSpec]` that is merged
#: into one registry, so a provider package registers every id it tools with a
#: single entry (mirroring the `earthlens.backends` group).
CLI_ENTRY_POINT_GROUP = "earthlens.cli"

#: `role -> target`, the tooling a provider wires up for one catalog id.
#:
#: A *callable* role (`refresher`, `writer`, `curated_ids`, `bundled_ids`,
#: `coverage`, `prober`, `deep_prober`, `validator`, `live_validator`,
#: `emitter`, `tile_regen`) names its handler as a `"module:attr"` string that
#: core imports lazily on dispatch — so resolving the entry point pulls in no
#: provider SDK. A *config* role (`index_attr`, `stanza_block`, …) carries a
#: literal string the command reads directly. The role vocabulary is owned by
#: the CLI commands that consume the table, not by this module; discovery treats
#: every value opaquely.
CliToolingSpec = dict[str, str]


def discover_cli_tooling() -> dict[str, CliToolingSpec]:
    """Merge the CLI-tooling tables published by every installed provider.

    Two provider distributions claiming the same id would otherwise resolve by
    `importlib.metadata` iteration order, which is not a stable contract. A
    collision is a packaging mistake, so it is logged as a warning naming both
    entry points and the winner, rather than resolved silently — the same rule
    `discover_backends` applies to backend keys.

    Returns:
        An `id -> CliToolingSpec` mapping union of every entry point in the
        `earthlens.cli` group. On a duplicate id the later entry wins, and the
        collision is warned about.
    """
    merged: dict[str, CliToolingSpec] = {}
    source: dict[str, str] = {}
    for ep in entry_points(group=CLI_ENTRY_POINT_GROUP):
        table = ep.load()
        for key, spec in table.items():
            if key in merged and source[key] != ep.value:
                logger.warning(
                    f"CLI-tooling id {key!r} is published by both "
                    f"{source[key]!r} and {ep.value!r}; {ep.value!r} wins. Two "
                    f"provider distributions tool the same id — the resolution "
                    f"depends on entry-point order, so fix the duplicate."
                )
            merged[key] = spec
            source[key] = ep.value
    return merged


def resolve_target(target: str) -> Any:
    """Import and return the object a `"module:attr"` spec target names.

    Args:
        target: A `"module:attr"` string, where `attr` may be dotted to reach a
            nested attribute (e.g. `"earthlens.gee.cli:categories.RULES"`).

    Returns:
        The resolved attribute — typically the provider's handler callable.

    Raises:
        ValueError: If `target` is not of the form `"module:attr"`.
    """
    module_name, sep, attr = target.partition(":")
    if not module_name or not sep or not attr:
        raise ValueError(
            f"CLI-tooling target {target!r} is not of the form 'module:attr'"
        )
    obj: Any = import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _thunk(target: str) -> Callable[..., Any]:
    """Wrap a `"module:attr"` target so the provider module imports on first call.

    Building a command's dispatch table must not import any provider handler
    module: the table is built while core's command module is still importing,
    and a handler module imports `earthlens.cli.toolkit` (hence back into the
    command module), so eager resolution would be a circular import. The thunk
    defers `resolve_target` to call time — i.e. when a command actually
    dispatches to that provider — by which point every module is fully loaded.

    Args:
        target: The `"module:attr"` handler target to resolve lazily.

    Returns:
        A callable that resolves `target` and forwards its arguments on the
        first (and every) call.
    """

    def handler(*args: Any, **kwargs: Any) -> Any:
        """Resolve the provider handler and forward the call to it."""
        return resolve_target(target)(*args, **kwargs)

    return handler


def dispatch_table(role: str) -> dict[str, Callable[..., Any]]:
    """Build a provider-id -> handler dict for one callable role, from discovery.

    Each handler is wrapped in a lazy thunk (see `_thunk`), so building the
    table imports only the import-light `CLI_TOOLING` tables, never a provider
    handler module or its SDK.

    Args:
        role: The tooling role to project (e.g. `"refresher"`, `"validator"`).

    Returns:
        An `id -> handler` mapping for every provider that publishes `role`.
    """
    return {
        key: _thunk(spec[role])
        for key, spec in discover_cli_tooling().items()
        if role in spec
    }


def config_table(role: str) -> dict[str, str]:
    """Build a provider-id -> literal-value dict for one config role, from discovery.

    The counterpart to `dispatch_table` for the non-callable roles (e.g.
    `index_attr`): the value is used verbatim, not imported.

    Args:
        role: The config role to project (e.g. `"index_attr"`).

    Returns:
        An `id -> value` mapping for every provider that publishes `role`.
    """
    return {
        key: spec[role] for key, spec in discover_cli_tooling().items() if role in spec
    }
