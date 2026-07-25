"""Strict YAML loading shared by the package's variable/data catalogs.

The catalogs under `earthlens` (`cds_data_catalog.yaml` for the ECMWF
backend, the per-category YAMLs under `earthlens/gee/catalog/` for the
GEE backend, ...) are
hand-maintained config-as-code. PyYAML's default `SafeLoader` silently
merges duplicate mapping keys (last one wins), which would let a
copy-paste typo — two identical variable/band codes under the same
dataset — slip through with the first silently shadowed. This module
provides a loader that fails loud at parse time with a `ValueError`
naming the offending line, plus a small `load_yaml_strict` helper, so
every catalog gets the same guarantee from one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class CatalogParseCache(dict):
    """Parse cache that keeps only the newest entry per catalog path.

    Every backend's `catalog.py` memoises its parsed rows under a
    `(resolved_path, mtime)` key so editing the YAML invalidates the entry
    without re-parsing on every `Catalog()`. Keyed that way a plain `dict`
    grows without bound: each edit adds a key and the superseded parse is never
    evicted, so a long-lived process that regenerates a catalog (the `earthlens
    datasets refresh` tooling, a test suite rewriting a temp catalog) retains
    every past version of it.

    This keeps the memoisation but drops the stale generations: writing a key
    evicts any other key for the same path, so a given catalog holds at most
    one parse. The eviction reads `key[0]` — the resolved path string every
    backend puts first, whether the rest of the key is a single mtime or a
    per-shard tuple.

    Examples:
        - A newer mtime for the same path replaces the older entry:
            ```python
            >>> from earthlens.base.yaml_loader import CatalogParseCache
            >>> cache = CatalogParseCache()
            >>> cache[("/catalog.yaml", 1)] = "first parse"
            >>> cache[("/catalog.yaml", 2)] = "second parse"
            >>> list(cache)
            [('/catalog.yaml', 2)]

            ```
        - Entries for different paths coexist:
            ```python
            >>> from earthlens.base.yaml_loader import CatalogParseCache
            >>> cache = CatalogParseCache()
            >>> cache[("/a.yaml", 1)] = "a"
            >>> cache[("/b.yaml", 1)] = "b"
            >>> sorted(cache)
            [('/a.yaml', 1), ('/b.yaml', 1)]

            ```
        - Re-reading a cached entry is still a plain dict hit:
            ```python
            >>> from earthlens.base.yaml_loader import CatalogParseCache
            >>> cache = CatalogParseCache()
            >>> cache[("/a.yaml", 1)] = "parsed"
            >>> cache[("/a.yaml", 1)]
            'parsed'

            ```
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the cache and register it for `clear_all_catalog_caches`.

        The import is deferred to call time because `catalog_source` imports this
        module; doing it at module scope would be a cycle.
        """
        super().__init__(*args, **kwargs)
        from earthlens.base.catalog_source import register_cache

        register_cache(self)

    def __setitem__(self, key: Any, value: Any) -> None:
        """Store `value`, evicting any other entry for the same catalog path."""
        if isinstance(key, tuple) and key:
            path = key[0]
            for existing in [
                k
                for k in self
                if isinstance(k, tuple) and k and k[0] == path and k != key
            ]:
                super().__delitem__(existing)
        super().__setitem__(key, value)


class _StrictSafeLoader(yaml.SafeLoader):
    """:class:`yaml.SafeLoader` that rejects duplicate keys in any mapping.

    Behaves like `SafeLoader` (no arbitrary object instantiation) except
    that a mapping declaring the same key twice raises a `ValueError`
    pinpointing the line/column rather than silently keeping the last
    value.
    """


def _construct_mapping_no_duplicates(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    """Build a dict from a YAML mapping node, rejecting duplicate keys.

    Replaces :meth:`yaml.SafeLoader.construct_mapping` for
    :class:`_StrictSafeLoader` so every mapping in a catalog YAML (the
    dataset map, each dataset's `variables:` / `bands:` block, every
    `extras:` map, ...) is required to have unique keys.

    Args:
        loader: The active strict loader instance.
        node: The YAML mapping node being constructed.
        deep: Whether to construct child nodes eagerly (passed through
            to :meth:`yaml.Loader.construct_object`).

    Returns:
        The mapping as a plain `dict`.

    Raises:
        ValueError: If the same key appears more than once in the
            mapping; the message includes the line/column of the
            duplicate.
    """
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ValueError(
                f"duplicate YAML key {key!r} at line {mark.line + 1}, "
                f"column {mark.column + 1} of {mark.name}: every key in a "
                "YAML mapping must be unique (in particular, every variable "
                "or band code must be unique within its dataset's block)"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def load_yaml_strict(path: str | Path) -> Any:
    """Parse a YAML file, rejecting duplicate mapping keys.

    A thin wrapper over `yaml.load(..., Loader=_StrictSafeLoader)` so
    callers (the catalog loaders) never touch the loader class directly.

    Args:
        path: Filesystem path to the YAML file.

    Returns:
        The parsed YAML (typically a `dict`), or `None` for an empty
        file.

    Raises:
        ValueError: If any mapping in the file declares a key twice.

    Examples:
        - Parse a small YAML file and read a value:
            ```python
            >>> import os, tempfile, textwrap
            >>> p = os.path.join(tempfile.mkdtemp(), "ok.yaml")
            >>> _ = open(p, "w").write(textwrap.dedent('''
            ...     name: demo
            ...     items:
            ...       - a
            ...       - b
            ... '''))
            >>> data = load_yaml_strict(p)
            >>> data["name"]
            'demo'
            >>> data["items"]
            ['a', 'b']

            ```
        - A duplicate mapping key is rejected at parse time:
            ```python
            >>> import os, tempfile, textwrap
            >>> p = os.path.join(tempfile.mkdtemp(), "dup.yaml")
            >>> _ = open(p, "w").write(textwrap.dedent('''
            ...     a: 1
            ...     a: 2
            ... '''))
            >>> load_yaml_strict(p)  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: duplicate YAML key 'a' at line 3, ...

            ```

    See Also:
        earthlens.ecmwf.catalog.Catalog: Uses this to load the CDS catalog.
        earthlens.gee.catalog.Catalog: Uses this to load the GEE catalog.

    """
    with open(path, encoding="utf-8") as stream:
        # `_StrictSafeLoader` subclasses `yaml.SafeLoader` (no arbitrary
        # object instantiation); bandit's B506 flags any `yaml.load`.
        return yaml.load(stream, Loader=_StrictSafeLoader)  # nosec B506
