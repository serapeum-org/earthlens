"""Catalog for the FLOPROS flood-protection-standard backend.

The FLOPROS backend fetches one shapefile — the global FLOPROS database of
flood-protection standards (Scussolini et al., 2016) — so the catalog is a
single :class:`FloprosDataset` row under the inherited :attr:`datasets` map,
keyed `"flopros"`. The row carries the supplement-zip `url`, the
`shapefile_stem` inside it, the `identity_columns` kept on every polygon, and
the `layers:` map from a public layer name (`"merged_riverine"`) to its source
`.dbf` column (`"MerL_Riv"`), plus the top-level `license` / `attribution`.

:data:`CATALOG_PATH` is the path to the bundled YAML; it is monkey-patchable in
tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "flopros_data_catalog.yaml"

#: Module-level parse cache keyed on the resolved path plus the YAML's
#: `(mtime_ns, size)`, so a repeated `Catalog()` skips the parse + validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class FloprosDataset(BaseModel):
    """The FLOPROS shapefile's download + column spec.

    The dataset name (`"flopros"`) is the parent key in
    :attr:`Catalog.datasets`, not stored on the row.

    Attributes:
        url: Download URL of the NHESS supplement zip the shapefile lives in.
        shapefile_stem: The shapefile name (without extension) inside the zip;
            the `.shp` and its sidecars (`.dbf` / `.shx` / `.prj`) all share it.
        crs: The CRS the shapefile is tagged with (`"EPSG:4326"`).
        identity_columns: Non-value columns kept on every returned polygon
            (`name` / `geonunit` / `type_en`).
        layers: Public layer name -> source `.dbf` column (`"merged_riverine"`
            -> `"MerL_Riv"`); each value is a protection standard as a return
            period in years.

    Examples:
        - Read the merged-riverine source column:
            ```python
            >>> from earthlens.flopros import Catalog
            >>> Catalog().get("flopros").layers["merged_riverine"]
            'MerL_Riv'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    shapefile_stem: str
    crs: str = "EPSG:4326"
    identity_columns: list[str] = Field(default_factory=list)
    layers: dict[str, str] = Field(default_factory=dict)


def _load_catalog_data(path: Path) -> dict[str, Any]:
    """Parse, validate, and cache the catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        dict[str, Any]: Every field a :class:`Catalog` is built from.

    Raises:
        ValueError: If the file has no `datasets:` block or the row fails
            validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cast("dict[str, Any]", cached)

    data = load_yaml_strict(path) or {}
    rows_yaml = data.get("datasets") or {}
    if not rows_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. The FLOPROS "
            "catalog must list the 'flopros' dataset row."
        )
    datasets: dict[str, FloprosDataset] = {}
    for name, body in rows_yaml.items():
        try:
            datasets[name] = FloprosDataset(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {name!r} failed validation:\n{exc}"
            ) from exc

    value: dict[str, Any] = {
        "datasets": datasets,
        "license": str(data.get("license") or ""),
        "attribution": str(data.get("attribution") or ""),
    }
    _CATALOG_CACHE[key] = value
    return value


class Catalog(AbstractCatalog[FloprosDataset]):
    """Catalog for the FLOPROS flood-protection-standard backend.

    Reads the bundled `flopros_data_catalog.yaml` (shipped as package data) and
    exposes its single `flopros` row under the inherited :attr:`datasets` map,
    plus the `license` / `attribution`. Instantiate with no arguments
    (`Catalog()`); resolve the row with :meth:`get`.

    Attributes:
        datasets: Map with the single `"flopros"` -> :class:`FloprosDataset`
            row.
        license: SPDX-style redistribution licence (`"CC-BY-3.0"`).
        attribution: Required citation string.

    Examples:
        - Resolve the row and read its licence:
            ```python
            >>> from earthlens.flopros import Catalog
            >>> cat = Catalog()
            >>> cat.get("flopros").shapefile_stem
            'FLOPROS_shp_V1'
            >>> cat.license
            'CC-BY-3.0'

            ```
    """

    _catalog_kind: str = "FLOPROS catalog"
    _entry_noun: str = "datasets"

    datasets: dict[str, FloprosDataset] = Field(default_factory=dict)
    license: str = ""
    attribution: str = ""

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: Every field read from the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "license": loaded.license,
            "attribution": loaded.attribution,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the FLOPROS catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block or the row fails
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(**_load_catalog_data(catalog_path))

    def get(self, dataset: str = "flopros") -> FloprosDataset:
        """Resolve a dataset name to its :class:`FloprosDataset` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            dataset: The dataset name; only `"flopros"` is shipped.

        Returns:
            FloprosDataset: The matching catalog row.

        Raises:
            ValueError: If `dataset` is not a known dataset.
        """
        return cast("FloprosDataset", self.get_dataset(dataset))
