"""Command-line interface for querying earthlens' federated data catalogs.

earthlens bundles a curated catalog for each of its provider backends
(CHC, ERA5 on S3, ECMWF, Google Earth Engine, CMEMS, NASA Earthdata, …).
This subpackage exposes those catalogs from the shell so a user can ask
"which provider(s) expose dataset X?" without writing any Python.

The entry point is the `earthlens` console script (installed with
`pip install earthlens[cli]`), which mounts two command groups:

* `earthlens datasets …` — federated queries over every backend's catalog
  (`where`, `search`, `list`, `show`, `facets`).
* `earthlens providers …` — inspect the backend registry itself
  (`list`).

The public surface is the Typer application object :data:`app` (and the
:func:`main` wrapper the console script calls).

Examples:
    - The application object is the console-script entry point:

        ```python
        >>> from earthlens.cli import app
        >>> type(app).__name__
        'Typer'

        ```
"""

from __future__ import annotations

from earthlens.cli.app import app, main

__all__ = [
    "app",
    "main",
]
