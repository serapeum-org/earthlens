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

Two caveats worth knowing at the call site:

* The extra does **not** replace `[gee]`. A request is still built through
  `earthengine-api` before its pixels are fetched, so Earth Engine
  credentials remain required.
* pyramids-eo configures GDAL's EEDAI credentials process-globally for the
  duration of a read, so two `GEE` instances using different service
  accounts concurrently in one process can race over which identity is in
  effect. Sequential reads are unaffected — each configures its own.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

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

    Examples:
        - Read one asset's pixels straight through the reader:
            ```python
            >>> from earthlens.gee._eedai import import_earthengine_reader
            >>> reader = import_earthengine_reader()  # doctest: +SKIP
            >>> dataset = reader.from_earthengine(  # doctest: +SKIP
            ...     "USGS/SRTMGL1_003",
            ...     bands=["elevation"],
            ...     window=reader.Window(bbox=(31.25, 29.95, 31.3, 30.0)),
            ... )
            >>> dataset.shape  # doctest: +SKIP
            (1, 63, 62)

            ```
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

    Examples:
        - Pick the fetch engine from what is installed:
            ```python
            >>> from earthlens.gee._eedai import eedai_available
            >>> engine = "eedai" if eedai_available() else "ee"
            >>> engine in {"eedai", "ee"}
            True

            ```
    """
    try:
        importlib.import_module(_READER_MODULE)
    except ImportError:
        return False
    return True


def credentials_for(service_key: str | None) -> Any:
    """Adapt earthlens's resolved GEE `service_key` to a pyramids-eo credential.

    earthlens accepts either a path to a service-account JSON key or the
    key's JSON content inline — the two forms `earthlens.gee.auth` already
    understands — and pyramids-eo exposes a named constructor for each, plus
    Application Default Credentials when nothing is supplied.

    Args:
        service_key: Path to a service-account JSON key, that key's JSON
            content, or `None` to fall back to Application Default
            Credentials.

    Returns:
        The `pyramids_eo.earthengine.EarthEngineCredentials` for this key.

    Raises:
        ImportError: If `pyramids-eo` (the `[eedai]` extra) is not installed.

    Examples:
        - A key file on disk resolves to a service-account credential:
            ```python
            >>> from earthlens.gee._eedai import credentials_for
            >>> credentials_for("/keys/ee-sa.json")  # doctest: +SKIP
            <EarthEngineCredentials ...>

            ```
        - The key's JSON content works too, for secrets kept out of files:
            ```python
            >>> import json
            >>> payload = json.dumps({"type": "service_account"})
            >>> credentials_for(payload)  # doctest: +SKIP
            <EarthEngineCredentials ...>

            ```
    """
    credentials = import_earthengine_reader().EarthEngineCredentials
    if service_key is None:
        return credentials.application_default()
    if service_key.lstrip().startswith("{"):
        return credentials.from_service_account_info(service_key)
    return credentials.from_service_account(service_key)
