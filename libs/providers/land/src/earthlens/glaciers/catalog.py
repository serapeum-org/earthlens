"""Dataset + GTN-G region catalog for the glaciers backend.

The glaciers backend routes a request to one of three open glacier sources —
the Randolph Glacier Inventory 7.0 (`rgi`, per-region outline shapefiles), the
GLIMS time-series outline database (`glims`, WFS bbox query), and the WGMS
Fluctuations of Glaciers database (`wgms`, tabular CSV tables). This module is
the bridge from a dataset id (`"rgi:outlines"`, `"glims:outlines"`,
`"wgms:mass_balance"`, …) to its source, output kind, and the source-specific
request detail needed to fetch it.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled sharded `catalog/` directory (`rgi.yaml` / `glims.yaml` /
`wgms.yaml` + `_index.yaml`) and exposes each row as a :class:`Dataset`. The
`rgi.yaml` file also ships a `regions:` block — the GTN-G first-order region
table (id → name + bbox(es) + per-region download URL) that drives the
bbox → region(s) mapping — exposed as :attr:`Catalog.regions` of
:class:`Region` rows. Resolve one dataset with :meth:`Catalog.get` (a
did-you-mean hint on an unknown id); list the shipped ids with
:meth:`Catalog.available`.

:data:`CATALOG_PATH` is the path to the bundled `catalog/` directory; tests may
monkey-patch it at a temporary directory or a single YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled sharded catalog directory (`rgi.yaml` / `glims.yaml` /
#: `wgms.yaml` + the `_index.yaml` informational index). Tests can monkey-patch
#: this attribute to redirect the loader at a temporary directory or a single
#: YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-source file invalidates the entry without re-parsing on an unchanged
#: tree. The value is the `(datasets, regions, available)` triple the fields are
#: built from.
_CATALOG_CACHE: dict[Any, tuple[dict[str, Dataset], dict[str, Region], list[str]]] = (
    CatalogParseCache()
)

#: The three glacier sources a :class:`Dataset` row can name.
Source = Literal["rgi", "glims", "wgms"]

#: The two output shapes a glacier dataset can emit (vector -> FeatureCollection
#: for rgi/glims outlines, tabular -> DataFrame for wgms fluctuations).
#: `OUTPUT_KIND` is set per instance from this.
OutputKind = Literal["vector", "tabular"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys include every
    contributing file's `st_mtime_ns`, so any real file mutation invalidates the
    entry on its own.
    """
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='glaciers', shard_noun='per-source')


