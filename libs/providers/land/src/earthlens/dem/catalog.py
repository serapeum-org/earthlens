"""Dataset catalog for the Copernicus DEM backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`dem_data_catalog.yaml`. The catalog is a small single-family map from a
dataset key (`cop-dem-glo-30`, `cop-dem-glo-90`) to the anonymous S3
bucket, region, and the resolution token that appears in the tile key.

The DEM backend is one-axis: a request picks one dataset key and a bbox;
the tile enumeration is arithmetic on the integer-degree grid. There are
no variables (a Copernicus DEM tile is one elevation band), so the
catalog has no per-variable sub-block.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "dem_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level DEM catalog parse cache."""
    _CATALOG_CACHE.clear()


class DEMDataset(BaseModel):
    """One Copernicus DEM dataset row.

    A frozen value object that pins the exact S3 bucket, region, and the
    resolution token embedded in every tile key (`_10_` for GLO-30 or
    `_30_` for GLO-90 — the tokens Copernicus uses in the object names,
    not the pixel size).

    Attributes:
        key: Dataset key (`"cop-dem-glo-30"`, `"cop-dem-glo-90"`) — the
            value passed to `EarthLens(..., dataset=...)`.
        long_name: Human-readable label used in docs and logs.
        bucket: Anonymous S3 bucket that holds the tiles.
        region: AWS region of the bucket (`"eu-central-1"`).
        resolution_token: The `{token}` fragment in the tile file name
            (`"10"` for GLO-30, `"30"` for GLO-90).
        tile_degrees: Edge length of one tile in degrees (`1`).
        native_resolution_m: Approximate pixel size in metres (30 / 90).
        vertical_datum: Vertical datum of the elevation values.
        horizontal_datum: Horizontal datum of the tile grid.
        attribution: Required attribution string; surfaced in docs.
        description: Human-readable summary of the dataset.

    Examples:
        - Load the shipped GLO-30 row:
            ```python
            >>> from earthlens.dem import Catalog
            >>> row = Catalog().get_dataset("cop-dem-glo-30")
            >>> row.bucket
            'copernicus-dem-30m'
            >>> row.resolution_token
            '10'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    long_name: str = ""
    bucket: str
    region: str = "eu-central-1"
    resolution_token: str
    tile_degrees: int = 1
    native_resolution_m: int = 0
    vertical_datum: str = ""
    horizontal_datum: str = ""
    attribution: str = ""
    description: str = ""


def _parse_datasets(files: list[Path]) -> dict[str, DEMDataset]:
    """Parse the DEM catalog's `datasets:` block into validated rows.

    Args:
        files: The contributing YAML files (DEM ships a single file).

    Returns:
        dict[str, DEMDataset]: One row per DEM dataset key. The rows are
            cached, not a built Catalog, so `load()` makes a fresh instance per
            call and one caller doing `datasets.pop(...)` cannot reach another's
            mapping. The row objects inside it *are* shared and are frozen
            pydantic models: treat them as read-only. A frozen model still
            permits in-place mutation of a mutable field (`row.columns[...] =`),
            which would reach every holder — deep-copying every row per load
            would cost more than that edge is worth.

    Raises:
        ValueError: If the `datasets:` block is missing or empty, or a row
            fails :class:`DEMDataset` validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The DEM catalog must list at least one dataset."
        )
    datasets: dict[str, DEMDataset] = {}
    for key, body in datasets_yaml.items():
        try:
            datasets[key] = DEMDataset(key=key, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {key!r} failed validation:\n{exc}"
            ) from exc
    return datasets


class Catalog(AbstractCatalog[DEMDataset]):
    """Dataset catalog for the DEM backend.

    Reads the bundled `dem_data_catalog.yaml` (shipped as package data)
    and exposes its `datasets:` block as a map of :class:`DEMDataset`
    rows keyed by dataset key under the inherited :attr:`datasets`
    field, which supplies the `cat["cop-dem-glo-30"]` /
    `"cop-dem-glo-30" in cat` / `len(cat)` dict-like surface and the
    did-you-mean error for free. Instantiate with no arguments
    (`Catalog()`); :func:`model_post_init` loads and validates the YAML
    in one pass and caches it by `(path, mtime)`.

    Attributes:
        datasets: Map from dataset key to its :class:`DEMDataset` row.
        available_datasets: Sorted dataset keys — the curated datasets
            are the whole dataset universe for this backend.

    Examples:
        - List datasets and resolve one:
            ```python
            >>> from earthlens.dem import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.datasets)
            ['cop-dem-glo-30', 'cop-dem-glo-90']
            >>> cat.get_dataset("cop-dem-glo-90").native_resolution_m
            90

            ```
        - An unknown key raises with a did-you-mean hint:
            ```python
            >>> from earthlens.dem import Catalog
            >>> Catalog().get_dataset("cop-dem-glo-3")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'cop-dem-glo-3' is not in the DEM catalog. ... Did you mean 'cop-dem-glo-30'?

            ```
    """

    _catalog_kind: str = "DEM catalog"

    datasets: dict[str, DEMDataset] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read
        (used in tests). Either way the `available_datasets` index is
        derived from the loaded map.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the DEM catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `datasets:` block, or any row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        rows = load_catalog(path, _CATALOG_CACHE, _parse_datasets, provider="DEM")
        return cls(datasets=dict(rows))
