"""Registered backend table and entry-point discovery for the `EarthLens` facade.

This module is deliberately **import-light**: it holds only a plain data table
and the `importlib.metadata` lookup, and imports no provider SDK. That is what
lets `EarthLens.DataSources` stay lazy — resolving an entry point costs one
small module import, never a backend's optional dependency.

Facade-key grammar: a key is either a bare **source/brand** name (`chc`, `cmems`,
`gebco`) or a qualified **`source:topic`** key (`dem:elevation`,
`jrc:sea-level-forecast`). A generic topic word (a `RESERVED_TOPICS` member) is
never a bare key — several sources can serve the same subject only when each
qualifies it, so a bare topic word can never be silently owned by one backend.
"""

from __future__ import annotations

from collections.abc import Iterable
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

#: Generic domain words that must never be a *bare* facade key. A reserved word
#: names a subject several providers could serve (`elevation`, `precipitation`,
#: `sea-level-forecast`), so it is reachable only in qualified `source:topic`
#: form — `dem:elevation`, not a bare `elevation` that one arbitrary backend
#: owns. Requesting a bare reserved word raises `AmbiguousDataSourceError`; a
#: provider table registering one bare fails the registration guard. The set is
#: seeded with the words in the registry today plus common subjects not yet
#: claimed, so a future provider cannot squat one.
RESERVED_TOPICS: frozenset[str] = frozenset(
    {
        "elevation",
        "insar",
        "bare-earth-dem",
        "human-settlement",
        "climate-projections",
        "teleconnections",
        "european-flood-hazard",
        "sea-level-forecast",
        "coastal-forecast",
        "twl-forecast",
        "solar-pv",
        "precipitation",
        "temperature",
        "discharge",
        "streamflow",
        "wind",
        "sea-surface-temperature",
        "soil-moisture",
        "evapotranspiration",
        "snow",
        "humidity",
        "air-quality",
        "land-cover",
    }
)


class AmbiguousDataSourceError(ValueError):
    """Raised when a bare generic topic word is requested as a `data_source`.

    A generic domain word (a `RESERVED_TOPICS` member) is never a bare key — it
    is reachable only in qualified `source:topic` form, so several sources can
    serve the same subject without colliding. Subclasses `ValueError` so callers
    that already catch the facade's unknown-source `ValueError` keep working.
    """


def topic_claimants(keys: Iterable[str], topic: str) -> list[str]:
    """Return the sorted `source:topic` keys that serve a bare `topic`.

    A key's topic is the segment after its first `:` — the same definition the
    registration guard uses — so `foo:sea-surface-temperature` is a claimant of
    `sea-surface-temperature`, never of `temperature`.

    Args:
        keys: The registered facade keys to search.
        topic: A bare generic topic word (no `:` separator).

    Returns:
        list[str]: Every registered key of the form `<source>:<topic>`, sorted;
        empty when no source exposes the topic.
    """
    return sorted(key for key in keys if ":" in key and key.split(":", 1)[1] == topic)


def discover_backends() -> dict[str, BackendSpec]:
    """Merge the backend tables published by every installed provider package.

    Two provider distributions claiming the same key would otherwise resolve by
    `importlib.metadata` iteration order, which is not a stable contract — the
    same install could dispatch a key to different backends on different
    machines. A collision is a packaging mistake, so it is logged as a warning
    naming both entry points and the winner, rather than resolved silently.

    A bare `RESERVED_TOPICS` word is also warned about: the in-repo tables never
    register one (a test enforces it), but an out-of-tree provider could, so the
    invariant is checked at discovery too rather than only in this repo's tests.

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
    for key in merged:
        if ":" not in key and key in RESERVED_TOPICS:
            logger.warning(
                f"backend key {key!r} (from {source[key]!r}) is a reserved "
                f"generic topic word; register it as '<source>:{key}', not bare. "
                f"A bare reserved key is a packaging mistake — qualify it."
            )
    return merged
