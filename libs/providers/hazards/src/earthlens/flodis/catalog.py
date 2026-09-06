"""Catalog for the FLODIS observed-flood impacts backend.

FLODIS ships two small tabular products — `damages` (EM-DAT fatalities and
economic damages matched to Global Flood Database footprints) and `displacement`
(IDMC displacements matched to the same) — published on a pinned Zenodo record.
This module is the bridge between the friendly request vocabulary
(`dataset="damages"`, `country="MOZ"`) and what the release actually ships: the
pinned record, the per-dataset file names and join keys, and the friendly-name
-> CSV-header map.

Three shapes are modelled, all frozen:

* :class:`ZenodoRecord` — the pinned record, its DOI, `data_period`, licence and
  attribution. Pinning a record id rather than a moving branch is what makes a
  request reproducible.
* :class:`FlodisDataset` — one selectable table (its Zenodo file name, a
  description, and its join-key columns). These are the catalog's dict-surface
  rows, keyed by `dataset` name (`"damages"` / `"displacement"`) under the
  inherited :attr:`datasets` field.
* the `columns` map — friendly name -> exact FLODIS CSV header, so the backend
  locates the selector / join-key / impact columns without hard-coding spellings.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `flodis_data_catalog.yaml` (shipped as package data) through
the shared :func:`~earthlens.base.catalog_source.load_catalog` (with a
:class:`~earthlens.base.yaml_loader.CatalogParseCache`), mirroring
`hanze/catalog.py`. :data:`CATALOG_PATH` is the path to the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "flodis_data_catalog.yaml"

#: Zenodo REST content URL for one file of a pinned record.
_CONTENT_URL = "https://zenodo.org/api/records/{record}/files/{name}/content"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path plus
#: the YAML's `(mtime_ns, size)`, so a repeated `Catalog.load()` skips the parse.
#: (`Catalog()` autoloads through `_autoload` and does not consult this cache.)
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class ZenodoRecord(BaseModel):
    """The pinned Zenodo record FLODIS is fetched from.

    Attributes:
        record: The pinned Zenodo record id (`8123096`). Every file URL is
            composed from it, so a request is reproducible.
        concept_doi: The dataset DOI as cited in the paper. Recorded for the
            citation and a future refresh check; never used to fetch.
        version: The dataset version label, if the record carries one.
        data_period: The `first-last` year span the record covers (`"2000-2018"`).
        license: SPDX-ish licence id (`CC-BY-4.0`).
        attribution: The citation obligation the licence carries.

    Examples:
        - The record is the pinned id, and the licence is CC-BY-4.0:
            ```python
            >>> from earthlens.flodis import Catalog
            >>> Catalog().record.record
            8123096
            >>> Catalog().record.license
            'CC-BY-4.0'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: int
    concept_doi: str = ""
    version: str = ""
    data_period: str = ""
    license: str = ""
    attribution: str = ""


class FlodisDataset(BaseModel):
    """One selectable FLODIS table (a row of the catalog's dict surface).

    The `dataset` string (`"damages"`, `"displacement"`) is the parent key in
    :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        file: The file name on the Zenodo record
            (`"FLODIS_mortality_damage.csv"`).
        description: One-line human-readable summary.
        key_columns: The join-key column(s) the table is keyed on — `disasterno`
            (EM-DAT) for `damages`, `GID_1` / `GID_2` (GADM) for `displacement`.
            A caller joins on these to the `emdat` (GDIS) footprints and `gee`
            (Global Flood Database) extents.

    Examples:
        - The content URL is composed from the pinned record and file name:
            ```python
            >>> from earthlens.flodis import Catalog
            >>> damages = Catalog().dataset("damages")
            >>> damages.key_columns
            ('disasterno',)
            >>> damages.content_url(8123096)
            'https://zenodo.org/api/records/8123096/files/FLODIS_mortality_damage.csv/content'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    description: str = ""
    key_columns: tuple[str, ...] = ()

    def content_url(self, record: int) -> str:
        """Return the Zenodo REST content URL this table is served from.

        Args:
            record: The pinned record id the file belongs to.

        Returns:
            str: `https://zenodo.org/api/records/<record>/files/<file>/content`.

        Examples:
            - Compose the REST content URL for a table on a record:
                ```python
                >>> from earthlens.flodis import FlodisDataset
                >>> FlodisDataset(file="FLODIS_displacement.csv").content_url(8123096)
                'https://zenodo.org/api/records/8123096/files/FLODIS_displacement.csv/content'

                ```
        """
        return _CONTENT_URL.format(record=record, name=self.file)


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the FLODIS catalog YAML into the :class:`Catalog` field payload.

    Args:
        files: The contributing YAML files (FLODIS ships a single file).

    Returns:
        dict[str, Any]: The keyword payload for :class:`Catalog` — `record`,
            `datasets` (the two tables) and `columns`.

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
            "FLODIS catalog must pin a Zenodo record."
        )
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'datasets:' block. The "
            "FLODIS catalog must list at least one table."
        )
    columns_yaml = data.get("columns") or {}

    try:
        record = ZenodoRecord(**dict(record_yaml))
        dataset_map = {
            name: FlodisDataset(**dict(body or {}))
            for name, body in datasets_yaml.items()
        }
    except ValidationError as exc:
        raise ValueError(f"{catalog_path} failed validation:\n{exc}") from exc

    for required in ("damages", "displacement"):
        if required not in dataset_map:
            raise ValueError(
                f"{catalog_path} 'datasets:' block is missing required table "
                f"{required!r}. Known: {sorted(dataset_map)}."
            )

    return {
        "record": record,
        "datasets": dataset_map,
        "columns": {str(key): str(value) for key, value in columns_yaml.items()},
        "available_datasets": sorted(dataset_map),
    }


