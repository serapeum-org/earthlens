"""Registered backend table and entry-point discovery for the `EarthLens` facade.

This module is deliberately **import-light**: it holds only a plain data table
and the `importlib.metadata` lookup, and imports no provider SDK. That is what
lets `EarthLens.DataSources` stay lazy — resolving an entry point costs one
small module import, never a backend's optional dependency.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from loguru import logger

#: Group under which a provider distribution publishes its backend table.
#: Each entry point resolves to a `dict[str, BackendSpec]` that is merged into
#: the facade's registry, so a provider package registers all of its keys with
#: a single entry.
ENTRY_POINT_GROUP = "earthlens.backends"

#: `key -> (module, class_name, extras_hint, default_kwargs)`.
#:
#: `extras_hint` names the pip extra that supplies the backend's SDK (empty when
#: it needs none); `default_kwargs` pre-binds constructor arguments for alias
#: keys (e.g. the STAC `"cdse"` alias binds `endpoint="cdse"`). Neither can be
#: expressed by an entry point's `module:attr` target, which is why an entry
#: point resolves to a whole mapping rather than to a backend class.
#:
#: Core defines the shape but owns no table: each provider distribution
#: publishes its own slice (`earthlens._<theme>:BACKENDS`), so core names no
#: backend and depends on no provider distribution.
BackendSpec = tuple[str, str, str, dict[str, object]]


def discover_backends() -> dict[str, BackendSpec]:
    """Merge the backend tables published by every installed provider package.

    Two provider distributions claiming the same key would otherwise resolve by
    `importlib.metadata` iteration order, which is not a stable contract — the
    same install could dispatch a key to different backends on different
    machines. A collision is a packaging mistake, so it is logged as a warning
    naming both entry points and the winner, rather than resolved silently.

    Returns:
        A `key -> BackendSpec` mapping union of every entry point in the
        `earthlens.backends` group. On a duplicate key the later entry wins, and
        the collision is warned about.
    """
    merged: dict[str, BackendSpec] = {}
    source: dict[str, str] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        table = ep.load()
        for key, spec in table.items():
            if key in merged and source[key] != ep.value:
                logger.warning(
                    f"backend key {key!r} is published by both {source[key]!r} "
                    f"and {ep.value!r}; {ep.value!r} wins. Two provider "
                    f"distributions claim the same key — the resolution depends "
                    f"on entry-point order, so fix the duplicate."
                )
            merged[key] = spec
            source[key] = ep.value
    return merged
