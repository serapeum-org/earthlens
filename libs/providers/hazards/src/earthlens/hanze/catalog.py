"""Catalog for the HANZE historical-flood-impacts backend.

HANZE is a single tabular product — the database of observed European flood
events and their impacts (Paprotny et al.) — published as individual small files
on a pinned Zenodo *version* record. This module is the bridge between the
friendly request vocabulary (`type="River"`, `country="DE"`) and what the
release actually ships: the pinned record, the per-file names, the flood-type
vocabulary, the friendly-name -> CSV-header map, and the region-geometry join
configuration.

Four shapes are modelled, all frozen:

* :class:`ZenodoRecord` — the pinned version record, its concept DOI, `version`,
  `data_period`, licence and attribution. Pinning a version rather than the
  moving concept DOI is what makes a request reproducible.
* :class:`HanzeFile` — one downloadable Zenodo object (its name, and the REST
  content `url` composed from the pinned record). HANZE ships small individual
  files, so each is a direct download, never a range-read.
* :class:`FloodType` — one row of the `Type` vocabulary (`River`, `Flash`,
  `Coastal`, `River/Coastal`). These are the catalog's dict-surface rows, keyed
  by type under the inherited :attr:`datasets` field.
* :class:`GeometryJoin` — the region-shapefile join: its member stem, the join
  field (`Code`), the name field, and the shapefile CRS (`EPSG:3035`).

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `hanze_data_catalog.yaml` through the shared
:func:`~earthlens.base.catalog_source.load_catalog` (with a `CatalogParseCache`),
mirroring `gdacs/catalog.py`. :data:`CATALOG_PATH` is the path to the bundled
YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "hanze_data_catalog.yaml"

#: Zenodo REST content URL for one file of a pinned record.
_CONTENT_URL = "https://zenodo.org/api/records/{record}/files/{name}/content"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path plus
#: the YAML's `(mtime_ns, size)`, so a repeated `Catalog()` skips the parse.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class ZenodoRecord(BaseModel):
    """The pinned Zenodo version record HANZE is fetched from.

    Attributes:
        record: The pinned Zenodo *version* record id (`20478847`). Every file
            URL is composed from it, so a request is reproducible.
        concept_doi: The moving concept DOI. Recorded so a refresh check can
            discover a newer version; never used to fetch.
        version: The dataset version (`v3.0.1-beta`). Flagged as beta in the
            docs and logs.
        data_period: The `first-last` year span the record covers
            (`"1870-2025"`), for documentation and the drift check.
        license: SPDX-ish licence id (`CC-BY-4.0`).
        attribution: The citation obligation the licence carries.

    Examples:
        - The record is the pinned version, not the concept DOI:
            ```python
            >>> from earthlens.hanze import Catalog
            >>> Catalog().record.record
            20478847
            >>> Catalog().record.version
            'v3.0.1-beta'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: int
    concept_doi: str = ""
    version: str = ""
    data_period: str = ""
    license: str = ""
    attribution: str = ""


class HanzeFile(BaseModel):
    """One downloadable Zenodo object of the pinned HANZE record.

    Attributes:
        name: The file name on the record.
        description: One-line human-readable summary.

    Examples:
        - The content URL is composed from the pinned record and file name:
            ```python
            >>> from earthlens.hanze import Catalog
            >>> events = Catalog().file("events")
            >>> events.content_url(20478847)
            'https://zenodo.org/api/records/20478847/files/HANZE_events_v3_0_1b.csv/content'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""

    def content_url(self, record: int) -> str:
        """Return the Zenodo REST content URL this file is served from.

        Args:
            record: The pinned version record id the file belongs to.

        Returns:
            str: `https://zenodo.org/api/records/<record>/files/<name>/content`.

        Examples:
            - Compose the REST content URL for a file on a record:
                ```python
                >>> from earthlens.hanze import HanzeFile
                >>> HanzeFile(name="events.csv").content_url(20478847)
                'https://zenodo.org/api/records/20478847/files/events.csv/content'

                ```
        """
        return _CONTENT_URL.format(record=record, name=self.name)


