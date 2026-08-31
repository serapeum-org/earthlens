"""Process-wide directories for earthlens output and cached intermediates.

earthlens writes two different kinds of file, and they belong in two different
places:

- **Output** — the products a caller asked for. A backend given an explicit
  `path=` writes there; otherwise the location is resolved by `output_dir()`,
  so a whole project can be pointed at one location (a NAS mount, say) without
  threading `path=` through every call.
- **Cache** — raw intermediates a backend downloads on the way to that output:
  archives it unzips, `.osm.pbf` extracts, GRIB index sidecars, catalog CSVs.
  These are regenerable, so they are kept out of the output tree and resolved
  by `cache_dir()`. Backends that expose a `cache_dir=` argument still let a
  caller override it per request.

Keeping them apart matters: a cleanup script that deletes anything called a
cache must not take requested products with it.

Both settings are process-wide module state with no locking. Set them once at
process start, before any backend is constructed: a backend captures its output
directory at construction, so changing a setting mid-run splits output across
two locations rather than raising.

Resolution order for each, highest priority first:

| | output | cache |
|---|---|---|
| 1 | `set_output_dir(...)` | `set_cache_dir(...)` |
| 2 | `EARTHLENS_DATA_DIR` | `EARTHLENS_CACHE` |
| 3 | `~/.earthlens/data` | the per-platform user cache directory |

Example:
    ```python
    from earthlens.core import EarthLens, set_output_dir

    set_output_dir("/data/earthlens")             # or: set EARTHLENS_DATA_DIR=...
    EarthLens(data_source="chc", ...).download()  # writes under /data/earthlens/chc
    ```
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

OUTPUT_DIR_ENV = "EARTHLENS_DATA_DIR"
"""Environment variable naming the output directory, read when no override is set."""

CACHE_DIR_ENV = "EARTHLENS_CACHE"
"""Environment variable naming the cache directory, read when no override is set."""

_output_override: Path | None = None
"""The `set_output_dir()` override, or `None` when no override is active."""

_cache_override: Path | None = None
"""The `set_cache_dir()` override, or `None` when no override is active."""


def _resolve(path: str | os.PathLike[str]) -> Path:
    """Expand `~` and make `path` absolute without touching the filesystem."""
    return Path(path).expanduser().resolve()


def set_output_dir(path: str | os.PathLike[str] | None) -> None:
    """Set the process-wide directory downloads are written to.

    Takes precedence over the `EARTHLENS_DATA_DIR` environment variable. Only
    `None` clears a previous override; every other value is treated as a
    directory, so `""` means the current working directory rather than a reset.

    Args:
        path: The directory to use, or `None` to clear a previous override and
            fall back to the environment variable (then the built-in default).
            `~` is expanded and the value is made absolute, so a relative value
            is anchored to the current working directory. The directory is not
            created here; it is created lazily when a download first writes to
            it.

    Examples:
        - Point every backend at one directory, then clear the override:
            ```python
            >>> from earthlens.config import output_dir, set_output_dir
            >>> set_output_dir("/data/earthlens")
            >>> output_dir().name
            'earthlens'
            >>> set_output_dir(None)

            ```
        - The override wins over whatever the environment says:
            ```python
            >>> from earthlens.config import output_dir, set_output_dir
            >>> set_output_dir("/data/first")
            >>> output_dir().name
            'first'
            >>> set_output_dir("/data/second")
            >>> output_dir().name
            'second'
            >>> set_output_dir(None)

            ```

    See Also:
        output_dir: Reads back the directory this function sets.
        set_cache_dir: The equivalent for regenerable intermediates.
    """
    global _output_override
    _output_override = None if path is None else _resolve(path)


def output_dir() -> Path:
    """Resolve the directory earthlens downloads are written to.

    Resolution order: the `set_output_dir()` override, then the
    `EARTHLENS_DATA_DIR` environment variable, then `~/.earthlens/data`.

    Returns:
        The resolved absolute directory. It is *not* created here; backends
        create it lazily on the first download that writes to it.

    Examples:
        - Read back the directory the next download will write to:
            ```python
            >>> from earthlens.config import output_dir, set_output_dir
            >>> set_output_dir("/data/earthlens")
            >>> output_dir().is_absolute()
            True
            >>> output_dir().name
            'earthlens'
            >>> set_output_dir(None)

            ```
        - Resolving the directory never creates it on disk:
            ```python
            >>> from earthlens.config import output_dir, set_output_dir
            >>> set_output_dir("/data/earthlens-not-created-by-resolving")
            >>> output_dir().exists()
            False
            >>> set_output_dir(None)

            ```

    See Also:
        set_output_dir: Sets the override this function reads first.
        earthlens.base.AbstractDataSource: Falls back to this directory when it
            is constructed without an explicit `path`.
    """
    if _output_override is not None:
        return _output_override
    env = os.environ.get(OUTPUT_DIR_ENV)
    if env:
        return _resolve(env)
    return _resolve(Path.home() / ".earthlens" / "data")


def resolve_output_path(path: str | os.PathLike[str] | None) -> Path:
    """Resolve a backend's `path=` argument to an absolute output directory.

    The single place the `path=` contract is implemented, so every backend
    resolves it identically.

    Args:
        path: The backend's `path=` argument. `None` means "not given" and
            falls back to `output_dir()`. Any other value is a directory;
            surrounding whitespace is stripped, `~` is expanded, and an empty
            result means the current working directory. The path is made
            absolute rather than resolved, so a mapped drive or a junction the
            caller typed is handed back as typed.

    Returns:
        The absolute output directory. It is not created here.

    Examples:
        - An omitted path follows the configured output directory:
            ```python
            >>> from earthlens.config import resolve_output_path, set_output_dir
            >>> set_output_dir("/data/earthlens")
            >>> resolve_output_path(None).name
            'earthlens'
            >>> set_output_dir(None)

            ```
        - An explicit value wins, and is made absolute:
            ```python
            >>> from earthlens.config import resolve_output_path, set_output_dir
            >>> set_output_dir("/data/earthlens")
            >>> resolve_output_path("/tmp/here").is_absolute()
            True
            >>> resolve_output_path("/tmp/here").name
            'here'
            >>> set_output_dir(None)

            ```

    See Also:
        output_dir: The fallback used when `path` is `None`.
    """
    if path is None:
        return output_dir()
    return Path(str(path).strip() or ".").expanduser().absolute()


def set_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """Set the process-wide directory regenerable intermediates are cached in.

    Takes precedence over the `EARTHLENS_CACHE` environment variable. Only
    `None` clears a previous override; every other value is treated as a
    directory, so `""` means the current working directory rather than a reset.

    This is the root each backend hangs its own subdirectory off — the archives,
    extracts and index sidecars it downloads on the way to producing output. It
    is not where requested products land; that is `output_dir()`.

    Args:
        path: The directory to use, or `None` to clear a previous override and
            fall back to the environment variable (then the built-in default).
            `~` is expanded and the value is made absolute. The directory is not
            created here; each backend creates its own subdirectory on first use.

    Examples:
        - Point every backend's intermediates at one directory:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens-cache")
            >>> cache_dir().name
            'earthlens-cache'
            >>> set_cache_dir(None)

            ```
        - Backends hang their own subdirectory off it:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens-cache")
            >>> (cache_dir() / "osm_pbf").name
            'osm_pbf'
            >>> set_cache_dir(None)

            ```

    See Also:
        cache_dir: Reads back the directory this function sets.
        set_output_dir: The equivalent for requested products.
    """
    global _cache_override
    _cache_override = None if path is None else _resolve(path)


def cache_dir() -> Path:
    """Resolve the directory regenerable intermediates are cached in.

    Resolution order: the `set_cache_dir()` override, then the
    `EARTHLENS_CACHE` environment variable, then the per-platform user cache
    directory (`platformdirs.user_cache_dir`), which is the correct spelling on
    Windows as well as Linux and macOS.

    Backends append their own name, so two backends never share a cache tree.

    Returns:
        The resolved absolute directory. It is *not* created here; each backend
        creates its own subdirectory on first use.

    Examples:
        - Read back the cache root a backend will hang its subdirectory off:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens-cache")
            >>> cache_dir().is_absolute()
            True
            >>> cache_dir().name
            'earthlens-cache'
            >>> set_cache_dir(None)

            ```
        - Resolving the directory never creates it on disk:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens-cache-not-created")
            >>> cache_dir().exists()
            False
            >>> set_cache_dir(None)

            ```

    See Also:
        set_cache_dir: Sets the override this function reads first.
        output_dir: Where requested products land, as opposed to intermediates.
    """
    if _cache_override is not None:
        return _cache_override
    env = os.environ.get(CACHE_DIR_ENV)
    if env:
        return _resolve(env)
    return _resolve(platformdirs.user_cache_dir("earthlens", appauthor=False))
