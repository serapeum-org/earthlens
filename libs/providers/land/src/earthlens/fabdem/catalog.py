"""Catalog loader for the FABDEM V1-2 backend.

FABDEM is a single, static, global product, so the catalog is one
`fabdem_data_catalog.yaml` at the package root holding the dataset row plus the
licence / attribution the non-commercial CC-BY-NC-SA terms require. It loads
through the shared strict YAML loader and the `(path, mtime)` parse cache, and
exposes the row via the inherited `AbstractCatalog` surface (`cat["fabdem"]`,
`get_dataset`, the did-you-mean error).

`CATALOG_PATH` is the bundled YAML; `clear_catalog_cache` empties the parse
cache (used by tests that monkey-patch `CATALOG_PATH`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "fabdem_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level FABDEM catalog parse cache."""
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One FABDEM product row.

    Attributes:
        id: The catalog key (`"fabdem"`).
        title: Human-readable product title.
        version: Published data version (`"V1-2"`).
        provider: Short provider token (`"fathom-bristol"`).
        band: The single elevation band name (`"elevation"`).
        long_name: Human-readable band description.
        units: Physical units of the band (`"m"`).
        dtype: Pixel data type (`"float32"`).
        crs: Native CRS as an EPSG string (`"EPSG:4326"`).
        nodata: The source tiles' no-data value (`-9999.0`), stamped on the
            mosaic so genuine 0 m (sea-level) cells are not lost.
        spatial_resolution: Nominal resolution in metres (`30`).
        source_url: The dataset landing page.

    Examples:
        - Read the single band and version:
            ```python
            >>> from earthlens.fabdem import Catalog
            >>> row = Catalog().get("fabdem")
            >>> row.band, row.version, row.units
            ('elevation', 'V1-2', 'm')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str = ""
    version: str = ""
    provider: str = ""
    band: str = "elevation"
    long_name: str = ""
    units: str = "m"
    dtype: str = "float32"
    crs: str = "EPSG:4326"
    nodata: float = -9999.0
    spatial_resolution: float | None = None
    source_url: str = ""


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the FABDEM catalog YAML into `Catalog` construction kwargs.

    Args:
        files: The contributing YAML files (FABDEM ships a single file).

    Returns:
        dict[str, Any]: Validated construction kwargs (`datasets`,
            `available_datasets`, `license_id`, `attribution`,
            `commercial_contact`).

    Raises:
        ValueError: When the `datasets:` block is missing / empty or a row
            fails validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The FABDEM catalog must list the fabdem product."
        )
    datasets: dict[str, Dataset] = {}
    for key, body in datasets_yaml.items():
        try:
            datasets[key] = Dataset(id=key, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {key!r} failed validation:\n{exc}"
            ) from exc
    return {
        "datasets": datasets,
        "available_datasets": sorted(datasets),
        "license_id": data.get("license", ""),
        "attribution": data.get("attribution", ""),
        "commercial_contact": data.get("commercial_contact", ""),
    }


class Catalog(AbstractCatalog):
    """Product catalog for the FABDEM backend.

    Reads the bundled `fabdem_data_catalog.yaml` and exposes its single row
    under the inherited `datasets` field — which supplies the `cat["fabdem"]` /
    `"fabdem" in cat` / `len(cat)` surface and the did-you-mean error for free.
    Instantiate with no arguments; the base `model_post_init` auto-loads via
    `_autoload`, cached by `(path, mtime)`.

    Attributes:
        datasets: Product key to its `Dataset` row.
        available_datasets: Sorted product keys.
        license_id: SPDX-ish licence label (`"CC-BY-NC-SA-4.0"`), fed to
            `warn_license`.
        attribution: The citation the licence requires downstream products to
            carry.
        commercial_contact: How to obtain a commercial (Fathom) licence.

    Examples:
        - List products and read the licence:
            ```python
            >>> from earthlens.fabdem import Catalog
            >>> cat = Catalog()
            >>> list(cat.datasets)
            ['fabdem']
            >>> cat.license_id
            'CC-BY-NC-SA-4.0'

            ```
    """

    _catalog_kind: str = "FABDEM catalog"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    license_id: str = ""
    attribution: str = ""
    commercial_contact: str = ""

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Return the disk payload to fill an empty catalog (base post-init hook).

        Returns:
            dict[str, Any]: The parsed field → value map from `_parse_catalog`.
        """
        return load_catalog(
            CATALOG_PATH, _CATALOG_CACHE, _parse_catalog, provider="FABDEM"
        )

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the FABDEM catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            Catalog: A fully-populated catalog.

        Raises:
            ValueError: If `catalog_path` does not exist, has no `datasets:`
                block, or the row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="FABDEM")
        return cls(**payload)

    def get(self, key: str) -> Dataset:
        """Return the `Dataset` for `key`, with a did-you-mean hint.

        Args:
            key: A product key (`"fabdem"`).

        Returns:
            Dataset: The matching product row.

        Raises:
            ValueError: If `key` is not a registered product.
        """
        return cast("Dataset", self.get_dataset(key))
