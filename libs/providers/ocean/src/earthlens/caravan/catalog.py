"""Extension and variable catalog for the Caravan backend.

Caravan publishes per-catchment daily streamflow plus ERA5-Land forcing as
static archives on Zenodo. This module is the bridge between the friendly
request vocabulary (`dataset="grdc"`, `variables=["streamflow"]`) and what the
archives actually contain: a pinned Zenodo record, the file to read, how that
file is packaged, and the real column names inside it.

Three shapes matter and are modelled separately:

* :class:`Extension` — one Zenodo record set (`base`, `grdc`, `germany`,
  `denmark`, `israel`), carrying its licence, its `sources:` map, and one or
  more :class:`Version` entries.
* :class:`Version` — a specific, reproducible release of an extension. Pinning
  a version rather than the moving concept DOI is what makes a request
  repeatable, and it is what carries `data_period` / `n_catchments` /
  `column_set`. `base` has two — the current `1.6` and the range-readable
  `1.2` — so the cheap path is data, not a special case in code.
* :class:`ArchiveFile` — one downloadable artifact with its size, md5, and
  crucially its `archive_format`. A `zip` is read in place over HTTP Range
  requests; a `tar.gz` is a single gzip stream that must be fetched whole.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "caravan_data_catalog.yaml"

#: Module-level cache of parsed catalog rows, keyed on the resolved path plus
#: the YAML's `st_mtime_ns`, so editing the file invalidates the entry without
#: re-parsing on every `Catalog()`. Mirrors the loaders in the sibling backends.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Any]] = CatalogParseCache()

#: How an archive is packaged, which decides the transport. `zip` carries a
#: central directory and is therefore seekable over HTTP Range; `tar.gz` is one
#: gzip stream and has to be downloaded whole before anything can be read.
ArchiveFormat = Literal["zip", "tar.gz"]

#: Which timeseries encoding a request wants. Both live in the same archive for
#: most extensions; `base` splits them across two Zenodo records.
TimeseriesFormat = Literal["csv", "netcdf"]

#: The known timeseries column-set variants. `current` is the v1.5+ layout with
#: the split ERA5-Land / FAO Penman-Monteith PET pair; `legacy` is base v1.2 and
#: earlier with a single `potential_evaporation_sum`. `camelsde` and
#: `camelses` are `current` plus that extension's own extra columns -
#: Caravan-DE's two observed ones, Caravan-ES's four EFAS/EMO-1 ones.
#:
#: Only `legacy` changes how a column is resolved (see `Variable.column_for`);
#: the other two are descriptive, because their extra columns are reached
#: through per-variable `sources` restrictions rather than the column set.
#: They are still declared so a row states which shape it ships, and so the
#: validator rejects a typo.
ColumnSet = Literal["current", "legacy", "camelsde", "camelses"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful when the catalog is rewritten on disk and a re-parse is wanted
    immediately. Production callers do not need this — the cache key includes
    the file's `st_mtime_ns`, so any real edit invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Any]:
    """Parse, validate, and cache the Caravan catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        dict[str, Any]: `{"datasets": {key: Extension}, "variables": {name:
            Variable}, "available_datasets": [key, ...]}`.

    Raises:
        ValueError: If the file has no `extensions:` or no `variables:` block,
            or a row fails validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    extensions_yaml = data.get("extensions") or {}
    if not extensions_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'extensions:' block. "
            "The Caravan catalog must list at least one extension."
        )
    variables_yaml = data.get("variables") or {}
    if not variables_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'variables:' block. "
            "The Caravan catalog must map at least one variable."
        )

    extensions: dict[str, Extension] = {}
    for name, body in extensions_yaml.items():
        try:
            extensions[name] = Extension(key=name, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} extension {name!r} failed validation:\n{exc}"
            ) from exc

    variables: dict[str, Variable] = {}
    for name, body in variables_yaml.items():
        try:
            variables[name] = Variable(name=name, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} variable {name!r} failed validation:\n{exc}"
            ) from exc

    parsed: dict[str, Any] = {
        "datasets": extensions,
        "variables": variables,
        "available_datasets": sorted(extensions),
        # The informational index, including the records deliberately not
        # wrapped. The drift refresher reads it so a known exclusion is not
        # reported as a new discovery on every run.
        "extension_index": list(data.get("available_extensions") or []),
    }
    _CATALOG_CACHE[key] = parsed
    return parsed


