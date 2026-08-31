"""Property / depth / quantile catalog for the SoilGrids backend.

SoilGrids 2.0 publishes a fixed set of numeric soil properties (clay, sand,
silt, cfvo, phh2o, cec, nitrogen, soc, ocd, ocs, bdod), each as its own OGC WCS
service serving the standard six depth intervals × the `Q0.05 / Q0.5 / Q0.95 /
mean / uncertainty` layers (the `ocs` carbon-stock property is the exception —
a single `0-30cm` interval). That surface is small and slow-changing, so it is
curated as config-as-code in the bundled `catalog/` directory — per-group
`*.yaml` files (`texture.yaml`, `chemistry.yaml`, `carbon.yaml`,
`physical.yaml`) plus an `_index.yaml` carrying the informational
`available_datasets:` list — and validated here against typed pydantic rows.
The loader merges every file at construction time (the `ghsl` / `cmems` /
`solar_wind_atlas` sharded pattern) through a `(path, mtime_ns)` parse cache.

A request names one or more property ids plus optional depths / quantiles;
`Catalog.get` returns a property's row (did-you-mean on a miss) and
`Catalog.parameters` lists the curated ids. Each `Property` row records the WCS
`endpoint`, the published `depths` + `quantiles`, and the scaled-integer unit
metadata (`unit` / `mapped_units` / `scale_factor`) — SoilGrids stores every
value as an integer that must be divided by `scale_factor` to reach the
conventional `unit` (verified live 2026-07-01; see
the A1 gate captures).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-group `*.yaml` files plus the
#: `_index.yaml` informational index. Override this attribute to redirect the
#: loader at another directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-group file invalidates the entry without re-parsing an unchanged tree.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Property]]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Forces a re-parse after the catalog files on disk change. Production
    callers do not need this — the cache keys include every contributing
    file's `st_mtime_ns`, so any real file mutation invalidates the entry on
    its own.
    """
    _CATALOG_CACHE.clear()


class Property(BaseModel):
    """One curated SoilGrids soil property (an independent WCS service).

    Attributes:
        id: Catalog key / property id (`"clay"`, `"phh2o"`). Set from the
            catalog key by the loader.
        endpoint: The property's MapServer WCS endpoint
            (`".../mapserv?map=/map/clay.map"`); a `(property, depth,
            quantile)` request resolves to one `COVERAGEID` served here.
        title: Human-readable property title from the catalog (e.g.
            `"Soil pH in H2O"`).
        depths: The published depth intervals (`["0-5cm", ...]`; a single
            `["0-30cm"]` for the `ocs` carbon-stock property).
        quantiles: The published quantile / layer tokens (`["Q0.05", "Q0.5",
            "Q0.95", "mean", "uncertainty"]`).
        unit: The conventional unit of the values after rescaling (`"pH"`,
            `"%"`, `"g/kg"`).
        mapped_units: The unit the raw stored integers are in (`"pH*10"`,
            `"g/kg"`, `"cg/kg"`) before dividing by `scale_factor`.
        scale_factor: Divide a stored pixel value by this to convert
            `mapped_units` to `unit` (e.g. `10` for pH, `100` for nitrogen).
        license_note: Attribution / licence text (CC-BY 4.0, ISRIC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    endpoint: str
    title: str = ""
    depths: list[str] = Field(min_length=1)
    quantiles: list[str] = Field(min_length=1)
    unit: str = ""
    mapped_units: str = ""
    scale_factor: float = 1.0
    license_note: str = "CC-BY 4.0 (c) ISRIC - World Soil Information"


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='soilgrids', shard_noun='per-group')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Property]]:
    """Parse, validate, and cache the SoilGrids catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (an id declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`, so a second `Catalog()` on an unchanged tree skips both
    YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Property]]: The merged `available_datasets:`
            index and the curated property map (keyed by id).

    Raises:
        ValueError: If no file has a `datasets:` block, an id is declared in
            two files, a row fails validation, or a curated id is absent from
            `available_datasets:`.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    merged_available: list[str] = []
    merged_datasets_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for code, body in (data.get("datasets") or {}).items():
            if code in merged_datasets_yaml:
                raise ValueError(
                    f"property {code!r} declared in two catalog files: "
                    f"{origin[code]} and {file_path}"
                )
            merged_datasets_yaml[code] = body
            origin[code] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The soilgrids catalog must list at least one property."
        )

    available = set(merged_available)
    datasets: dict[str, Property] = {}
    for code, body in merged_datasets_yaml.items():
        try:
            datasets[code] = Property(id=code, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[code]} property {code!r} failed validation:\n{exc}"
            ) from exc
        if available and code not in available:
            raise ValueError(
                f"property {code!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[code]}); add it to "
                "_index.yaml too."
            )

    _CATALOG_CACHE[key] = (merged_available, datasets)
    return _CATALOG_CACHE[key]


class Catalog(AbstractCatalog):
    """Property / depth / quantile catalog for the SoilGrids backend.

    Merges the bundled `catalog/` directory's per-group `*.yaml` files and
    exposes their `datasets:` blocks as a map of `Property` rows keyed by id
    under the inherited `datasets` field (giving `cat["clay"]`, `"clay" in
    cat`, `len(cat)`, and the did-you-mean error for free). Instantiate with no
    arguments (`Catalog()`); `model_post_init` loads and validates the catalog
    through the parse cache.

    Attributes:
        datasets: Map from property id to its `Property` row.
        available_datasets: Every property id from `_index.yaml`. The curated
            set is the full numeric-property WCS surface, so this equals the
            curated keys (there is no larger auto-discovered universe).
    """

    _catalog_kind: str = "SoilGrids property catalog"
    _entry_noun: str = "properties"

    datasets: dict[str, Property] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `available_datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "available_datasets": loaded.available_datasets,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the property catalog from disk (directory or single file).

        Merges every per-group `*.yaml` in the catalog directory (the curated
        `datasets:` blocks + the `_index.yaml` `available_datasets:` list)
        through the `(path, mtime_ns)`-keyed parse cache.

        Args:
            catalog_path: Catalog directory or single YAML file. Defaults to
                the module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If no file has a `datasets:` block, an id is declared
                in two files, a row fails `Property` validation, or a curated
                id is absent from `available_datasets:`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, datasets = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(datasets),
            available_datasets=list(available),
        )

    def get_catalog(self) -> dict[str, Property]:
        """Return the property map (satisfies the abstract contract)."""
        return self.datasets

    def get(self, property_id: str) -> Property:
        """Return the `Property` for a curated id, did-you-mean on miss.

        Args:
            property_id: A curated property id (`"clay"`, `"phh2o"`).

        Returns:
            Property: The matching row.

        Raises:
            ValueError: If `property_id` is not a curated property; the message
                lists the known ids with a did-you-mean hint.

        Examples:
            - A property resolves to its row (units + WCS endpoint):
                ```python
                >>> from earthlens.soilgrids import Catalog
                >>> Catalog().get("phh2o").unit
                'pH'
                >>> Catalog().get("ocs").depths
                ['0-30cm']

                ```
        """
        return cast("Property", self.get_dataset(property_id))

    def parameters(self) -> list[str]:
        """Return the curated property ids, sorted.

        Returns:
            list[str]: The curated property ids (`["bdod", "cec", ...]`).
        """
        return sorted(self.datasets)
