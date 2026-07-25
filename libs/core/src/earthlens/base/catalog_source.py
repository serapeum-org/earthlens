"""Shared plumbing for the per-provider catalog loaders.

Every one of the 48 provider `catalog.py` modules memoises its parsed rows the
same way: resolve `CATALOG_PATH`, work out which YAML files contribute, build a
`(path, mtime)` cache key from them, return the cached parse when the key still
matches, else parse and store. Only the *parse* differs between backends — the
row models are genuinely per-provider — so this module owns the ceremony around
it and leaves the parse alone.

What was duplicated before this module existed:

* `_yaml_files_for` — 18 copies of the same three-branch globber, differing only
  in the provider name and the "per-family" / "per-DAAC" noun in the error.
* the cache-key dance — 34 copies across two archetypes (a sharded directory
  keyed on every contributing file's `st_mtime_ns`, and a single file keyed on
  its own).
* `clear_catalog_cache` — 48 module-level functions, 44 of them byte-identical,
  with no way to clear them all at once.

The public entry point is :func:`load_catalog`, which composes all three. A
loader that needs something else can still call :func:`yaml_files_for` and
:func:`catalog_cache_key` directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from earthlens.base.yaml_loader import CatalogParseCache

T = TypeVar("T")

#: Every :class:`~earthlens.base.yaml_loader.CatalogParseCache` built anywhere in
#: the process, so :func:`clear_all_catalog_caches` can empty them in one call.
#: Registration happens in `CatalogParseCache.__init__`, so a loader that
#: declares its cache the normal way is covered without doing anything.
_REGISTRY: list[CatalogParseCache] = []


def register_cache(cache: CatalogParseCache) -> None:
    """Add `cache` to the registry cleared by :func:`clear_all_catalog_caches`.

    Args:
        cache: The parse cache to register. Registering the same cache twice is
            harmless — the registry is only ever iterated to call `.clear()`.
    """
    _REGISTRY.append(cache)


def clear_all_catalog_caches() -> None:
    """Empty every registered catalog parse cache.

    The per-module `clear_catalog_cache()` functions each empty exactly one
    cache, so a caller that rewrites several catalogs on disk previously had to
    know which modules to reach into — and there are 52 caches across 48
    modules. This clears all of them.

    Examples:
        - Clearing is idempotent and safe with no caches registered:
            ```python
            >>> from earthlens.base.catalog_source import clear_all_catalog_caches
            >>> clear_all_catalog_caches()
            >>> clear_all_catalog_caches()

            ```
    """
    for cache in _REGISTRY:
        cache.clear()


def yaml_files_for(
    path: Path,
    *,
    provider: str,
    shard_noun: str = "",
) -> list[Path]:
    """Return the sorted YAML files contributing to one catalog load.

    A catalog is stored either as a directory of per-family `*.yaml` shards (the
    layout for the large multi-family catalogs) or as a single
    `<pkg>_data_catalog.yaml`. Both load through here, and so does a test that
    monkey-patches `CATALOG_PATH` to a temp file.

    Args:
        path: The catalog directory or single YAML file.
        provider: Provider name for the error message (e.g. `"CMEMS"`).
        shard_noun: Optional description of how the directory is sharded, used
            only in the error text (e.g. `"per-domain"`). Empty for a catalog
            with no particular sharding convention.

    Returns:
        Sorted YAML paths — every `*.yaml` in the directory, or the single file.

    Raises:
        ValueError: If `path` is neither an existing directory nor an existing
            file, so a missing catalog fails with a clear message rather than an
            obscure read error later.

    Examples:
        - A directory yields its `*.yaml` children, sorted, ignoring other files:
            ```python
            >>> import tempfile
            >>> from pathlib import Path
            >>> from earthlens.base.catalog_source import yaml_files_for
            >>> directory = Path(tempfile.mkdtemp())
            >>> _ = (directory / "b.yaml").write_text("datasets: {}\\n")
            >>> _ = (directory / "a.yaml").write_text("datasets: {}\\n")
            >>> _ = (directory / "notes.txt").write_text("ignored\\n")
            >>> [p.name for p in yaml_files_for(directory, provider="Demo")]
            ['a.yaml', 'b.yaml']

            ```
        - A single file returns just itself:
            ```python
            >>> import tempfile
            >>> from pathlib import Path
            >>> from earthlens.base.catalog_source import yaml_files_for
            >>> one = Path(tempfile.mkdtemp()) / "one.yaml"
            >>> _ = one.write_text("datasets: {}\\n")
            >>> yaml_files_for(one, provider="Demo") == [one]
            True

            ```
        - A missing path names the provider and the expected layout:
            ```python
            >>> from pathlib import Path
            >>> from earthlens.base.catalog_source import yaml_files_for
            >>> try:
            ...     yaml_files_for(Path("no-such-catalog"), provider="Demo",
            ...                    shard_noun="per-family")
            ... except ValueError as exc:
            ...     print("per-family" in str(exc), "Demo" in str(exc))
            True True

            ```
    """
    if path.is_dir():
        return sorted(path.glob("*.yaml"))
    if path.is_file():
        return [path]
    shard = f"{shard_noun} " if shard_noun else ""
    raise ValueError(
        f"{provider} catalog path {path} does not exist (expected a directory "
        f"of {shard}*.yaml files, or a single YAML file)."
    )


def catalog_cache_key(path: Path, files: Sequence[Path]) -> tuple[Any, ...]:
    """Build the memoisation key for a catalog load.

    The key is the resolved catalog path plus every contributing file's
    `st_mtime_ns`, so editing any shard invalidates the entry without the loader
    having to compare parsed contents. The resolved path is always element 0,
    which is what lets :class:`CatalogParseCache` evict superseded generations
    for the same catalog.

    A file that disappears between the glob and the `stat` degrades to mtime `0`
    rather than raising: the load that follows will surface the real error.

    Args:
        path: The catalog directory or file (becomes element 0 of the key).
        files: The contributing YAML files, from :func:`yaml_files_for`.

    Returns:
        A hashable `(resolved_path, ((file, mtime_ns), ...))` key.

    Examples:
        - Touching a shard changes the key, so the parse is redone:
            ```python
            >>> import os, tempfile
            >>> from pathlib import Path
            >>> from earthlens.base.catalog_source import (
            ...     catalog_cache_key, yaml_files_for)
            >>> directory = Path(tempfile.mkdtemp())
            >>> shard = directory / "a.yaml"
            >>> _ = shard.write_text("datasets: {}\\n")
            >>> before = catalog_cache_key(directory, yaml_files_for(
            ...     directory, provider="Demo"))
            >>> os.utime(shard, (0, 0))
            >>> before == catalog_cache_key(directory, yaml_files_for(
            ...     directory, provider="Demo"))
            False

            ```
    """
    resolved = str(path.resolve())
    try:
        stamps = tuple((str(f), f.stat().st_mtime_ns) for f in files)
    except FileNotFoundError:
        stamps = ((resolved, 0),)
    return (resolved, stamps or ((resolved, 0),))


def load_catalog(
    path: Path,
    cache: CatalogParseCache,
    parse: Callable[[list[Path]], T],
    *,
    provider: str,
    shard_noun: str = "",
) -> T:
    """Return the parsed catalog at `path`, memoised on the files' mtimes.

    The composition every provider loader repeats: resolve the contributing
    files, build the key, return a live cache hit, else call `parse` and store
    the result. `parse` receives the file list and owns everything
    provider-specific — the row models, the merge across shards, the
    duplicate-key checks.

    Args:
        path: The catalog directory or single YAML file.
        cache: The module's :class:`CatalogParseCache`.
        parse: Callable taking the contributing files and returning the parsed
            catalog. Called only on a cache miss.
        provider: Provider name for the not-found error.
        shard_noun: Optional sharding description for that error.

    Returns:
        Whatever `parse` returned, from the cache when the mtimes are unchanged.

    Raises:
        ValueError: If `path` does not exist (see :func:`yaml_files_for`).

    Examples:
        - The parse runs once, then the cached value is reused:
            ```python
            >>> import tempfile
            >>> from pathlib import Path
            >>> from earthlens.base.yaml_loader import CatalogParseCache
            >>> from earthlens.base.catalog_source import load_catalog
            >>> one = Path(tempfile.mkdtemp()) / "c.yaml"
            >>> _ = one.write_text("datasets: {}\\n")
            >>> cache, calls = CatalogParseCache(), []
            >>> def parse(files):
            ...     calls.append(files)
            ...     return {"rows": len(files)}
            >>> load_catalog(one, cache, parse, provider="Demo")
            {'rows': 1}
            >>> load_catalog(one, cache, parse, provider="Demo")
            {'rows': 1}
            >>> len(calls)
            1

            ```
    """
    files = yaml_files_for(path, provider=provider, shard_noun=shard_noun)
    key = catalog_cache_key(path, files)
    cached = cache.get(key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    parsed = parse(files)
    cache[key] = parsed
    return parsed


def iter_registered_caches() -> Iterable[CatalogParseCache]:
    """Yield every registered parse cache (used by tests and tooling)."""
    return tuple(_REGISTRY)
