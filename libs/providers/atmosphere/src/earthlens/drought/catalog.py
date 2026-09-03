"""Dataset catalog loader for the `earthlens.drought` backend.

The drought backend reaches three live sources with three different
transports (USDM GeoJSON polygons, Copernicus EDO/GDO OGC WCS rasters, CSIC
SPEIbase NetCDF rasters), each with a different output shape. Curating that
as config-as-code in a sharded `catalog/` directory keeps the backend a thin
router: per-source `*.yaml` files (`usdm.yaml`, `edo.yaml`, `gdo.yaml`,
`speibase.yaml`) plus an `_index.yaml` carrying the merged
`available_datasets:` list. The loader merges every file at construction
time and validates each row against the typed `Dataset` pydantic model.

Each row carries the four fields the backend routes on — `transport`
(`usdm-geojson` / `edo-wcs` / `netcdf-url`), `endpoint`, `coverage` (WCS id
or `None`), `output_kind` (`raster` / `vector`) — plus the metadata the
facade and the per-source attribution log need (`source`, `cadence`,
`native_crs`, `license_note`).

`Catalog.get(id)` resolves a dataset id and emits a did-you-mean
`ValueError` when the id is unknown (the shipped helper on
`AbstractCatalog`). The catalog is cached on `(path, mtime_ns)` so repeated
construction is free; reassigning the module-level `CATALOG_PATH` redirects
the loader at another directory or a single YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"
"""Path to the bundled `catalog/` directory of per-source YAML files plus
the `_index.yaml` informational index. Reassign this attribute to redirect
the loader at another directory or a single YAML file."""

_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset]]] = CatalogParseCache()

TransportLiteral = Literal["usdm-geojson", "edo-wcs", "netcdf-url"]
"""The three live transports the drought backend dispatches on. `_fetch`
routes purely on this string; adding a fourth transport (e.g. a drought.gov
gridded raster) means one new literal plus one new branch in `_fetch`."""

OutputKindLiteral = Literal["raster", "vector"]
"""Per-row output shape. `vector` rows return a `FeatureCollection`;
`raster` rows return a `list[Path]`. The backend copies this onto its
per-instance `OUTPUT_KIND` so the facade gates `aggregate=` correctly
(rejected for vector, forwarded for raster)."""

CadenceLiteral = Literal["weekly", "10day", "monthly"]
"""Source release cadence used by `snap_to_cadence` in `_helpers.py` —
USDM is weekly (Thursday release, Tuesday valid), EDO/GDO indicators are
10-day periods, SPEIbase is monthly."""


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful after rewriting the catalog on disk to force a re-parse.
    Production callers do not need this — the cache keys include every
    contributing file's `st_mtime_ns`, so any real file mutation invalidates
    the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='drought', shard_noun='per-source')


def _load_catalog_data(
    path: Path,
) -> tuple[list[str], dict[str, Dataset]]:
    """Parse, validate, and cache the drought catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (an id declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Dataset]]: The merged `available_datasets:`
            index and the curated dataset map.

    Raises:
        ValueError: If no file has a `datasets:` block, an id is declared in
            two files, a dataset row fails validation, or a curated id is
            absent from `available_datasets:`.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    merged_available: list[str] = []
    merged_datasets_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for ds_key, ds_body in (data.get("datasets") or {}).items():
            if ds_key in merged_datasets_yaml:
                raise ValueError(
                    f"drought dataset {ds_key!r} declared in two catalog files: "
                    f"{origin[ds_key]} and {file_path}"
                )
            merged_datasets_yaml[ds_key] = ds_body
            origin[ds_key] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one curated dataset."
        )

    structural: dict[str, Dataset] = {}
    for ds_key, ds_body in merged_datasets_yaml.items():
        try:
            structural[ds_key] = Dataset(id=ds_key, **(ds_body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[ds_key]} dataset {ds_key!r} failed validation:\n{exc}"
            ) from exc

    missing = [ds_id for ds_id in structural if ds_id not in merged_available]
    if missing:
        raise ValueError(
            "drought catalog: curated dataset ids absent from "
            f"`available_datasets:` index: {sorted(missing)}"
        )

    _CATALOG_CACHE[key] = (merged_available, structural)
    return _CATALOG_CACHE[key]


