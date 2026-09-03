"""Source catalog for the NSI flood-exposure backend.

The `nsi` backend serves three keyless US-federal REST sources selected by a
`source=` discriminator: `structures` (USACE National Structure Inventory),
`nfhl` (FEMA National Flood Hazard Layer), and `nfip` (FEMA NFIP redacted
claims v3). This module is the bridge from a `source` key to its endpoint, the
per-instance output kind, and the friendly -> provider field map used to shape
the response.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `nsi_data_catalog.yaml` and exposes each row as a
:class:`Source`. Resolve one source with :meth:`Catalog.get` (a did-you-mean
hint on an unknown key); list the shipped keys with :meth:`Catalog.available`.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog, OutputKind
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "nsi_data_catalog.yaml"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus the
#: YAML's `st_mtime_ns`, so editing the file invalidates the entry without
#: re-parsing on every `Catalog()`. Mirrors the risk_indicators loader.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Source]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes the
    file's `st_mtime_ns`, so any real edit invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Source]:
    """Parse, validate, and cache the catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        The source map keyed by `source` name.

    Raises:
        ValueError: If the file has no `sources:` block, or a row fails
            :class:`Source` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    sources_yaml = data.get("sources") or {}
    if not sources_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'sources:' block. "
            "The NSI catalog must list at least one source."
        )
    rows: dict[str, Source] = {}
    for source_id, body in sources_yaml.items():
        try:
            rows[source_id] = Source(id=source_id, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} source {source_id!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = rows
    return rows


class Source(BaseModel):
    """One NSI catalog row (one of the three flood sources).

    The source name is the parent key in :attr:`Catalog.datasets` and is stored
    on the row as :attr:`id` so a resolved :class:`Source` is self-describing.
    Which fields matter depends on :attr:`provider`; a cross-field validator
    enforces the source-specific ones (`nfhl` needs a layer, `nfip` a records
    key).

    Attributes:
        id: The source name (`"structures"`, `"nfhl"`, `"nfip"`).
        provider: The upstream service — `"nsi"`, `"fema-arcgis"`, or
            `"openfema"`.
        endpoint: The base REST URL.
        output_kind: `"vector"` (a `FeatureCollection`) or `"tabular"` (a
            `DataFrame`). Copied onto the backend's `OUTPUT_KIND` per instance.
        long_name: Human-readable label.
        citation: The source's citation string, logged once on use.
        license: The data licence (all three are US public domain).
        fields: Friendly -> provider field-name map. For the tabular `nfip`
            source this **shapes the output** — the `DataFrame` is subset to
            these columns, renamed friendly. For the vector `structures` / `nfhl`
            sources it is **informational only**: those return the full provider
            column set unchanged (renaming would drop the many other NSI/ArcGIS
            fields), and this map documents the notable ones.
        layer_id: ArcGIS MapServer layer id (`nfhl` only — `S_Fld_Haz_Ar` = 28).
        layer_name: ArcGIS layer name (`nfhl` only).
        records_key: JSON envelope key holding the record list (`nfip` only —
            `NfipClaims`).
        page_size: OData page size for paged fetches (`nfip` only).

    Examples:
        - Build a source row directly:
            ```python
            >>> from earthlens.nsi import Source
            >>> row = Source(
            ...     id="nfip",
            ...     provider="openfema",
            ...     endpoint="https://www.fema.gov/api/open/v3/NfipClaims",
            ...     output_kind="tabular",
            ...     records_key="NfipClaims",
            ... )
            >>> row.output_kind
            'tabular'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    provider: str
    endpoint: str
    output_kind: OutputKind
    long_name: str = ""
    citation: str = ""
    license: str = ""
    fields: dict[str, str] = Field(default_factory=dict)

    # nfhl
    layer_id: int | None = None
    layer_name: str | None = None
    # nfip
    records_key: str | None = None
    page_size: int | None = None

    @model_validator(mode="after")
    def _check_provider_fields(self) -> Source:
        """Enforce the per-source required fields.

        Returns:
            The validated row.

        Raises:
            ValueError: If an `nfhl` row omits `layer_id`/`layer_name`, or an
                `nfip` row omits `records_key`, or `output_kind` disagrees with
                the source family.
        """
        if self.provider == "fema-arcgis":
            if self.layer_id is None or not self.layer_name:
                raise ValueError(
                    f"nfhl source {self.id!r} needs layer_id and layer_name"
                )
            if self.output_kind != "vector":
                raise ValueError(
                    f"nfhl source {self.id!r} must be output_kind 'vector'"
                )
        if self.provider == "openfema":
            if not self.records_key:
                raise ValueError(f"nfip source {self.id!r} needs records_key")
            if self.output_kind != "tabular":
                raise ValueError(
                    f"nfip source {self.id!r} must be output_kind 'tabular'"
                )
        if self.provider == "nsi" and self.output_kind != "vector":
            raise ValueError(
                f"structures source {self.id!r} must be output_kind 'vector'"
            )
        return self


class Catalog(AbstractCatalog):
    """Source catalog for the NSI backend.

    Reads the bundled `nsi_data_catalog.yaml` (shipped as package data) and
    exposes its `sources:` block as a map of :class:`Source` rows keyed by
    name. Instantiate with no arguments (`Catalog()`). Resolve one row with
    :meth:`get` and list the shipped keys with :meth:`available`.

    Attributes:
        datasets: Map from source name to its :class:`Source` row.

    Examples:
        - Resolve a source and read its output kind:
            ```python
            >>> from earthlens.nsi import Catalog
            >>> cat = Catalog()
            >>> cat.get("structures").output_kind
            'vector'
            >>> cat.get("nfip").output_kind
            'tabular'
            >>> cat.available()
            ['nfhl', 'nfip', 'structures']

            ```
    """

    _catalog_kind: str = "NSI catalog"
    _entry_noun: str = "sources"

    #: The source rows live in the base :attr:`datasets` field so the inherited
    #: dict surface and :meth:`get_dataset`'s did-you-mean hint work unchanged.
    datasets: dict[str, Source] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` map read from the bundled catalog.
        """
        return {"datasets": Catalog.load().datasets}

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the NSI catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `sources:` block, or a row fails
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(datasets=dict(_load_catalog_data(catalog_path)))

    def get(self, source_id: str) -> Source:
        """Resolve a source name to its :class:`Source` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown key.

        Args:
            source_id: A shipped source name (`"structures"`, `"nfhl"`,
                `"nfip"`).

        Returns:
            Source: The matching catalog row.

        Raises:
            ValueError: If `source_id` is not a known source; the message names
                the catalog kind and, when a close match exists, adds a
                did-you-mean hint.
        """
        return cast("Source", self.get_dataset(source_id))

    def available(self) -> list[str]:
        """Return the sorted list of shipped source names.

        Returns:
            list[str]: Every catalog key, sorted.
        """
        return sorted(self.datasets)
