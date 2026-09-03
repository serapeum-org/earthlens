"""Taxon dispatch table for the GBIF occurrence backend.

GBIF occurrence search keys on a numeric backbone `taxonKey`, so this
"catalog" is a small curated map from a friendly name (`"birds"`,
`"mammals"`, `"plants"`, …) to its `taxonKey`, plus a `resolve_taxon_key`
helper that also accepts a raw integer key or a live `taxon:<scientific
name>` lookup. There is no `refresh` / `probe` tooling and no
`tools/gbif/` directory — the curated rows are a hand-edit of one YAML
file (`earthlens datasets validate gbif` confirms the keys upstream).

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass storing the rows in the framework's
:attr:`~earthlens.base.AbstractCatalog.datasets` field, so the inherited
dict-like surface (`len(cat)`, `name in cat`, `cat[name]`, `iter(cat)`,
:meth:`get_dataset` with a did-you-mean hint) works. Parsing is cached on
`(path, mtime)` (see :data:`_CATALOG_CACHE` / :func:`clear_catalog_cache`);
:data:`CATALOG_PATH` is the bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "gbif_data_catalog.yaml"

#: Prefix marking a live scientific-name lookup in a `variables` selector.
TAXON_PREFIX = "taxon:"

# Module-level cache of parsed catalog rows, keyed on the resolved path plus
# the YAML's `st_mtime_ns`, so editing the file invalidates the entry without
# re-parsing on every `Catalog()`. Mirrors the FDSN / OpenAQ catalog loaders.
_CATALOG_CACHE: dict[tuple[str, int], tuple[dict[str, Taxon], list[str]]] = (
    CatalogParseCache()
)


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    the file's `st_mtime_ns`, so any real edit invalidates the entry.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> tuple[dict[str, Taxon], list[str]]:
    """Parse, validate, and cache the GBIF taxon catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        A pair `(rows, available)` of the friendly-name `Taxon` map and the
        informational `available_datasets:` index (the richer taxonomic
        listing the refresher diffs against).

    Raises:
        ValueError: If the file has no `taxa:` block, or a row fails
            :class:`Taxon` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    taxa_yaml = data.get("taxa") or {}
    if not taxa_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'taxa:' block. "
            "The GBIF catalog must list at least one taxon."
        )
    rows: dict[str, Taxon] = {}
    for name, body in taxa_yaml.items():
        try:
            rows[name] = Taxon(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} taxon {name!r} failed validation:\n{exc}"
            ) from exc

    available = [str(item) for item in (data.get("available_datasets") or [])]
    _CATALOG_CACHE[key] = (rows, available)
    return rows, available


def _name_backbone_key(name: str) -> int:
    """Resolve a scientific name to a GBIF backbone `taxonKey` (live).

    Imports `pygbif` lazily so the package imports without the `[gbif]`
    extra. Reads the 0.6.6 nested shape (`result["usage"]["key"]`) and
    falls back to the legacy flat `result["usageKey"]`.

    Args:
        name: A scientific name (e.g. `"Panthera leo"`).

    Returns:
        The matched backbone `taxonKey`.

    Raises:
        ValueError: If GBIF returns no backbone match for `name`.
    """
    from pygbif import species

    # pygbif 0.6.6's first parameter is `scientificName` (positional here); an
    # unknown `name=` kwarg would be forwarded to requests and raise TypeError.
    result = species.name_backbone(name) or {}
    usage = result.get("usage") or {}
    key = usage.get("key", result.get("usageKey"))
    if key is None:
        raise ValueError(
            f"GBIF found no backbone taxon for {name!r}. Check the spelling, "
            "or pass a raw integer taxonKey instead."
        )
    return int(key)


class Taxon(BaseModel):
    """One friendly GBIF taxon's dispatch row.

    The user-facing name is the parent key in :attr:`Catalog.datasets`
    and is not stored on the row.

    Attributes:
        taxon_key: GBIF backbone `taxonKey` the occurrence search filters
            on (e.g. `212` for Aves).
        title: Human-readable description used in logs and docs.
        rank: Taxonomic rank of the key (`"kingdom"`, `"class"`, …),
            informational.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.gbif import Taxon
            >>> t = Taxon(taxon_key=212, title="Aves — birds", rank="class")
            >>> t.taxon_key, t.rank
            (212, 'class')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    taxon_key: int
    title: str = ""
    rank: str = ""


class Catalog(AbstractCatalog):
    """Taxon catalog for the GBIF occurrence backend.

    Reads the bundled `gbif_data_catalog.yaml` (shipped as package data)
    and exposes its `taxa:` block as a map of :class:`Taxon` rows.
    Instantiate with no arguments (`Catalog()`); :func:`model_post_init`
    loads and validates the YAML (cached) in one pass. The rows live in
    the framework's :attr:`datasets` field so the inherited dict-like
    surface behaves like every other backend's catalog.

    Attributes:
        datasets: Map from the friendly taxon name to its :class:`Taxon`
            row (the framework field).

    Examples:
        - The dict-like surface works like the other backends:
            ```python
            >>> from earthlens.gbif import Catalog
            >>> cat = Catalog()
            >>> "birds" in cat
            True
            >>> cat["birds"].taxon_key
            212

            ```
        - Resolve a friendly key, a raw integer, and a name lookup:
            ```python
            >>> from earthlens.gbif import Catalog
            >>> cat = Catalog()
            >>> cat.resolve_taxon_key("mammals")
            359
            >>> cat.resolve_taxon_key(212)
            212

            ```
        - An unknown friendly key raises with a did-you-mean hint:
            ```python
            >>> from earthlens.gbif import Catalog
            >>> Catalog().resolve_taxon_key("bird")
            Traceback (most recent call last):
                ...
            ValueError: 'bird' is not in the GBIF taxon catalog. Known taxa: ['animals', 'birds', 'fungi', 'mammals', 'plants']. Did you mean 'birds'?

            ```
    """

    _catalog_kind: str = "GBIF taxon catalog"
    _entry_noun: str = "taxa"

    datasets: dict[str, Taxon] = Field(default_factory=dict)

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
        """Read the GBIF taxon catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `taxa:` block, or a row fails
                :class:`Taxon` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        rows, available = _load_catalog_data(catalog_path)
        return cls(datasets=dict(rows), available_datasets=list(available))

    def resolve_taxon_key(self, selector: str | int) -> int:
        """Resolve a `variables` selector to a GBIF backbone `taxonKey`.

        Accepts a raw integer (or digit string) key, a `taxon:<scientific
        name>` live lookup, or a friendly catalog key (`"birds"`).

        Args:
            selector: One `variables` entry — an `int`, a digit string, a
                `"taxon:<name>"` string, or a friendly taxon name.

        Returns:
            The resolved backbone `taxonKey`.

        Raises:
            ValueError: If a friendly key is unknown (with a did-you-mean
                hint) or a name lookup finds no backbone match.
        """
        if isinstance(selector, int):
            return selector
        text = selector.strip()
        if text.isdigit():
            return int(text)
        if text.lower().startswith(TAXON_PREFIX):
            return _name_backbone_key(text[len(TAXON_PREFIX) :].strip())
        return cast("int", self.get_dataset(text).taxon_key)