class Dataset(BaseModel):
    """One curated drought dataset row (USDM / EDO / GDO / SPEIbase).

    Mirrors a single `datasets.<id>:` block in one of the per-source
    `catalog/*.yaml` files. The dataset id is the parent key in the YAML and
    is copied onto the row at load time so the backend can echo it back into
    output filenames without indexing back through the catalog dict.

    Attributes:
        id: The dataset id (the catalog key, e.g. `"usdm"`,
            `"edo-spaST"`, `"speibase-12"`). Copied from the YAML key.
        source: Human-readable source / version string used in the
            success log line (`"Copernicus European Drought Observatory
            (EDO)"`, `"CSIC SPEIbase v2.11 (Vicente-Serrano et al., CRU
            TS 4.09)"`, …).
        transport: The transport the backend's `_fetch` dispatches on.
            One of `"usdm-geojson"`, `"edo-wcs"`, `"netcdf-url"`.
        endpoint: URL or URL template. USDM has a `{ymd}` placeholder
            substituted with the **Tuesday valid date** (USDM releases on
            Thursday for the prior Tuesday, and the JSON URL stem is
            keyed on that Tuesday — verified live; every Thursday URL
            returns 404); EDO/GDO is the WCS map endpoint (the coverage
            and time-subset go in the request, not the URL); SPEIbase
            is the literal per-scale NetCDF URL.
        coverage: WCS `CoverageId` for `"edo-wcs"` rows; `None` for the
            other transports.
        output_kind: Per-row output shape — `"vector"` for USDM,
            `"raster"` for EDO/GDO/SPEIbase. Copied onto the backend
            instance's `OUTPUT_KIND` (`G1`).
        cadence: Source release cadence — `"weekly"` (USDM),
            `"10day"` (most EDO/GDO indicators), `"monthly"` (SPEIbase,
            some EDO/GDO indicators like `spgTS`). Drives the
            date-snapping helper.
        native_crs: The CRS the source delivers in, as an EPSG string
            (`"EPSG:4326"`). USDM was documented Albers but ships
            `GCS_WGS_1984` today (`G4` — kept as a `to_crs("EPSG:4326")`
            defensive no-op in the backend).
        timescale: The Copernicus `SELECTED_TIMESCALE` value for
            `edo-wcs` rows — a one-or-two-digit string naming the SPI /
            SPEI integration window in months (`"01"`, `"03"`, `"06"`,
            `"12"`, …). The SPI coverages (`spaST`, `spcLT`, `spgTS`)
            **require** it; the other EDO/GDO indicators accept and ignore
            it, so every `edo-wcs` row carries one (default `"01"`). It is
            config-as-code: change a row's `timescale` in the YAML to pick
            a different window without touching the backend. `None` for
            the non-WCS transports.
        license_note: Short attribution string logged once on success
            (`"Copernicus EMS — free reuse with attribution"`,
            `"USDM public domain — cite NDMC / UNL"`, `"CC-BY 4.0 — cite
            Vicente-Serrano et al. and SPEIbase v2.11"`).

    Examples:
        - Inspect a USDM vector row:
            ```python
            >>> from earthlens.drought import Catalog
            >>> ds = Catalog().get("usdm")
            >>> ds.output_kind, ds.transport, ds.cadence
            ('vector', 'usdm-geojson', 'weekly')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source: str
    transport: TransportLiteral
    endpoint: str
    coverage: str | None = None
    output_kind: OutputKindLiteral
    cadence: CadenceLiteral
    native_crs: str = "EPSG:4326"
    timescale: str | None = None
    license_note: str = ""


class Catalog(AbstractCatalog[Dataset]):
    """Dataset catalog for the `earthlens.drought` backend.

    Reads the bundled `catalog/` directory (shipped as package data) and
    exposes its consumed top-level sections as typed pydantic fields.
    Instantiate with no arguments (`Catalog()`) — `model_post_init` parses
    the YAML and populates every field in one pass. Mirrors the
    `earthlens.eumetsat` / `earthlens.earthdata` / `earthlens.ghsl`
    catalogs: `datasets` (the curated map) and `available_datasets` (the
    informational index).

    Attributes:
        available_datasets: Informational list of every curated dataset
            id, merged across every per-source YAML. Runtime code does
            not consume it; it seeds the `earthlens datasets list
            drought` CLI and the audit gate.
        datasets: Structural map keyed by dataset id. Each value is a
            `Dataset`.

    Examples:
        - List how many datasets are curated:
            ```python
            >>> from earthlens.drought import Catalog
            >>> len(Catalog().datasets) >= 25
            True

            ```
    """

    _catalog_kind: str = "drought catalog"
    _entry_noun: str = "datasets"

    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, Dataset] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `available_datasets`, `datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "available_datasets": loaded.available_datasets,
            "datasets": loaded.datasets,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the drought catalog from disk (cached).

        Args:
            catalog_path: Path to the `catalog/` directory or a single
                `*.yaml` file. Defaults to module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: Propagated from `_load_catalog_data`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, datasets = _load_catalog_data(catalog_path)
        return cls(
            available_datasets=list(available),
            datasets=dict(datasets),
        )

    def get(self, dataset_id: str) -> Dataset:
        """Resolve a dataset id to its row, with a did-you-mean on miss.

        Args:
            dataset_id: A curated dataset id (`"usdm"`, `"edo-spaST"`,
                `"speibase-12"`, …).

        Returns:
            Dataset: The matching row.

        Raises:
            ValueError: When `dataset_id` is not curated; the message
                lists the closest known ids via the shipped
                `AbstractCatalog.get_dataset` did-you-mean helper.
        """
        return cast("Dataset", self.get_dataset(dataset_id))
