"""Country dispatch table for the IUCN Red List backend.

The Red List v4 `countries/{code}` endpoint takes an ISO 3166-1 alpha-2
code, so this "catalog" is a curated map from an alpha-2 code to a country
name + region, plus a `resolve_iso2` helper that also accepts a friendly
country name or a `country:<ISO2>` selector. The catalog is a convenience
and the `validate` target — the backend accepts **any** valid alpha-2 code
whether or not it is listed here, and species are selected by binomial
(`species:<name>`), not from this catalog.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass
storing the rows in the framework's :attr:`~earthlens.base.AbstractCatalog.datasets`
field, so the inherited dict-like surface works. Parsing is cached on
`(path, mtime)`; :data:`CATALOG_PATH` is the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "iucn_data_catalog.yaml"

#: Prefix marking an explicit country selector in `variables`.
COUNTRY_PREFIX = "country:"

# Module-level cache of parsed catalog rows, keyed on the resolved path plus
# the YAML's `st_mtime_ns`. Mirrors the FDSN / GBIF / WDPA catalog loaders.
_CATALOG_CACHE: dict[tuple[str, int], tuple[dict[str, Country], list[str]]] = (
    CatalogParseCache()
)


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    the file's `st_mtime_ns`, so any real edit invalidates the entry.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> tuple[dict[str, Country], list[str]]:
    """Parse, validate, and cache the IUCN country catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        A pair `(rows, available)` of the ISO2-keyed `Country` map and the
        informational `available_datasets:` mirror of the curated codes.

    Raises:
        ValueError: If the file has no `countries:` block, or a row fails
            :class:`Country` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    countries_yaml = data.get("countries") or {}
    if not countries_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'countries:' block. "
            "The IUCN catalog must list at least one country."
        )
    rows: dict[str, Country] = {}
    for code, body in countries_yaml.items():
        try:
            rows[code] = Country(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} country {code!r} failed validation:\n{exc}"
            ) from exc

    available = [str(item) for item in (data.get("available_datasets") or [])]
    _CATALOG_CACHE[key] = (rows, available)
    return rows, available


class Country(BaseModel):
    """One curated IUCN country's dispatch row.

    The ISO2 code is the parent key in :attr:`Catalog.datasets` and is not
    stored on the row.

    Attributes:
        name: Human-readable country name (e.g. `"Kenya"`).
        region: Continent / region grouping (e.g. `"Africa"`).

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.iucn import Country
            >>> c = Country(name="Kenya", region="Africa")
            >>> c.name
            'Kenya'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    region: str = ""


class Catalog(AbstractCatalog):
    """Country catalog for the IUCN Red List backend.

    Reads the bundled `iucn_data_catalog.yaml` (shipped as package data) and
    exposes its `countries:` block as a map of :class:`Country` rows.
    Instantiate with no arguments (`Catalog()`); :func:`model_post_init`
    loads and validates the YAML (cached) in one pass.

    Attributes:
        datasets: Map from the ISO2 code to its :class:`Country` row.

    Examples:
        - Resolve a code, a friendly name, and a `country:` selector:
            ```python
            >>> from earthlens.iucn import Catalog
            >>> cat = Catalog()
            >>> cat.resolve_iso2("KE")
            'KE'
            >>> cat.resolve_iso2("kenya")
            'KE'
            >>> cat.resolve_iso2("country:BR")
            'BR'

            ```
        - An uncatalogued but well-formed alpha-2 code passes through:
            ```python
            >>> from earthlens.iucn import Catalog
            >>> Catalog().resolve_iso2("ZW")
            'ZW'

            ```
    """

    _catalog_kind: str = "IUCN country catalog"
    _entry_noun: str = "countries"

    datasets: dict[str, Country] = Field(default_factory=dict)

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
        """Read the IUCN country catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `countries:` block, or a row
                fails :class:`Country` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        rows, available = _load_catalog_data(catalog_path)
        return cls(datasets=dict(rows), available_datasets=list(available))

    def resolve_iso2(self, selector: str) -> str:
        """Resolve a country `variables` selector to an ISO2 country code.

        Accepts a `country:<ISO2>` selector, a bare alpha-2 code (passed
        through uppercased, whether or not it is catalogued), or a friendly
        country name (e.g. `"kenya"`).

        Args:
            selector: A `"country:<ISO2>"`, bare alpha-2 code, or country
                name (the `country:` prefix is optional).

        Returns:
            The ISO2 country code.

        Raises:
            ValueError: If a non-code name is not a catalogued country
                (with a did-you-mean hint).
        """
        import difflib

        text = selector.strip()
        if text.lower().startswith(COUNTRY_PREFIX):
            text = text[len(COUNTRY_PREFIX) :].strip()
        if len(text) == 2 and text.isalpha():
            return text.upper()
        for code, country in self.datasets.items():
            if country.name.lower() == text.lower():
                return code
        names = [country.name for country in self.datasets.values()]
        close = difflib.get_close_matches(text, names, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{selector!r} is not a known IUCN country name or alpha-2 code.{hint}"
        )