def _load_catalog_data(
    path: Path,
) -> tuple[dict[str, Dataset], dict[str, Region], list[str]]:
    """Parse, validate, and cache the glaciers catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `datasets:` maps are
    unioned (an id declared in two files is an error), `available_datasets:`
    lists are concatenated (the `_index.yaml` index), and the single `regions:`
    block (in `rgi.yaml`) is read once. Cached on the resolved path plus every
    contributing file's `mtime_ns`, so a second `Catalog()` on an unchanged tree
    skips both YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default :data:`CATALOG_PATH`) or a single
            `*.yaml`.

    Returns:
        A `(datasets, regions, available)` triple: the dataset map keyed by id,
        the GTN-G region map keyed by region id, and the `available_datasets:`
        index.

    Raises:
        ValueError: If no file has a `datasets:` block, an id is declared in two
            files, a dataset / region row fails validation, or a curated id is
            absent from `available_datasets:`.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    merged_datasets_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    regions_yaml: dict[str, Any] = {}
    merged_available: list[str] = []
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for dataset_id, body in (data.get("datasets") or {}).items():
            if dataset_id in merged_datasets_yaml:
                raise ValueError(
                    f"dataset {dataset_id!r} declared in two catalog files: "
                    f"{origin[dataset_id]} and {file_path}"
                )
            merged_datasets_yaml[dataset_id] = body
            origin[dataset_id] = file_path
        for region_id, body in (data.get("regions") or {}).items():
            if region_id in regions_yaml:
                raise ValueError(
                    f"region {region_id!r} declared twice in the catalog ({file_path})."
                )
            regions_yaml[region_id] = body

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The glaciers catalog must list at least one dataset."
        )

    available = set(merged_available)
    datasets: dict[str, Dataset] = {}
    for dataset_id, body in merged_datasets_yaml.items():
        try:
            datasets[dataset_id] = Dataset(id=dataset_id, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[dataset_id]} dataset {dataset_id!r} failed validation:\n{exc}"
            ) from exc
        if available and dataset_id not in available:
            raise ValueError(
                f"dataset {dataset_id!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[dataset_id]}); add it to "
                "_index.yaml too."
            )

    regions: dict[str, Region] = {}
    for region_id, body in regions_yaml.items():
        try:
            regions[region_id] = Region(id=region_id, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(f"region {region_id!r} failed validation:\n{exc}") from exc

    value = (datasets, regions, merged_available)
    _CATALOG_CACHE[key] = value
    return value


class Dataset(BaseModel):
    """One glaciers catalog row.

    The dataset id is the parent key in :attr:`Catalog.datasets` and is also
    stored on the row as :attr:`id` so a resolved :class:`Dataset` is
    self-describing. Which source-specific fields are populated depends on
    :attr:`source`; a cross-field validator enforces that the right ones are
    present.

    Attributes:
        id: The dataset id (`"rgi:outlines"`, `"wgms:mass_balance"`).
        source: Which source serves it — `"rgi"`, `"glims"`, or `"wgms"`.
        output_kind: `"vector"` (a `FeatureCollection`, rgi/glims) or
            `"tabular"` (a `DataFrame`, wgms). Copied onto the backend's
            `OUTPUT_KIND` per instance.
        long_name: Human-readable label.
        citation: The source's citation string, logged once on use.
        table: WGMS only — the CSV table name inside the FoG zip
            (`"mass_balance"`, `"front_variation"`, `"state"`).
        archive_url: WGMS only — the FoG database zip download URL.
        wfs_url: GLIMS only — the GeoServer WFS endpoint URL.
        wfs_typename: GLIMS only — the WFS feature-type name.

    Examples:
        - Build an RGI row directly:
            ```python
            >>> from earthlens.glaciers import Dataset
            >>> row = Dataset(id="rgi:outlines", source="rgi", output_kind="vector")
            >>> row.source
            'rgi'
            >>> row.output_kind
            'vector'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source: Source
    output_kind: OutputKind
    long_name: str = ""
    citation: str = ""

    # WGMS
    table: str | None = None
    archive_url: str | None = None
    # GLIMS
    wfs_url: str | None = None
    wfs_typename: str | None = None

    @model_validator(mode="after")
    def _check_source_fields(self) -> Dataset:
        """Enforce the per-source required fields and output kind.

        Returns:
            The validated row.

        Raises:
            ValueError: If the row's `output_kind` does not match its `source`,
                a `wgms` row omits `table` / `archive_url`, or a `glims` row
                omits `wfs_url` / `wfs_typename`.
        """
        if self.source in ("rgi", "glims") and self.output_kind != "vector":
            raise ValueError(
                f"{self.source} dataset {self.id!r} must be output_kind 'vector'"
            )
        if self.source == "wgms":
            if self.output_kind != "tabular":
                raise ValueError(
                    f"wgms dataset {self.id!r} must be output_kind 'tabular'"
                )
            if not (self.table and self.archive_url):
                raise ValueError(
                    f"wgms dataset {self.id!r} needs table and archive_url"
                )
        if self.source == "glims" and not (self.wfs_url and self.wfs_typename):
            raise ValueError(
                f"glims dataset {self.id!r} needs wfs_url and wfs_typename"
            )
        return self


class Region(BaseModel):
    """One GTN-G first-order region row (RGI per-region download metadata).

    Attributes:
        id: The two-digit GTN-G region id (`"01"` … `"19"`).
        name: The region's full name (`"Central Europe"`).
        bboxes: One or more `[west, south, east, north]` bounding boxes in
            EPSG:4326. Most regions have a single box; region 10 (North Asia)
            crosses the antimeridian and carries two.
        url: The full IHP-WINS resource download URL for this region's outline
            shapefile zip.

    Examples:
        - A region's corners and download URL:
            ```python
            >>> from earthlens.glaciers import Catalog
            >>> region = Catalog().regions["11"]
            >>> region.name
            'Central Europe'
            >>> region.bboxes[0]
            [-6.0, 40.0, 26.0, 50.0]

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    bboxes: list[list[float]]
    url: str

    @model_validator(mode="after")
    def _check_bboxes(self) -> Region:
        """Validate every bbox is a `[west, south, east, north]` quadruple.

        Returns:
            The validated row.

        Raises:
            ValueError: If `bboxes` is empty or any box is not length 4.
        """
        if not self.bboxes:
            raise ValueError(f"region {self.id!r} has no bboxes")
        for box in self.bboxes:
            if len(box) != 4:
                raise ValueError(
                    f"region {self.id!r} bbox {box!r} must be "
                    "[west, south, east, north]"
                )
        return self


