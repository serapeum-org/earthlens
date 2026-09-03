"""Index catalogue for the climate-indices backend.

The climate-indices backend fetches small monthly teleconnection-index
ASCII files from two open sources (NOAA PSL, KNMI Climate Explorer). This
module is the bridge from a short index id (`"oni"`, `"nao"`, …) to the
URL + ASCII dialect + light metadata needed to fetch and parse it.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `climate_indices_data_catalog.yaml` and
exposes each row as an :class:`Index`. The YAML keeps a DRY `sources:`
block (base URL + citation per source) and a `datasets:` block (one row
per index); the loader joins `base_url + file` into each row's
:attr:`Index.url` and attaches the source citation, so a resolved
:class:`Index` is self-describing. Resolve one with :meth:`Catalog.get`
(a did-you-mean hint on an unknown id); list the shipped ids with
:meth:`Catalog.available`.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "climate_indices_data_catalog.yaml"

#: Module-level cache of parsed catalog rows, keyed on the resolved path
#: plus the YAML's `st_mtime_ns`, so editing the file invalidates the
#: entry without re-parsing on every `Catalog()`. Mirrors the
#: `_CATALOG_CACHE` pattern in the usgs_water / gee / ecmwf loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Index]] = CatalogParseCache()

#: The two index sources, used to validate each row's `source`.
Source = Literal["noaa-psl", "knmi-climexp"]

#: The two ASCII dialects, used to pick the parser for each row.
Dialect = Literal["psl", "climexp"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key
    includes the file's `st_mtime_ns`, so any real edit invalidates the
    entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Index]:
    """Parse, validate, join, and cache the index catalogue at `path`.

    Reads the `sources:` and `datasets:` blocks, joins each row's
    `base_url + file` into its `url`, attaches the source citation, and
    validates the merged row as an :class:`Index`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        Mapping from index id to its :class:`Index` row.

    Raises:
        ValueError: If the file has no `datasets:` block, a row names an
            unknown `source`, or a row fails :class:`Index` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    sources = data.get("sources") or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The climate-indices catalog must list at least one index."
        )
    rows: dict[str, Index] = {}
    for index_id, body in datasets_yaml.items():
        row = dict(body or {})
        source = row.get("source")
        if source not in sources:
            raise ValueError(
                f"{path} index {index_id!r} names unknown source {source!r}; "
                f"known sources: {sorted(sources)}."
            )
        source_meta = sources[source] or {}
        base_url = str(source_meta.get("base_url", "")).rstrip("/")
        file_name = row.pop("file", "")
        row["url"] = f"{base_url}/{file_name}" if file_name else ""
        row["citation"] = source_meta.get("citation", "")
        try:
            rows[index_id] = Index(**row)
        except ValidationError as exc:
            raise ValueError(
                f"{path} index {index_id!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = rows
    return rows


class Index(BaseModel):
    """One climate-index catalogue row.

    The index id is the parent key in :attr:`Catalog.datasets`; the row
    carries everything the backend needs to fetch and parse the series.

    Attributes:
        source: Which open source serves the index — `"noaa-psl"` or
            `"knmi-climexp"`.
        dialect: The ASCII parser to use — `"psl"` or `"climexp"`.
        url: The full HTTPS URL of the index file (the YAML `base_url`
            joined with the per-row `file`).
        long_name: Human-readable label.
        units: Reporting units (`"degC"`, `"std"`).
        citation: The source's citation string, logged once on use.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.climate_indices import Index
            >>> row = Index(
            ...     source="noaa-psl",
            ...     dialect="psl",
            ...     url="https://psl.noaa.gov/data/correlation/oni.data",
            ...     long_name="Oceanic Niño Index",
            ...     units="degC",
            ...     citation="NOAA PSL",
            ... )
            >>> row.dialect
            'psl'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    dialect: Dialect
    url: str
    long_name: str = ""
    units: str = ""
    citation: str = ""


class Catalog(AbstractCatalog):
    """Index catalogue for the climate-indices backend.

    Reads the bundled `climate_indices_data_catalog.yaml` (shipped as
    package data) and exposes its `datasets:` block as a map of
    :class:`Index` rows keyed by index id. Instantiate with no arguments
    (`Catalog()`). Resolve one row with :meth:`get`, or list the shipped
    ids with :meth:`available`.

    Attributes:
        datasets: Map from index id to its :class:`Index` row.

    Examples:
        - Resolve a row and read its dialect:
            ```python
            >>> from earthlens.climate_indices import Catalog
            >>> cat = Catalog()
            >>> cat.get("oni").dialect in {"psl", "climexp"}
            True
            >>> "amo" in cat.available()
            True

            ```
        - An unknown but close id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.climate_indices import Catalog
            >>> Catalog().get("noo")
            Traceback (most recent call last):
                ...
            ValueError: 'noo' is not in the climate-indices catalog. Known indices: [...]. Did you mean 'nao'?

            ```
    """

    _catalog_kind: str = "climate-indices catalog"
    _entry_noun: str = "indices"

    #: The index rows live in the base :attr:`datasets` field so the
    #: inherited dict surface (`len`, `in`, `[]`, iteration) and
    #: :meth:`get_dataset`'s did-you-mean hint work unchanged.
    datasets: dict[str, Index] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` read from
                the bundled catalog.
        """
        return {"datasets": Catalog.load().datasets}

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the climate-indices catalogue from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block, or a row
                fails validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(datasets=dict(_load_catalog_data(catalog_path)))

    def get(self, index_id: str) -> Index:
        """Resolve an index id to its :class:`Index` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises
        a `ValueError` with a did-you-mean hint on an unknown id.

        Args:
            index_id: A shipped index id (`"oni"`, `"nao"`, …).

        Returns:
            Index: The matching catalogue row.

        Raises:
            ValueError: If `index_id` is not a known index; the message
                names the catalog kind and, when a close match exists,
                adds a did-you-mean hint.
        """
        return cast("Index", self.get_dataset(index_id))

    def available(self) -> list[str]:
        """Return the sorted list of shipped index ids.

        Returns:
            list[str]: Every catalogue key, sorted.
        """
        return sorted(self.datasets)