class Variable(BaseModel):
    """One requestable variable and the archive column it maps to.

    The friendly name is the parent key in the catalog's `variables:` block and
    is also stored here, so a resolved row is self-describing.

    Attributes:
        name: The friendly request name (`"total_precipitation"`).
        column: The real column name in a current-era archive
            (`"total_precipitation_sum"`).
        legacy_column: The column name in a `legacy` column-set archive, when it
            differs. Only `potential_evaporation` needs this — base v1.2 and
            earlier ship one `potential_evaporation_sum` instead of the split
            ERA5-Land / FAO pair.
        units: The reporting units (`"mm/d"`, `"degC"`, `"m3/m3"`).
        sources: Archive source directories this variable exists in. Empty (the
            default) means every source has it; `["camelsde"]` marks the two
            Caravan-DE-only observed columns.
        description: One-line human-readable summary.

    Examples:
        - The friendly name and the archive column differ for precipitation:
            ```python
            >>> from earthlens.caravan import Variable
            >>> v = Variable(name="total_precipitation",
            ...             column="total_precipitation_sum", units="mm/d")
            >>> v.column
            'total_precipitation_sum'
            >>> v.sources
            []

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    column: str
    legacy_column: str = ""
    units: str = ""
    sources: list[str] = Field(default_factory=list)
    description: str = ""

    def column_for(self, column_set: ColumnSet) -> str:
        """Return the column name this variable has in `column_set`.

        Args:
            column_set: The archive's column-set variant.

        Returns:
            str: :attr:`legacy_column` when the archive is `legacy` and this
                variable declares one, otherwise :attr:`column`.

        Examples:
            - PET is the one variable whose name changed between eras:
                ```python
                >>> from earthlens.caravan import Variable
                >>> pet = Variable(
                ...     name="potential_evaporation",
                ...     column="potential_evaporation_sum_ERA5_LAND",
                ...     legacy_column="potential_evaporation_sum",
                ... )
                >>> pet.column_for("current")
                'potential_evaporation_sum_ERA5_LAND'
                >>> pet.column_for("legacy")
                'potential_evaporation_sum'

                ```
        """
        if column_set == "legacy" and self.legacy_column:
            return self.legacy_column
        return self.column


class ArchiveFile(BaseModel):
    """One downloadable Zenodo artifact and how it is packaged.

    Attributes:
        record: The pinned Zenodo **version** record id the file belongs to.
            Held per file because `base` splits its CSV and NetCDF timeseries
            across two different records.
        name: The file name on the record.
        size: Size in bytes, as reported by the Zenodo REST API.
        md5: The file's md5 checksum (bare hex, no `md5:` prefix).
        archive_format: `"zip"` (range-readable in place) or `"tar.gz"`
            (must be downloaded whole).
        root_prefix: The directory every member sits under inside the archive,
            or `None` when members start at the archive root. Every value is
            measured from the archive itself, so `None` means "this archive has
            no root directory", never "nobody looked". Recorded for
            documentation and as a cross-check only — member paths are resolved
            from the archive's own index, because this prefix varies per record
            and is absent in several.

    Examples:
        - The format is what decides whether a fetch is cheap:
            ```python
            >>> from earthlens.caravan import ArchiveFile
            >>> f = ArchiveFile(record=15349031, name="x.zip", size=1,
            ...                 md5="abc", archive_format="zip")
            >>> f.is_range_readable
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: int
    name: str
    size: int
    md5: str
    archive_format: ArchiveFormat
    root_prefix: str | None = None

    @property
    def is_range_readable(self) -> bool:
        """Whether a member can be read without downloading the whole file.

        Returns:
            bool: `True` for a `zip`, whose central directory makes it
                seekable over HTTP Range; `False` for a `tar.gz`.
        """
        return self.archive_format == "zip"

    @property
    def url(self) -> str:
        """The Zenodo REST content URL this file is served from.

        Returns:
            str: `https://zenodo.org/api/records/<record>/files/<name>/content`.

        Examples:
            - The URL is composed from the pinned record and file name:
                ```python
                >>> from earthlens.caravan import ArchiveFile
                >>> ArchiveFile(record=15200118, name="Caravan_extension_DK.zip",
                ...             size=1, md5="a", archive_format="zip").url
                'https://zenodo.org/api/records/15200118/files/Caravan_extension_DK.zip/content'

                ```
        """
        return f"https://zenodo.org/api/records/{self.record}/files/{self.name}/content"


class Source(BaseModel):
    """One source dataset directory inside an archive.

    An extension is a Zenodo record; a source is a folder *within* it. Every
    community extension has exactly one, but `base` bundles seven — CAMELS-US,
    CAMELS-AUS, CAMELS-BR, CAMELS-CL, CAMELS-GB, HYSETS and LamaH-CE — which is
    why they are not separately downloadable and never appear as their own
    catalog rows.

    Attributes:
        n_catchments: Catchments this source contributes.
        name: Human-readable name of the upstream dataset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_catchments: int = 0
    name: str = ""


