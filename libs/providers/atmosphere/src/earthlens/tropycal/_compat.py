"""Compatibility shims for importing tropycal under modern toolchains.

tropycal 1.4 (the latest release) imports `pkg_resources` at module load —
a now-dead `import pkg_resources` in `tropycal/plot/plot.py` and a
`get_distribution(__package__).version` fallback in `tropycal/_version.py`.
`setuptools >= 81` removed `pkg_resources`, so a bare `import tropycal`
raises `ModuleNotFoundError: No module named 'pkg_resources'` on any
environment with a current setuptools.

Rather than pin `setuptools < 81` across the whole environment, this module
installs a minimal `pkg_resources` stand-in (only when the real one is
absent) exposing the single symbol tropycal actually uses, backed by the
standard-library `importlib.metadata`. `earthlens.tropycal` calls
`ensure_pkg_resources()` at import time, before the backend lazily imports
tropycal.
"""

from __future__ import annotations

import importlib.util
import sys
import types


def ensure_pkg_resources() -> None:
    """Install a minimal `pkg_resources` stand-in when setuptools dropped it.

    A no-op when `pkg_resources` is already importable (setuptools `< 81`, or
    a stand-in already registered). Otherwise registers a `pkg_resources`
    module in `sys.modules` exposing `get_distribution(name)` — the only
    symbol tropycal 1.4 reads — returning an object whose `version` comes
    from `importlib.metadata`.

    Idempotent and safe to call repeatedly.
    """
    if "pkg_resources" in sys.modules:
        return
    try:
        if importlib.util.find_spec("pkg_resources") is not None:
            return
    except (ImportError, ValueError):
        # A broken/partial install — fall through and provide the stand-in.
        pass

    import importlib.metadata as metadata

    shim = types.ModuleType("pkg_resources")

    def get_distribution(name: str):
        """Return a stub distribution exposing `.version` for `name`."""
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "0+unknown"
        return type("_Distribution", (), {"version": version})()

    shim.get_distribution = get_distribution
    shim.__earthlens_shim__ = True
    sys.modules["pkg_resources"] = shim
