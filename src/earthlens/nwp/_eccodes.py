"""Make the pip-installed ecCodes binary loadable on Windows.

The NWP backend decodes GRIB through `pyramids.grib.open_grib`, whose import
chain (`cfgrib` -> `eccodes`) needs the native ecCodes C library. The pip
`eccodes` binding gets that library from the `eccodeslib` package on Linux and
macOS, but `eccodeslib` is **not published for Windows**, so a pure-pip Windows
install raises `RuntimeError: Cannot find the ecCodes library` the moment GRIB
is decoded.

`ecmwflibs` (ECMWF's own binary-bundle package) *does* ship a Windows
`eccodes.dll`, but the modern `eccodes` binding no longer consults it. This
module bridges the two: it puts the `ecmwflibs` directory on the DLL search
path (so `eccodes.dll`'s sibling dependencies resolve) and routes the binding
through `findlibs`, which then finds the DLL on `PATH`.

It is a deliberate no-op when the library is already locatable (a conda or
system ecCodes — the pixi `dev` / `notebook` envs), off Windows (Linux / macOS
get the binary from `eccodeslib`), or when `ecmwflibs` is not installed.
"""

from __future__ import annotations

import os

_done = False


def ensure_eccodes() -> None:
    """Wire the pip `eccodes` binding to the `ecmwflibs` library on Windows.

    Idempotent and best-effort: runs its work at most once per process, and
    silently leaves the environment untouched whenever it cannot or need not
    act (see the module docstring for the no-op cases). Call it immediately
    before the first `cfgrib` / `eccodes` import (i.e. before
    `pyramids.grib.open_grib`).
    """
    global _done
    if _done:
        return
    _done = True

    if os.name != "nt":
        return

    # Already resolvable (conda / system ecCodes)? Leave it alone.
    try:
        import findlibs

        if findlibs.find("eccodes"):
            return
    except ImportError:
        pass

    # Otherwise, wire up the ecmwflibs-bundled Windows binary if present.
    try:
        import ecmwflibs
    except ImportError:
        return

    lib = ecmwflibs.find("eccodes")
    if not lib:
        return
    lib_dir = os.path.dirname(lib)
    os.add_dll_directory(lib_dir)
    os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")
    # Route the binding through findlibs (which now finds the DLL on PATH). Set
    # unconditionally: the binding does `int(os.environ[var])` and crashes on a
    # present-but-empty value, so a bare `setdefault` is not enough.
    os.environ["ECCODES_PYTHON_USE_FINDLIBS"] = "1"
