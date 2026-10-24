"""Guarded access to scikit-learn, which only the learning operations need.

Mirrors `gee/_eedai.py`: one function that imports and raises a friendly error
naming the extra, so a missing install is a readable message rather than a bare
`ModuleNotFoundError` from four frames deep. Importing this module is free; the
import happens when a learning operation actually runs.

scikit-learn is declared on the `[gee]` extra, so an install that has the Earth
Engine backend has it. The guard exists for the case that extra is partially
present - and because the two operations that need it should say so rather than
failing on an attribute.
"""

from __future__ import annotations

import importlib
from importlib.util import find_spec
from types import ModuleType

__all__ = ["import_cluster", "import_ensemble", "import_tree", "sklearn_available"]

_INSTALL_HINT = (
    "scikit-learn is required for this operation but is not installed. Install "
    "it with `pip install earthlens[gee]` (or, for the provider distribution "
    "alone, `pip install earthlens-imagery[gee]`)."
)


def _import(module: str) -> ModuleType:
    """Import `module`, or raise with the install hint.

    Args:
        module: The dotted module path to import.

    Returns:
        The imported module.

    Raises:
        ImportError: If scikit-learn is not installed; the message names the
            extra to install.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


def import_cluster() -> ModuleType:
    """Return `sklearn.cluster`, home of `KMeans`.

    Returns:
        The imported `sklearn.cluster` module.

    Raises:
        ImportError: If scikit-learn is not installed.
    """
    return _import("sklearn.cluster")


def import_ensemble() -> ModuleType:
    """Return `sklearn.ensemble`, home of `RandomForestClassifier`.

    Returns:
        The imported `sklearn.ensemble` module.

    Raises:
        ImportError: If scikit-learn is not installed.
    """
    return _import("sklearn.ensemble")


def import_tree() -> ModuleType:
    """Return `sklearn.tree`, home of `DecisionTreeClassifier`.

    Returns:
        The imported `sklearn.tree` module.

    Raises:
        ImportError: If scikit-learn is not installed.
    """
    return _import("sklearn.tree")


def sklearn_available() -> bool:
    """Return whether scikit-learn can be imported.

    Returns:
        `True` when the package is installed. Used to decide whether a test or
        a caller can take the learning path at all, without catching an
        exception to find out.
    """
    # `find_spec` is imported by name rather than reached as
    # `importlib.util.find_spec`: `importlib.util` is a submodule, so that
    # attribute exists only once something has imported it. It happens to be
    # preloaded in a dev environment, which is why the original spelling worked
    # here and would have raised `AttributeError` on a clean interpreter.
    return find_spec("sklearn") is not None