class Version(BaseModel):
    """One pinned, reproducible release of an extension.

    Attributes:
        doi: The version DOI (never the concept DOI, which moves). When a
            release spans two records - `base` publishes its CSV and NetCDF
            timeseries separately - this names one of them; the authoritative
            per-format pointer is `files[<fmt>].record`.
        release_date: Zenodo publication date, `YYYY-MM-DD`.
        data_period: `[first_year, last_year]` the timeseries span.
        n_catchments: Catchments in this release.
        n_catchments_verified: Whether the count was measured from the archive
            index or only derived from the changelog. `base` 1.6 is a `tar.gz`
            and cannot be indexed without downloading it, so its count is
            arithmetic and this is `False`.
        column_set: Which timeseries column-set variant this release ships.
        files: Per timeseries format, the :class:`ArchiveFile` to read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doi: str = ""
    release_date: str = ""
    data_period: tuple[int, int] | None = None
    n_catchments: int = 0
    n_catchments_verified: bool = False
    column_set: ColumnSet = "current"
    files: dict[str, ArchiveFile] = Field(default_factory=dict)

    def file_for(self, timeseries_format: TimeseriesFormat) -> ArchiveFile:
        """Return the archive holding this release's `timeseries_format` data.

        Args:
            timeseries_format: `"csv"` or `"netcdf"`.

        Returns:
            ArchiveFile: The matching file descriptor.

        Raises:
            ValueError: If the release publishes no such format.
        """
        archive = self.files.get(timeseries_format)
        if archive is None:
            raise ValueError(
                f"this Caravan release publishes no {timeseries_format!r} "
                f"timeseries; available: {sorted(self.files)}."
            )
        return archive


class Extension(BaseModel):
    """One Caravan extension — a Zenodo record set with its releases.

    Attributes:
        key: The catalog key used as `dataset=` (`"grdc"`, `"denmark"`).
        title: The record's published title.
        concept_doi: The moving concept DOI. Recorded so the refresh tool can
            discover newer versions; never used to fetch.
        concept_doi_csv: The second concept DOI, when a row's CSV and NetCDF
            archives live under different Zenodo concepts. Only `base` does,
            from v1.6 onward.
        license: SPDX-ish licence id (every current row is `CC-BY-4.0`).
        attribution: The citation obligation the licence carries.
        license_file: Path to the in-archive licence text.
        sources: Archive source directory to its :class:`Source` row.
        default_version: Key into :attr:`versions` used when none is requested.
        versions: Version key to its :class:`Version`.

    Examples:
        - The default release is the one a bare request resolves to:
            ```python
            >>> from earthlens.caravan import Catalog
            >>> grdc = Catalog().get_extension("grdc")
            >>> grdc.default_version
            '0.6'
            >>> grdc.resolve_version().n_catchments
            5356

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    title: str = ""
    concept_doi: str = ""
    concept_doi_csv: str = ""
    license: str = ""
    attribution: str = ""
    license_file: str = ""
    sources: dict[str, Source] = Field(default_factory=dict)
    default_version: str = ""
    versions: dict[str, Version] = Field(default_factory=dict)

    @property
    def source_names(self) -> list[str]:
        """The archive source directory names, sorted.

        Returns:
            list[str]: e.g. `["grdc"]`, or the seven base sources.
        """
        return sorted(self.sources)

    def resolve_version(self, version: str | None = None) -> Version:
        """Return the requested release, or the row's default.

        Args:
            version: A key into :attr:`versions`. `None` (the default) picks
                :attr:`default_version`.

        Returns:
            Version: The matching release.

        Raises:
            ValueError: If `version` is not a known release of this extension;
                the message lists the valid keys.

        Examples:
            - An unknown release names the valid ones:
                ```python
                >>> from earthlens.caravan import Catalog
                >>> Catalog().get_extension("base").resolve_version("9.9")
                Traceback (most recent call last):
                    ...
                ValueError: '9.9' is not a known version of the 'base' Caravan extension. Known versions: ['1.2', '1.6'].

                ```
        """
        wanted = version if version is not None else self.default_version
        release = self.versions.get(wanted)
        if release is None:
            raise ValueError(
                f"{wanted!r} is not a known version of the {self.key!r} Caravan "
                f"extension. Known versions: {sorted(self.versions)}."
            )
        return release


class Catalog(AbstractCatalog):
    """Extension and variable catalog for the Caravan backend.

    Reads the bundled `caravan_data_catalog.yaml` (shipped as package data) and
    exposes its `extensions:` block as :class:`Extension` rows keyed by the
    `dataset=` name, plus the shared `variables:` block as :class:`Variable`
    rows. Instantiate with no arguments (`Catalog()`).

    Attributes:
        extensions: Map from extension key to its :class:`Extension` row.
        variables: Map from friendly variable name to its :class:`Variable`.

    Examples:
        - Look up an extension and the archive it would read:
            ```python
            >>> from earthlens.caravan import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.extensions)
            ['base', 'czechia', 'denmark', 'germany', 'grdc', 'israel', 'spain']
            >>> archive = cat.get_extension("denmark").resolve_version().file_for("csv")
            >>> archive.name
            'Caravan_extension_DK.zip'
            >>> archive.is_range_readable
            True

            ```
    """

    _catalog_kind: str = "Caravan catalog"
    _entry_noun: str = "extensions"

    #: The extension rows live in the base :attr:`datasets` field so the
    #: inherited dict surface (`len`, `in`, `[]`, iteration) and
    #: :meth:`get_dataset`'s did-you-mean hint work unchanged.
    datasets: dict[str, Extension] = Field(default_factory=dict)
    variables: dict[str, Variable] = Field(default_factory=dict)
    #: The YAML's informational `available_extensions:` block, including the
    #: records deliberately not wrapped. Named apart from the
    #: :attr:`available_extensions` property, which lists the supported keys.
    extension_index: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def extensions(self) -> dict[str, Extension]:
        """The extension map — alias for the base :attr:`datasets` field.

        Returns:
            dict[str, Extension]: The same mapping stored in :attr:`datasets`.
        """
        return self.datasets

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `variables` and `available_datasets`
                read from the bundled catalog.
        """
        return dict(_load_catalog_data(CATALOG_PATH))

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Caravan catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            Catalog: A fully-populated catalog.

        Raises:
            ValueError: If a required block is missing or a row fails
                validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(**_load_catalog_data(path))

    @property
    def available_extensions(self) -> list[str]:
        """The sorted list of extension keys.

        Returns:
            list[str]: Every catalog key, sorted.
        """
        return sorted(self.datasets)

    def get_extension(self, key: str) -> Extension:
        """Resolve an extension key to its row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown key.

        Args:
            key: An extension key (`"grdc"`, `"denmark"`).

        Returns:
            Extension: The matching catalog row.

        Raises:
            ValueError: If `key` is not a known extension.
        """
        return cast("Extension", self.get_dataset(key))

    def get_variable(self, dataset_key: str, variable_name: str) -> Variable:
        """Resolve one variable, checking it exists in the extension.

        Args:
            dataset_key: The extension the variable is requested against.
            variable_name: A friendly variable name, or the real archive column
                name (which passes through when it matches a known row).

        Returns:
            Variable: The matching variable row.

        Raises:
            ValueError: If the variable is unknown, or is restricted to source
                datasets the extension does not contain (e.g. asking
                Caravan-DE's `water_level` of the GRDC extension).
        """
        row = self.variables.get(variable_name) or self._by_column(variable_name)
        if row is None:
            raise ValueError(
                f"{variable_name!r} is not a Caravan variable. Known variables: "
                f"{sorted(self.variables)}."
            )
        if row.sources:
            available = set(self.get_extension(dataset_key).sources)
            if not available.intersection(row.sources):
                raise ValueError(
                    f"variable {row.name!r} exists only in the "
                    f"{sorted(row.sources)} source data, which the "
                    f"{dataset_key!r} extension does not contain."
                )
        return row

    def _by_column(self, column: str) -> Variable | None:
        """Find a variable by its real archive column name.

        Lets a caller pass `"total_precipitation_sum"` as readily as the
        friendly `"total_precipitation"`, since the archive's own header is
        what most users have in front of them.

        Args:
            column: A real column name from a Caravan timeseries file.

        Returns:
            Variable | None: The matching row, or `None`.
        """
        for row in self.variables.values():
            if column in {row.column, row.legacy_column} and column:
                return row
        return None