class Catalog(AbstractCatalog):
    """Dataset + region catalog for the glaciers backend.

    Reads the bundled sharded `catalog/` directory (shipped as package data) and
    exposes its `datasets:` blocks as a map of :class:`Dataset` rows keyed by id,
    plus the GTN-G `regions:` table as a map of :class:`Region` rows.
    Instantiate with no arguments (`Catalog()`). Resolve one row with
    :meth:`get`, list the shipped ids with :meth:`available`, and read the region
    table via :attr:`regions`.

    Attributes:
        datasets: Map from dataset id to its :class:`Dataset` row.
        regions: Map from GTN-G region id to its :class:`Region` row.

    Examples:
        - Resolve rows and a region:
            ```python
            >>> from earthlens.glaciers import Catalog
            >>> cat = Catalog()
            >>> cat.get("rgi:outlines").output_kind
            'vector'
            >>> cat.get("wgms:mass_balance").output_kind
            'tabular'
            >>> cat.regions["11"].name
            'Central Europe'

            ```
        - An unknown but close id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.glaciers import Catalog
            >>> Catalog().get("rgi:outline")
            Traceback (most recent call last):
                ...
            ValueError: 'rgi:outline' is not in the glaciers catalog. Known datasets: [...]. Did you mean 'rgi:outlines'?

            ```
    """

    _catalog_kind: str = "glaciers catalog"
    _entry_noun: str = "datasets"

    #: The dataset rows live in the base :attr:`datasets` field so the inherited
    #: dict surface (`len`, `in`, `[]`, iteration) and :meth:`get_dataset`'s
    #: did-you-mean hint work unchanged.
    datasets: dict[str, Dataset] = Field(default_factory=dict)

    #: GTN-G first-order region id -> :class:`Region` (RGI download metadata).
    regions: dict[str, Region] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `regions`, `available_datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "regions": loaded.regions,
            "available_datasets": loaded.available_datasets,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the glaciers catalog from disk.

        Args:
            catalog_path: Path to the catalog directory or a single YAML.
                Defaults to the module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the catalog has no `datasets:` block, or a row fails
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        datasets, regions, available = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(datasets),
            regions=dict(regions),
            available_datasets=list(available),
        )

    def get(self, dataset_id: str) -> Dataset:
        """Resolve a dataset id to its :class:`Dataset` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown id.

        Args:
            dataset_id: A shipped dataset id (`"rgi:outlines"`).

        Returns:
            Dataset: The matching catalog row.

        Raises:
            ValueError: If `dataset_id` is not a known dataset; the message names
                the catalog kind and, when a close match exists, adds a
                did-you-mean hint.
        """
        return cast("Dataset", self.get_dataset(dataset_id))

    def available(self) -> list[str]:
        """Return the sorted list of shipped dataset ids.

        Returns:
            list[str]: Every catalog key, sorted.
        """
        return sorted(self.datasets)