class FloodType(BaseModel):
    """One entry of the HANZE flood-`Type` vocabulary.

    The type string (`"River"`, `"River/Coastal"`) is the parent key in
    :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        description: Short note on what the flood type covers.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.hanze import FloodType
            >>> FloodType(description="Riverine (fluvial) floods.").description
            'Riverine (fluvial) floods.'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = ""


class GeometryJoin(BaseModel):
    """The region-shapefile join configuration for `with_geometry`.

    Attributes:
        member_stem: The shapefile member stem inside the region zip
            (`"NUTS3_regions_v2024_simplified"`); the `.shp` and its sidecars
            share it.
        join_field: The shapefile attribute holding the NUTS-3 code (`"Code"`),
            joined to the semicolon-split `Regions affected (NUTS 3)` list.
        name_field: The shapefile attribute holding the region name (`"Name"`).
        crs: The shapefile's stored CRS (`"EPSG:3035"`, ETRS89-LAEA Europe). The
            backend reprojects to WGS84 for a degree bbox filter and for parity
            with the other vector backends.

    Examples:
        - The join field and CRS are what the geometry attach reads:
            ```python
            >>> from earthlens.hanze import Catalog
            >>> geometry = Catalog().geometry
            >>> geometry.join_field, geometry.crs
            ('Code', 'EPSG:3035')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_stem: str
    join_field: str = "Code"
    name_field: str = "Name"
    crs: str = "EPSG:3035"


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the HANZE catalog YAML into the :class:`Catalog` field payload.

    Args:
        files: The contributing YAML files (HANZE ships a single file).

    Returns:
        dict[str, Any]: The keyword payload for :class:`Catalog` — `record`,
            `files`, `geometry`, `datasets` (the flood types) and `columns`.

    Raises:
        ValueError: If a required block is missing or empty, or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}

    record_yaml = data.get("record") or {}
    if not record_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'record:' block. The "
            "HANZE catalog must pin a Zenodo version record."
        )
    files_yaml = data.get("files") or {}
    flood_types_yaml = data.get("flood_types") or {}
    if not flood_types_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'flood_types:' block. "
            "The HANZE catalog must list at least one flood type."
        )
    geometry_yaml = data.get("geometry") or {}
    columns_yaml = data.get("columns") or {}

    try:
        record = ZenodoRecord(**dict(record_yaml))
        file_map = {
            name: HanzeFile(**dict(body or {})) for name, body in files_yaml.items()
        }
        flood_types = {
            name: FloodType(**dict(body or {}))
            for name, body in flood_types_yaml.items()
        }
        geometry = GeometryJoin(**dict(geometry_yaml))
    except ValidationError as exc:
        raise ValueError(f"{catalog_path} failed validation:\n{exc}") from exc

    # Only the two files the backend actually fetches are required. The
    # `region_names` (S2) lookup is documented and shipped but never downloaded —
    # region names come from the shapefile `Name` attribute — so requiring it
    # would make a maintainer who drops the unused entry break `Catalog()`.
    for required in ("events", "regions"):
        if required not in file_map:
            raise ValueError(
                f"{catalog_path} 'files:' block is missing required file "
                f"{required!r}. Known: {sorted(file_map)}."
            )

    return {
        "record": record,
        "files": file_map,
        "geometry": geometry,
        "datasets": flood_types,
        "columns": {str(key): str(value) for key, value in columns_yaml.items()},
        "available_datasets": sorted(flood_types),
    }


class Catalog(AbstractCatalog):
    """Catalog for the HANZE backend.

    Reads the bundled `hanze_data_catalog.yaml` (shipped as package data) and
    exposes the pinned Zenodo record, the per-file names, the flood-`Type`
    vocabulary (as :class:`FloodType` rows keyed by type under the inherited
    :attr:`datasets` field — the `cat["River"]` / `"River" in cat` / `len(cat)`
    dict surface), the friendly-name -> CSV-header map, and the region-geometry
    join configuration. Instantiate with no arguments (`Catalog()`).

    Attributes:
        datasets: Map from a flood-`Type` string to its :class:`FloodType` row.
        record: The pinned :class:`ZenodoRecord`.
        files: Map from a logical key (`"events"`, `"regions"`,
            `"region_names"`) to its :class:`HanzeFile`.
        geometry: The :class:`GeometryJoin` for the region attach.
        columns: Friendly name -> exact HANZE CSV header.

    Examples:
        - List the flood types and resolve one, and read the pinned record:
            ```python
            >>> from earthlens.hanze import Catalog
            >>> cat = Catalog()
            >>> cat.flood_types()
            ['Coastal', 'Flash', 'River', 'River/Coastal']
            >>> cat.get_flood_type("River").description
            'Riverine (fluvial) floods.'
            >>> "Coastal" in cat
            True
            >>> cat.column("country_code")
            'Country code'

            ```
        - An unknown flood type raises with a did-you-mean hint:
            ```python
            >>> from earthlens.hanze import Catalog
            >>> Catalog().get_flood_type("Rivers")
            Traceback (most recent call last):
                ...
            ValueError: 'Rivers' is not in the HANZE catalog. Known flood types: ['Coastal', 'Flash', 'River', 'River/Coastal']. Did you mean 'River'?

            ```
    """

    _catalog_kind: str = "HANZE catalog"
    _entry_noun: str = "flood types"

    datasets: dict[str, FloodType] = Field(default_factory=dict)
    #: `record` / `geometry` default to `None` rather than a placeholder model:
    #: the base :meth:`model_post_init` autoload fills a field only when its
    #: current value is falsy, and a placeholder model instance is truthy, so it
    #: would be skipped and `Catalog()` would keep the placeholder. `None` is
    #: falsy, so the bundled record / geometry load as intended. `load()` and a
    #: test passing `datasets=` + these fields populate them directly.
    record: ZenodoRecord | None = Field(default=None, repr=False)
    files: dict[str, HanzeFile] = Field(default_factory=dict, repr=False)
    geometry: GeometryJoin | None = Field(default=None, repr=False)
    columns: dict[str, str] = Field(default_factory=dict, repr=False)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The full field payload read from the bundled catalog.
        """
        return dict(_parse_catalog([CATALOG_PATH]))

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the HANZE catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, a required block is
                missing, or a row fails validation.

        Examples:
            - Loading the bundled catalog yields the pinned record and types:
                ```python
                >>> from earthlens.hanze import Catalog
                >>> cat = Catalog.load()
                >>> cat.record.record
                20478847
                >>> cat.flood_types()
                ['Coastal', 'Flash', 'River', 'River/Coastal']

                ```
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        parsed = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_catalog, provider="HANZE"
        )
        return cls(**parsed)

    def get_flood_type(self, flood_type: str) -> FloodType:
        """Return the :class:`FloodType` for `flood_type`, with a did-you-mean hint.

        Thin alias over :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            flood_type: A HANZE flood-`Type` string (`"River"`, `"Coastal"`,
                `"Flash"`, `"River/Coastal"`).

        Returns:
            FloodType: The matching row.

        Raises:
            ValueError: If `flood_type` is not a registered flood type.

        Examples:
            - Resolve a type and read its description:
                ```python
                >>> from earthlens.hanze import Catalog
                >>> Catalog().get_flood_type("Coastal").description
                'Coastal (storm-surge) floods.'

                ```
        """
        return cast("FloodType", self.get_dataset(flood_type))

    def flood_types(self) -> list[str]:
        """Return the registered flood-`Type` strings, sorted.

        Returns:
            list[str]: The flood types
                (`["Coastal", "Flash", "River", "River/Coastal"]`).

        Examples:
            - The registered types come back sorted:
                ```python
                >>> from earthlens.hanze import Catalog
                >>> Catalog().flood_types()
                ['Coastal', 'Flash', 'River', 'River/Coastal']

                ```
        """
        return sorted(self.datasets)

    def file(self, key: str) -> HanzeFile:
        """Return the :class:`HanzeFile` for a logical key.

        Args:
            key: `"events"`, `"regions"`, or `"region_names"`.

        Returns:
            HanzeFile: The matching file descriptor.

        Raises:
            KeyError: If `key` is not a known file.

        Examples:
            - Resolve the events and region file names:
                ```python
                >>> from earthlens.hanze import Catalog
                >>> cat = Catalog()
                >>> cat.file("events").name
                'HANZE_events_v3_0_1b.csv'
                >>> cat.file("regions").name
                'Regions_v2024_simplified.zip'

                ```
        """
        return self.files[key]

    def column(self, friendly: str) -> str:
        """Return the exact HANZE CSV header for a friendly column name.

        Args:
            friendly: A friendly key from the catalog's `columns:` map
                (`"country_code"`, `"type"`, `"regions_nuts3"`, ...).

        Returns:
            str: The exact CSV header (`"Country code"`).

        Raises:
            KeyError: If `friendly` is not a mapped column.

        Examples:
            - Map friendly keys to their exact HANZE headers:
                ```python
                >>> from earthlens.hanze import Catalog
                >>> cat = Catalog()
                >>> cat.column("country_code")
                'Country code'
                >>> cat.column("regions_nuts3")
                'Regions affected (NUTS 3)'

                ```
        """
        return self.columns[friendly]
