"""Administrative-boundary source catalog for the admin backend.

The admin backend fetches administrative-boundary polygons from four public
sources — geoBoundaries (per-country ADM0–ADM5), CGAZ (seamless global ADM0/1/2),
Natural Earth (global cultural admin layers), and US Census TIGER/Line
(states / counties / tracts / nation). The set is small and slow-changing, so it
is curated as config-as-code in the bundled `catalog/` directory — per-provider
`*.yaml` files (`geoboundaries.yaml`, `cgaz.yaml`, `natural_earth.yaml`,
`tiger.yaml`) plus an `_index.yaml` carrying the informational
`available_datasets:` list — and validated here against typed pydantic rows. The
loader merges every file at construction time (the ghsl / bathymetry sharded
pattern) through a `(path, mtime_ns)` parse cache.

Every shipped row is reached through one of the four transports pinned in the A1
gate; the actual URL resolution
lives in `earthlens.admin._helpers`, and the read always goes through pyramids
`FeatureCollection.read_file` (never a bare `geopandas.read_file`). `GADM` is
deliberately omitted (its no-commercial / no-redistribute license is
incompatible).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-provider `*.yaml` files plus the
#: `_index.yaml` informational index. Tests can monkey-patch this attribute to
#: redirect the loader at a temporary directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-provider file invalidates the entry without re-parsing an unchanged tree.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset]]] = CatalogParseCache()

#: The four administrative-boundary providers a catalog row may declare.
Provider = Literal["geoboundaries", "cgaz", "tiger", "natural_earth"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys include
    every contributing file's `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One curated administrative-boundary dataset row.

    A single row model spans all four providers; the provider-specific fields
    (`adm_level`, `layer`, `resolution`, `default_scale`, `default_year`,
    `per_state`) default to `None` / `False` and only the ones a provider needs
    are populated in its YAML file.

    Attributes:
        id: Catalog key for the row (`"geoboundaries:adm1"`, `"tiger:county"`).
            Set from the catalog key by the loader.
        title: Human-readable one-line description.
        provider: Which source serves the dataset — `"geoboundaries"`,
            `"cgaz"`, `"natural_earth"`, or `"tiger"`.
        adm_level: ADM level for geoBoundaries / CGAZ rows (`"ADM1"`); `None`
            for the layer-named providers.
        layer: Layer name fragment — the Natural Earth file stem fragment
            (`"admin_0_countries"`) or the TIGER entity (`"county"`); `None`
            for geoBoundaries / CGAZ.
        resolution: TIGER cartographic-boundary resolution (`"500k"`, `"5m"`);
            `None` for the other providers.
        default_scale: Natural Earth default scale (`"110m"` / `"50m"` /
            `"10m"`) used when the request gives no `scale=`; `None` otherwise.
        default_year: TIGER default vintage year used when the request gives no
            `year=`; `None` otherwise.
        per_state: `True` for a TIGER entity served per-state (`tract`), which
            needs a `state=` FIPS selector.
        required_selectors: The selector names the request must supply for this
            dataset — `("country",)` for geoBoundaries, `("state",)` for TIGER
            tracts, empty for the seamless / nationwide rows.
        native_crs: The source CRS — `"EPSG:4326"`, `"EPSG:4269"` (TIGER /
            NAD83), or `"undefined"` (CGAZ's unlabelled geographic-degree CRS).
            The backend normalises every result to EPSG:4326.
        url_template: The URL / API template the helper formats; informational
            for geoBoundaries (whose two-step resolve uses the module constant).
        license_note: Attribution / license text surfaced in docs and logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    title: str = ""
    provider: Provider
    adm_level: str | None = None
    layer: str | None = None
    resolution: str | None = None
    default_scale: str | None = None
    default_year: int | None = None
    per_state: bool = False
    required_selectors: tuple[str, ...] = ()
    native_crs: str = "EPSG:4326"
    url_template: str
    license_note: str = ""


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='admin', shard_noun='per-provider')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Dataset]]:
    """Parse, validate, and cache the admin catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (an id declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`, so a second `Catalog()` on an unchanged tree skips both
    YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Dataset]]: The merged `available_datasets:`
            index and the curated dataset map (keyed by id).

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
                    f"dataset {code!r} declared in two catalog files: "
                    f"{origin[code]} and {file_path}"
                )
            merged_datasets_yaml[code] = body
            origin[code] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The admin catalog must list at least one boundary dataset."
        )

    available = set(merged_available)
    datasets: dict[str, Dataset] = {}
    for code, body in merged_datasets_yaml.items():
        try:
            datasets[code] = Dataset(id=code, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[code]} dataset {code!r} failed validation:\n{exc}"
            ) from exc
        if available and code not in available:
            raise ValueError(
                f"dataset {code!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[code]}); add it to "
                "_index.yaml too."
            )

    _CATALOG_CACHE[key] = (merged_available, datasets)
    return _CATALOG_CACHE[key]


class Catalog(AbstractCatalog):
    """Administrative-boundary source catalog for the admin backend.

    Merges the bundled `catalog/` directory's per-provider `*.yaml` files and
    exposes their `datasets:` blocks as a map of `Dataset` rows keyed by id
    under the inherited `datasets` field (giving `cat["geoboundaries:adm1"]`,
    `"tiger:county" in cat`, `len(cat)`, and the did-you-mean error for free).
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads and
    validates the catalog through the parse cache.

    Attributes:
        datasets: Map from dataset id to its `Dataset` row.
        available_datasets: Every dataset id from `_index.yaml`. For admin the
            curated set is the full shipped surface, so this equals the curated
            keys.
    """

    _catalog_kind: str = "admin boundary catalog"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    _alias_index: dict[str, str] = PrivateAttr(default_factory=dict)

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
        """Read the admin catalog from disk (directory or single file).

        Args:
            catalog_path: Catalog directory or single YAML file. Defaults to
                the module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If no file has a `datasets:` block, an id is declared
                in two files, a row fails `Dataset` validation, or a curated id
                is absent from `available_datasets:`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, datasets = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(datasets),
            available_datasets=list(available),
        )

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the dataset map (satisfies the abstract contract)."""
        return self.datasets

    def get(self, dataset_id: str) -> Dataset:
        """Return the `Dataset` for a curated id, did-you-mean on miss.

        Args:
            dataset_id: A curated dataset id (`"geoboundaries:adm1"`,
                `"tiger:county"`, `"natural_earth:countries"`).

        Returns:
            Dataset: The matching row.

        Raises:
            ValueError: If `dataset_id` is not a curated dataset; the message
                lists the known ids with a did-you-mean hint.

        Examples:
            - A known id resolves to its row:
                ```python
                >>> from earthlens.admin import Catalog
                >>> Catalog().get("geoboundaries:adm1").provider
                'geoboundaries'
                >>> Catalog().get("tiger:tract").per_state
                True

                ```
        """
        return cast("Dataset", self.get_dataset(dataset_id))
