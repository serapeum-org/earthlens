"""Lazy, guarded access to the optional `pyramids-eo` EEDAI reader.

The pyramids-eo Earth Engine reader (GDAL `EEDAI` / `EEDA` drivers, no
`earthengine-api`) is an *optional* acceleration for the GEE backend's raw,
no-compute fetch path: it materialises pixels straight from a real asset /
scene id, removing the `getDownloadURL` size cap and HTTP/zip round-trip. It
ships behind the `[eedai]` extra, not in the base install, so the backend
reaches it lazily.

Import it through :func:`import_earthengine_reader` so a missing install
surfaces as one clear, actionable error at the fetch seam rather than a bare
`ModuleNotFoundError` from deep in the pipeline.
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: The `pip` target that provides the reader (both the meta-package extra and
#: the provider-distribution extra activate `pyramids-eo`).
_INSTALL_HINT = (
    "the pyramids-eo EEDAI reader is required for this path but is not "
    "installed. Install it with `pip install earthlens[eedai]` (or, for the "
    "provider distribution alone, `pip install earthlens-imagery[eedai]`)."
)

#: The reader submodule that exposes the public entry points.
_READER_MODULE = "pyramids_eo.earthengine"


def import_earthengine_reader() -> ModuleType:
    """Return the `pyramids_eo.earthengine` module, or raise a friendly error.

    The module exposes the reader's public surface — `from_earthengine`,
    `collection_from_earthengine`, and `EarthEngineCredentials` — which the
    GEE backend's EEDAI fast-path builds on.

    Returns:
        The imported `pyramids_eo.earthengine` module.

    Raises:
        ImportError: If `pyramids-eo` (the `[eedai]` extra) is not installed;
            the message names the extra to install.
    """
    try:
        return importlib.import_module(_READER_MODULE)
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


def eedai_available() -> bool:
    """Return whether the optional `pyramids-eo` EEDAI reader is importable.

    A cheap, side-effect-free probe (it swallows the import error) for code
    that wants to choose the fast-path only when the extra is installed,
    without forcing the caller to handle :class:`ImportError`.

    Returns:
        `True` if `pyramids_eo.earthengine` imports, `False` otherwise.
    """
    try:
        importlib.import_module(_READER_MODULE)
    except ImportError:
        return False
    return True
