"""Dataset catalog for the EM-DAT backend.

The EM-DAT backend serves disaster event/impact data through two sanctioned
routes, and this module is the bridge from a dataset id (`"emdat:events"`,
`"gdis:points"`, `"gdis:polygons"`) to the provider, the output kind, and the
transport detail needed to fetch it.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `emdat_data_catalog.yaml` and exposes each row as a
:class:`Dataset`. The YAML also ships a `hazard_vocabularies:` block — one
canonical disaster-type list per source — which :meth:`Catalog.normalize_hazard`
uses to turn a caller's spelling into the canonical one for a given dataset.
Resolve one dataset with
:meth:`Catalog.get` (a did-you-mean hint on an unknown id); list the shipped ids
with :meth:`Catalog.available`.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

import difflib
import fnmatch
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "emdat_data_catalog.yaml"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus
#: the YAML's `st_mtime_ns`, so editing the file invalidates the entry without
#: re-parsing on every `Catalog()`. The value is the
#: `(datasets, hazard_vocabularies)` pair both fields are built from.
_CATALOG_CACHE: dict[
    tuple[str, int], tuple[dict[str, Dataset], dict[str, tuple[str, ...]]]
] = CatalogParseCache()

#: The two routes a :class:`Dataset` row can name — `dataverse` is the anonymous
#: UCLouvain archive, `earthdata` is GDIS behind an Earthdata Login.
Provider = Literal["dataverse", "earthdata"]

#: The two output shapes an EM-DAT dataset can emit (tabular -> DataFrame,
#: vector -> FeatureCollection). `OUTPUT_KIND` is set per instance from this.
OutputKind = Literal["tabular", "vector"]

#: The on-disk formats a GDIS granule ships in.
GdisFormat = Literal["csv", "gpkg"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful when the catalog is rewritten on disk and a re-parse is wanted
    immediately. Ordinary callers do not need this — the cache key includes the
    file's `st_mtime_ns`, so any real edit invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(
    path: Path,
) -> tuple[dict[str, Dataset], dict[str, tuple[str, ...]]]:
    """Parse, validate, and cache the catalog at `path`.

    Reads the `datasets:` and `hazard_vocabularies:` blocks and validates each
    dataset row as a :class:`Dataset`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        A `(datasets, hazard_vocabularies)` pair: the dataset map keyed by id,
        and each named disaster-type vocabulary in canonical form.

    Raises:
        ValueError: If the file has no `datasets:` block, or a row fails
            :class:`Dataset` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The EM-DAT catalog must list at least one dataset."
        )
    rows: dict[str, Dataset] = {}
    for dataset_id, body in datasets_yaml.items():
        try:
            rows[dataset_id] = Dataset(id=dataset_id, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {dataset_id!r} failed validation:\n{exc}"
            ) from exc
    vocabularies = {
        str(name): tuple(str(entry).strip().lower() for entry in (entries or []))
        for name, entries in (data.get("hazard_vocabularies") or {}).items()
    }
    for dataset_id, row in rows.items():
        if row.hazard_vocabulary not in vocabularies:
            raise ValueError(
                f"{path} dataset {dataset_id!r} names hazard_vocabulary "
                f"{row.hazard_vocabulary!r}, which is not in the "
                f"'hazard_vocabularies:' block. Known: {sorted(vocabularies)}."
            )

    value = (rows, vocabularies)
    _CATALOG_CACHE[key] = value
    return value


