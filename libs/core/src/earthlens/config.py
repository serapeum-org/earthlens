"""Process-wide output / cache directory for earthlens downloads.

Every backend writes its downloads — and stages any raw granules — under a
single directory. When a backend is given an explicit `path=`, that wins; when
it is not, the directory is resolved here, so a user can point *all* of
earthlens at one location (for example a NAS mount) without threading `path=`
through every call.

Resolution order, highest priority first:

1. `set_cache_dir(...)` — an explicit runtime override.
2. the `EARTHLENS_CACHE_DIR` environment variable.
3. the fallback `~/.earthlens/cache`.

Example:
    ```python
    from earthlens.core import EarthLens, set_cache_dir

    set_cache_dir(r"D:\\earthlens-cache")   # or: set EARTHLENS_CACHE_DIR=...
    EarthLens(data_source="chc", ...).download()   # writes under D:\\earthlens-cache
    ```
"""

from __future__ import annotations

import os
from pathlib import Path

CACHE_DIR_ENV = "EARTHLENS_CACHE_DIR"
"""Name of the environment variable read when no override is set."""

_DEFAULT = Path.home() / ".earthlens" / "cache"
"""Fallback location used when neither an override nor the env var is set."""

_override: Path | None = None
"""The `set_cache_dir()` override, or `None` when no override is active."""


def set_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """Set the process-wide default cache / output directory.

    Takes precedence over the `EARTHLENS_CACHE_DIR` environment variable. Pass
    `None` to clear a previous override and fall back to the environment
    variable (or the built-in default).

    Args:
        path: The directory to use, or `None` to clear a previous override.
            `~` is expanded and the value is resolved to an absolute path, so a
            relative value is anchored to the current working directory. Any
            falsy value — `None` or an empty string — clears the override. The
            directory is not created here; it is created lazily when a download
            first writes to it.

    Examples:
        - Point every backend at one directory, then clear the override:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens")
            >>> cache_dir().name
            'earthlens'
            >>> set_cache_dir(None)

            ```
        - An override outranks the environment variable, and an empty value
          clears it again:
            ```python
            >>> import os
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> os.environ["EARTHLENS_CACHE_DIR"] = "/data/fallback"
            >>> set_cache_dir("/data/override")
            >>> cache_dir().name
            'override'
            >>> set_cache_dir("")
            >>> cache_dir().name
            'fallback'
            >>> _ = os.environ.pop("EARTHLENS_CACHE_DIR")

            ```

    See Also:
        cache_dir: Reads back the directory this function sets.
    """
    global _override
    _override = Path(path).expanduser().resolve() if path else None


def cache_dir() -> Path:
    """Resolve the earthlens cache / output directory.

    Resolution order: the `set_cache_dir()` override, then the
    `EARTHLENS_CACHE_DIR` environment variable, then `~/.earthlens/cache`.

    Returns:
        The resolved absolute directory. It is *not* created here; backends
        create it lazily on the first download that writes to it.

    Examples:
        - Read back the directory the next download will write to:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens")
            >>> cache_dir().is_absolute()
            True
            >>> cache_dir().name
            'earthlens'
            >>> set_cache_dir(None)

            ```
        - Resolving the directory never creates it on disk:
            ```python
            >>> from earthlens.config import cache_dir, set_cache_dir
            >>> set_cache_dir("/data/earthlens-not-created-by-resolving")
            >>> cache_dir().exists()
            False
            >>> set_cache_dir(None)

            ```

    See Also:
        set_cache_dir: Sets the override this function reads first.
        earthlens.base.AbstractDataSource: Falls back to this directory when
            it is constructed without an explicit `path`.
    """
    if _override is not None:
        return _override
    env = os.environ.get(CACHE_DIR_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT.resolve()
