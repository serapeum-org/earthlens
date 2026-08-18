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
_DEFAULT = Path.home() / ".earthlens" / "cache"

_override: Path | None = None


def set_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """Set the process-wide default cache / output directory.

    Takes precedence over the `EARTHLENS_CACHE_DIR` environment variable. Pass
    `None` to clear a previous override and fall back to the environment
    variable (or the built-in default).

    Args:
        path: The directory to use, or `None` to clear a previous override.
            `~` is expanded and the value is resolved to an absolute path. The
            directory is not created here — it is created lazily when a
            download first writes to it.
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
    """
    if _override is not None:
        return _override
    env = os.environ.get(CACHE_DIR_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT.resolve()