class Dataset(BaseModel):
    """One EM-DAT catalog row.

    The dataset id is the parent key in :attr:`Catalog.datasets` and is also
    stored on the row as :attr:`id` so a resolved :class:`Dataset` is
    self-describing. Which transport fields are populated depends on
    :attr:`provider`; a cross-field validator enforces that the right ones are
    present.

    Attributes:
        id: The dataset id (`"emdat:events"`).
        provider: Which route serves it — `"dataverse"` or `"earthdata"`.
        output_kind: `"tabular"` (a `DataFrame`) or `"vector"` (a
            `FeatureCollection`). Copied onto the backend's `OUTPUT_KIND` per
            instance.
        long_name: Human-readable label for the dataset.
        description: Longer prose describing what the dataset covers, shown
            by `earthlens datasets show`. It is *not* what
            `earthlens datasets search` matches on — that covers provider, id
            and title, and `record_title` prefers `long_name` — which is why
            the hazard names live in :attr:`long_name` as well.
        dataverse_base: Base URL of the Dataverse installation
            (`provider="dataverse"` only).
        doi: The archive's persistent id (`provider="dataverse"` only).
        file_pattern: `fnmatch` pattern matched against the `:latest` version's
            file listing to find the data file. The archive file name carries a
            release-date prefix that changes every version, so it is never
            resolved by a hard-coded file id (`provider="dataverse"` only).
        sheet: Worksheet holding the table (`provider="dataverse"` only).
        short_name: CMR collection short name (`provider="earthdata"` only).
        granule: The granule file name to fetch (`provider="earthdata"` only).
        member: The file inside the granule zip (`provider="earthdata"` only).
        format: `"csv"` or `"gpkg"` (`provider="earthdata"` only).
        layer: Layer name inside a GeoPackage (`format="gpkg"` only).
        encoding: Text encoding of a CSV member (`format="csv"` only).
        download_mb: Approximate download size, used for the large-download
            warning.
        id_column: Column holding the per-event identifier.
        type_column: Column holding the disaster type.
        hazard_vocabulary: Name of the `hazard_vocabularies:` entry this
            dataset validates `hazard=` against. The two sources do not
            share a vocabulary and neither list contains the other — GDIS has
            `landslide`, EM-DAT files that under mass movement, and the archive
            carries the technological group GDIS lacks.
        iso_column: Column holding the ISO3 country code, when present.
        year_column: Column holding the event year (`"year"` for the GDIS CSV,
            `"Start Year"` for the EM-DAT archive). `None` for the GDIS
            GeoPackage, which carries no date field at all.
        year_from_id_prefix: Whether the year must be derived from the 4-digit
            prefix of :attr:`id_column` (the GDIS GeoPackage).
        latitude_column: Column holding the latitude, when present.
        longitude_column: Column holding the longitude, when present.
        start_year: First year covered by the dataset.
        end_year: Last year covered, or `None` for a living archive.
        licence: SPDX-style licence identifier.
        licence_url: Canonical URL for the licence text.
        restricted_use: Whether the licence restricts who may use the data for
            free, which triggers a `LicenseWarning` on download.
        terms_url: URL of the terms of use, when the data carries them.
        citation: The attribution string to surface to the user.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    provider: Provider
    output_kind: OutputKind
    long_name: str
    description: str | None = None

    dataverse_base: str | None = None
    doi: str | None = None
    file_pattern: str | None = None
    sheet: str | None = None

    short_name: str | None = None
    granule: str | None = None
    member: str | None = None
    format: GdisFormat | None = None
    layer: str | None = None
    encoding: str | None = None
    download_mb: float | None = None

    id_column: str | None = None
    type_column: str | None = None
    hazard_vocabulary: str = "gdis"
    iso_column: str | None = None
    year_column: str | None = None
    year_from_id_prefix: bool = False
    latitude_column: str | None = None
    longitude_column: str | None = None

    start_year: int | None = None
    end_year: int | None = None

    licence: str
    licence_url: str | None = None
    restricted_use: bool = False
    terms_url: str | None = None
    citation: str | None = None

    @model_validator(mode="after")
    def _check_provider_fields(self) -> Dataset:
        """Check the row carries the fields its provider needs.

        Returns:
            Dataset: The validated row.

        Raises:
            ValueError: When a provider-specific field is missing, or when a
                `gpkg` row omits its layer.
        """
        if self.provider == "dataverse":
            missing = [
                name
                for name in ("dataverse_base", "doi", "file_pattern", "sheet")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"dataset {self.id!r} (dataverse) is missing required "
                    f"field(s): {', '.join(missing)}."
                )
        else:
            missing = [
                name
                for name in ("short_name", "granule", "member", "format")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"dataset {self.id!r} (earthdata) is missing required "
                    f"field(s): {', '.join(missing)}."
                )
            if self.format == "gpkg" and not self.layer:
                raise ValueError(
                    f"dataset {self.id!r} is format='gpkg' and must name its `layer:`."
                )
        if self.year_from_id_prefix and not self.id_column:
            raise ValueError(
                f"dataset {self.id!r} sets `year_from_id_prefix: true` but names "
                "no `id_column:`, so no year could ever be derived and every "
                "windowed request would return nothing."
            )
        if self.year_column is None and not self.year_from_id_prefix:
            raise ValueError(
                f"dataset {self.id!r} must either name a `year_column:` or set "
                "`year_from_id_prefix: true` so a date window can be applied."
            )
        return self

    def matches_file(self, filename: str) -> bool:
        """Report whether `filename` is this row's data file.

        Args:
            filename: A file name from the Dataverse version listing.

        Returns:
            bool: `True` when the name matches :attr:`file_pattern`.
        """
        return bool(self.file_pattern) and fnmatch.fnmatch(
            filename, cast("str", self.file_pattern)
        )


class Catalog(AbstractCatalog):
    """Loader for the bundled EM-DAT dataset catalog.

    Reads `emdat_data_catalog.yaml` on construction (`Catalog()`) and exposes
    the parsed rows as :class:`Dataset` models, plus the canonical GDIS hazard
    vocabulary.

    Attributes:
        datasets: Map from dataset id to its :class:`Dataset` row.
        hazard_vocabularies: Each named disaster-type vocabulary, lower-case and
            stripped. `gdis` holds the eight values GDIS ships; `emdat` holds
            EM-DAT's fuller `Disaster Type` list including technological
            hazards.

    Examples:
        - Resolve a row and normalise a hazard name:
            ```python
            >>> from earthlens.emdat import Catalog
            >>> cat = Catalog()
            >>> cat.get("emdat:events").provider
            'dataverse'
            >>> cat.get("gdis:points").output_kind
            'vector'
            >>> cat.normalize_hazard("Flood", cat.get("gdis:points"))
            'flood'
            >>> cat.normalize_hazard("Wildfire", cat.get("emdat:events"))
            'wildfire'

            ```
        - An unknown but close id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.emdat import Catalog
            >>> Catalog().get("gdis:point")
            Traceback (most recent call last):
                ...
            ValueError: 'gdis:point' is not in the EM-DAT catalog. Known datasets: [...]. Did you mean 'gdis:points'?

            ```
    """

    _catalog_kind: str = "EM-DAT catalog"
    _entry_noun: str = "datasets"

    #: The dataset rows live in the base :attr:`datasets` field so the inherited
    #: dict surface (`len`, `in`, `[]`, iteration) and :meth:`get_dataset`'s
    #: did-you-mean hint work unchanged.
    datasets: dict[str, Dataset] = Field(default_factory=dict)

    #: Named disaster-type vocabularies, lower-case and stripped.
    hazard_vocabularies: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` and `hazard_types` read from the
                bundled catalog.
        """
        loaded = cls.load()
        return {
            "datasets": loaded.datasets,
            "hazard_vocabularies": loaded.hazard_vocabularies,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the EM-DAT catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block, or a row fails
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        datasets, vocabularies = _load_catalog_data(catalog_path)
        return cls(datasets=dict(datasets), hazard_vocabularies=dict(vocabularies))

    def get(self, dataset_id: str) -> Dataset:
        """Resolve a dataset id to its :class:`Dataset` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown id.

        Args:
            dataset_id: A shipped dataset id (`"emdat:events"`).

        Returns:
            Dataset: The matching catalog row.

        Raises:
            ValueError: If `dataset_id` is not a known dataset; the message
                names the catalog kind and, when a close match exists, adds a
                did-you-mean hint.
        """
        return cast("Dataset", self.get_dataset(dataset_id))

    def available(self) -> list[str]:
        """Return the sorted list of shipped dataset ids.

        Returns:
            list[str]: Every catalog key, sorted.
        """
        return sorted(self.datasets)

    def vocabulary_for(self, dataset: Dataset) -> tuple[str, ...]:
        """Return the disaster-type vocabulary `dataset` validates against.

        Args:
            dataset: A resolved catalog row.

        Returns:
            tuple[str, ...]: The canonical hazard names that row accepts.
        """
        return self.hazard_vocabularies.get(dataset.hazard_vocabulary, ())

    def normalize_hazard(self, hazard: str, dataset: Dataset) -> str:
        """Turn a caller's hazard spelling into the canonical one for `dataset`.

        Validation is per dataset, because the two sources do not share a
        vocabulary. The two lists overlap but neither contains the other: GDIS
        has `landslide`, which EM-DAT files under mass movement, and the archive
        carries the whole technological group GDIS lacks. Checking
        an archive request against the GDIS list would reject valid EM-DAT
        types such as `"wildfire"` or `"industrial accident (general)"`.

        The shipped GDIS data is also not internally consistent — the
        GeoPackage spells one value `"extreme temperature "` with a trailing
        space while the same table published on Earth Engine spells it without
        — so both sides are compared stripped and lower-cased.

        Args:
            hazard: A hazard name in any casing, with or without surrounding
                whitespace (`"Flood"`, `"extreme temperature "`).
            dataset: The row whose vocabulary applies.

        Returns:
            str: The canonical vocabulary entry (`"flood"`).

        Raises:
            ValueError: If `hazard` is not a disaster type of `dataset`; the
                message names the dataset, lists its vocabulary and, when a
                close match exists, adds a did-you-mean hint.
        """
        vocabulary = self.vocabulary_for(dataset)
        canonical = hazard.strip().lower()
        if canonical in vocabulary:
            return canonical
        close = difflib.get_close_matches(canonical, vocabulary, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{hazard!r} is not a disaster type of {dataset.id!r}. Known types: "
            f"{list(vocabulary)}.{hint}"
        )
