"""Species dispatch table for the OBIS occurrence backend.

OBIS occurrence search keys on a `scientificname` string, so this
"catalog" is a small curated map from a friendly name (`"blue-whale"`,
`"ocean-sunfish"`, …) to its scientific name, plus a `resolve_scientific_name`
helper that also accepts an explicit `species:<name>` selector. There is no
`refresh` / `probe` tooling and no `tools/obis/` directory — the curated rows
are a hand-edit of one YAML file (`earthlens datasets validate obis` confirms
the names upstream).

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass
storing the rows in the framework's :attr:`~earthlens.base.AbstractCatalog.datasets`
field, so the inherited dict-like surface (`len(cat)`, `name in cat`,
`cat[name]`, :meth:`get_dataset` with a did-you-mean hint) works. Parsing is
cached on `(path, mtime)`; :data:`CATALOG_PATH` is the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "obis_data_catalog.yaml"

#: Prefix marking an explicit scientific-name selector in `variables`.
SPECIES_PREFIX = "species:"

# Module-level cache of parsed catalog rows, keyed on the resolved path plus the
# YAML's `st_mtime_ns`, so editing the file invalidates the entry. Mirrors the
# FDSN / GBIF catalog loaders.
_CATALOG_CACHE: dict[tuple[str, int], tuple[dict[str, Species], list[str]]] = (
    CatalogParseCache()
)


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    the file's `st_mtime_ns`, so any real edit invalidates the entry.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> tuple[dict[str, Species], list[str]]:
    """Parse, validate, and cache the OBIS species catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        A pair `(rows, available)` of the friendly-name `Species` map and the
        informational `available_datasets:` index of common marine higher-rank
        taxa.

    Raises:
        ValueError: If the file has no `species:` block, or a row fails
            :class:`Species` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    species_yaml = data.get("species") or {}
    if not species_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'species:' block. "
            "The OBIS catalog must list at least one species."
        )
    rows: dict[str, Species] = {}
    for name, body in species_yaml.items():
        try:
            rows[name] = Species(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} species {name!r} failed validation:\n{exc}"
            ) from exc

    available = [str(item) for item in (data.get("available_datasets") or [])]
    _CATALOG_CACHE[key] = (rows, available)
    return rows, available


class Species(BaseModel):
    """One friendly OBIS species' dispatch row.

    The user-facing name is the parent key in :attr:`Catalog.datasets`
    and is not stored on the row.

    Attributes:
        scientific_name: OBIS `scientificname` the occurrence search
            filters on (e.g. `"Delphinus delphis"`).
        title: Human-readable common name used in logs and docs.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.obis import Species
            >>> s = Species(scientific_name="Mola mola", title="Ocean sunfish")
            >>> s.scientific_name
            'Mola mola'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scientific_name: str
    title: str = ""


class Catalog(AbstractCatalog[Species]):
    """Species catalog for the OBIS occurrence backend.

    Reads the bundled `obis_data_catalog.yaml` (shipped as package data)
    and exposes its `species:` block as a map of :class:`Species` rows.
    Instantiate with no arguments (`Catalog()`); :func:`model_post_init`
    loads and validates the YAML (cached) in one pass. The rows live in the
    framework's :attr:`datasets` field so the inherited dict-like surface
    behaves like every other backend's catalog.

    Attributes:
        datasets: Map from the friendly species name to its
            :class:`Species` row (the framework field).

    Examples:
        - The dict-like surface and name resolution work like GBIF's:
            ```python
            >>> from earthlens.obis import Catalog
            >>> cat = Catalog()
            >>> cat.resolve_scientific_name("blue-whale")
            'Balaenoptera musculus'
            >>> cat.resolve_scientific_name("species:Mola mola")
            'Mola mola'

            ```
        - An unknown friendly key raises with a did-you-mean hint:
            ```python
            >>> from earthlens.obis import Catalog
            >>> Catalog().resolve_scientific_name("blue-whal")
            Traceback (most recent call last):
                ...
            ValueError: 'blue-whal' is not in the OBIS species catalog. Known species: ['blue-whale', 'common-dolphin', 'great-white-shark', 'loggerhead-turtle', 'ocean-sunfish']. Did you mean 'blue-whale'?

            ```
    """

    _catalog_kind: str = "OBIS species catalog"
    _entry_noun: str = "species"

    datasets: dict[str, Species] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `available_datasets` read from
                the bundled catalog.
        """
        rows, available = _load_catalog_data(CATALOG_PATH)
        return {
            "datasets": dict(rows),
            "available_datasets": list(available),
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the OBIS species catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `species:` block, or a row
                fails :class:`Species` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        rows, available = _load_catalog_data(catalog_path)
        return cls(datasets=dict(rows), available_datasets=list(available))

    def resolve_scientific_name(self, selector: str) -> str:
        """Resolve a `variables` selector to an OBIS `scientificname`.

        Accepts an explicit `species:<scientific name>` selector (passed
        through verbatim) or a friendly catalog key (`"blue-whale"`).

        Args:
            selector: One `variables` entry — a `"species:<name>"` string
                or a friendly species name.

        Returns:
            The resolved OBIS `scientificname`.

        Raises:
            ValueError: If a friendly key is unknown (with a did-you-mean
                hint).
        """
        text = selector.strip()
        if text.lower().startswith(SPECIES_PREFIX):
            return text[len(SPECIES_PREFIX) :].strip()
        return cast("Species", self.get_dataset(text)).scientific_name