class Catalog(AbstractCatalog[FlodisDataset]):
    """Catalog for the FLODIS backend.

    Reads the bundled `flodis_data_catalog.yaml` (shipped as package data) and
    exposes the pinned Zenodo record, the two selectable tables (as
    :class:`FlodisDataset` rows keyed by `dataset` under the inherited
    :attr:`datasets` field — the `cat["damages"]` / `"damages" in cat` /
    `len(cat)` dict surface), and the friendly-name -> CSV-header map.
    Instantiate with no arguments (`Catalog()`).

    Attributes:
        datasets: Map from a `dataset` string to its :class:`FlodisDataset` row.
        record: The pinned :class:`ZenodoRecord`.
        columns: Friendly name -> exact FLODIS CSV header.

    Examples:
        - List the tables, resolve one, and read the pinned record:
            ```python
            >>> from earthlens.flodis import Catalog
            >>> cat = Catalog()
            >>> cat.tables()
            ['damages', 'displacement']
            >>> cat.dataset("displacement").file
            'FLODIS_displacement.csv'
            >>> "damages" in cat
            True
            >>> cat.column("disasterno")
            'disasterno'

            ```
        - An unknown table raises with a did-you-mean hint:
            ```python
            >>> from earthlens.flodis import Catalog
            >>> Catalog().dataset("damage")
            Traceback (most recent call last):
                ...
            ValueError: 'damage' is not in the FLODIS catalog. Known datasets: ['damages', 'displacement']. Did you mean 'damages'?

            ```
    """

    _catalog_kind: str = "FLODIS catalog"
    _entry_noun: str = "datasets"

    datasets: dict[str, FlodisDataset] = Field(default_factory=dict)
    #: `record` defaults to `None` rather than a placeholder model: the base
    #: :meth:`model_post_init` autoload fills a field only when its current value
    #: is falsy, and a placeholder model instance is truthy, so it would be
    #: skipped. `None` is falsy, so the bundled record loads as intended.
    record: ZenodoRecord | None = Field(default=None, repr=False)
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
        """Read the FLODIS catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, a required block is
                missing, or a row fails validation.

        Examples:
            - Loading the bundled catalog yields the pinned record and tables:
                ```python
                >>> from earthlens.flodis import Catalog
                >>> cat = Catalog.load()
                >>> cat.record.record
                8123096
                >>> cat.tables()
                ['damages', 'displacement']

                ```
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        parsed = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_catalog, provider="FLODIS"
        )
        return cls(**parsed)

    def dataset(self, name: str) -> FlodisDataset:
        """Return the :class:`FlodisDataset` for `name`, with a did-you-mean hint.

        Thin typed alias over :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            name: A FLODIS table name (`"damages"` or `"displacement"`).

        Returns:
            FlodisDataset: The matching row.

        Raises:
            ValueError: If `name` is not a registered table.

        Examples:
            - Resolve a table and read its file name:
                ```python
                >>> from earthlens.flodis import Catalog
                >>> Catalog().dataset("damages").file
                'FLODIS_mortality_damage.csv'

                ```
        """
        return cast("FlodisDataset", self.get_dataset(name))

    def tables(self) -> list[str]:
        """Return the registered table names, sorted.

        Returns:
            list[str]: The table names (`["damages", "displacement"]`).

        Examples:
            - The registered tables come back sorted:
                ```python
                >>> from earthlens.flodis import Catalog
                >>> Catalog().tables()
                ['damages', 'displacement']

                ```
        """
        return sorted(self.datasets)

    def column(self, friendly: str) -> str:
        """Return the exact FLODIS CSV header for a friendly column name.

        Args:
            friendly: A friendly key from the catalog's `columns:` map
                (`"iso3"`, `"year"`, `"disasterno"`, `"gid_1"`, ...).

        Returns:
            str: The exact CSV header (`"ISO3"`).

        Raises:
            KeyError: If `friendly` is not a mapped column.

        Examples:
            - Map friendly keys to their exact FLODIS headers:
                ```python
                >>> from earthlens.flodis import Catalog
                >>> cat = Catalog()
                >>> cat.column("iso3")
                'ISO3'
                >>> cat.column("total_damages_000_usd")
                'total_damages_(000_USD)'

                ```
        """
        return self.columns[friendly]
