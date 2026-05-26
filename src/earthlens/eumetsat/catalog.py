"""Dataset-catalog loader for the EUMETSAT Data Store backend.

Hosts `Catalog`, the pydantic-backed reader for the bundled EUMETSAT
catalog. Mirrors the shape of `earthlens.earthdata.catalog` and
`earthlens.gee.catalog`: the catalog ships as a directory of per-group
YAML files at `src/earthlens/eumetsat/catalog/` (`mtg.yaml`, `msg.yaml`,
`metop.yaml`, `sentinel3.yaml`, `sentinel5p.yaml`, …) plus a single
`_index.yaml` carrying the merged `available_datasets:` list. Each
per-group file contributes its `datasets:` block; the loader unions them
into one `Catalog` at construction time.

A friendly dataset key (e.g. `"msg-hrseviri"`) resolves to an
`EumetsatDataset` via `Catalog.get_dataset` / `Catalog()["..."]` /
`Catalog.resolve`. The row maps the friendly key to the real EUMETSAT
`EO:EUM:DAT:…` collection id (the string `eumdac`'s `get_collection`
needs) and carries the fields the backend shapes a search and a fetch
from: `group`, `mission`, the per-instance `output_kind` (`G1`), the
on-disk `format` (so the `aggregate=` path knows which products are
pyramids-readable NetCDF vs native), the informational `selectors`
(`G2`), the `tailor_product_type` for the deferred Data Tailor `Chain`
(`H4`), and the spatial / temporal coverage.

`available_datasets:` is the informational index of every Data Store
collection id the browse walk found (the `C7` auto-generated index); the
curated `datasets:` map is the vetted subset (here, the whole catalog).
The path to the bundled catalog directory lives at `CATALOG_PATH`; tests
redirect the loader by pointing that module attribute at a temporary
directory or a single YAML file.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"

# Module-level cache of parsed catalog data, keyed on the resolved path
# plus a tuple of `(file, mtime_ns)` for every YAML the load touched, so
# editing any per-group file invalidates the entry without inspecting
# every row. Mirrors the Earthdata / CMEMS / GEE multi-file pattern.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, "EumetsatDataset"]]] = {}

OutputKindLiteral = Literal["raster", "vector", "tabular"]

CadenceLiteral = Literal[
    "5min",
    "10min",
    "15min",
    "30min",
    "hourly",
    "subdaily",
    "daily",
    "10day",
    "monthly",
    "annual",
    "static",
    "irregular",
]

#: Delivery timeliness of a Data Store collection. `nrt` = near-real-time
#: (rolling retention, low latency); `reprocessed` = a consolidated reprocessed
#: archive; `offline` = the standard offline (OFFL) stream. `None` when the
#: distinction does not apply (e.g. geostationary L1.5 imagery).
TimelinessLiteral = Literal["nrt", "reprocessed", "offline"]


class DataStoreGroup(str, Enum):
    """The EUMETSAT Data Store collection groups (mission families).

    Each value is the human-readable group label carried on a catalog
    row's `group:` field and accepted by the backend's `group=` kwarg to
    disambiguate a collection key shared across groups (`G2`).
    """

    MTG = "MTG"
    MSG = "MSG"
    MFG = "MFG"
    METOP = "Metop"
    METOP_SG = "Metop-SG"
    SENTINEL_3 = "Sentinel-3"
    SENTINEL_5P = "Sentinel-5P"
    SENTINEL_6 = "Sentinel-6"
    OSI_SAF = "OSI-SAF"
    OTHER = "Other"


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys
    include every contributing file's `st_mtime_ns`, so any real file
    mutation invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted list of YAML files that contribute to a load.

    `path` may point at either a directory of per-group `*.yaml` files
    (the default layout) or a single `*.yaml` file (back-compat for
    tests that redirect `CATALOG_PATH` to a temp file).

    Args:
        path: Catalog directory or single YAML file.

    Returns:
        Sorted list of YAML paths. For a directory, every `*.yaml`
            sibling (including `_index.yaml`); for a file, just that
            file.

    Raises:
        ValueError: If `path` is neither an existing directory nor an
            existing file.
    """
    if path.is_dir():
        return sorted(path.glob("*.yaml"))
    if path.is_file():
        return [path]
    raise ValueError(
        f"EUMETSAT catalog path {path} does not exist (expected a "
        "directory of per-group *.yaml files, or a single YAML file)."
    )


def _load_catalog_data(
    path: Path,
) -> tuple[list[str], dict[str, "EumetsatDataset"]]:
    """Parse, validate, and cache the EUMETSAT catalog at `path`.

    Returns an `(available_datasets, datasets)` tuple. When `path` is a
    directory, every `*.yaml` file is merged: `available_datasets:` lists
    are concatenated and `datasets:` maps are unioned (a key declared in
    two files is an error). Cached on the resolved path plus every
    contributing file's `mtime_ns`.

    Args:
        path: Catalog directory (default `src/earthlens/eumetsat/catalog/`)
            or a single `*.yaml` file.

    Returns:
        Tuple of `(list[str], dict[str, EumetsatDataset])` — the merged
            `available_datasets:` index and the curated `datasets:` map.

    Raises:
        ValueError: If no file has a `datasets:` block, a key is declared
            in two files, or a dataset fails validation.
    """
    resolved = str(path.resolve())
    files = _yaml_files_for(path)
    try:
        mtime_tuple = tuple((str(f), f.stat().st_mtime_ns) for f in files)
    except FileNotFoundError:
        mtime_tuple = ((resolved, 0),)
    key = (resolved, mtime_tuple)
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
                    f"dataset {ds_key!r} declared in two catalog files: "
                    f"{origin[ds_key]} and {file_path}"
                )
            merged_datasets_yaml[ds_key] = ds_body
            origin[ds_key] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one curated dataset."
        )

    structural: dict[str, EumetsatDataset] = {}
    for ds_key, ds_body in merged_datasets_yaml.items():
        body = dict(ds_body or {})
        extent_body = body.pop("extent", None) or {}
        temporal_body = body.pop("temporal", None) or {}
        try:
            structural[ds_key] = EumetsatDataset(
                extent=Extent(**extent_body),
                temporal=TemporalCoverage(
                    start=temporal_body.get("start"),
                    end=temporal_body.get("end"),
                ),
                **body,
            )
        except ValidationError as exc:
            raise ValueError(
                f"{origin[ds_key]} dataset {ds_key!r} failed " f"validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = (merged_available, structural)
    return _CATALOG_CACHE[key]


class Extent(BaseModel):
    """Spatial coverage of an EUMETSAT collection (lat / lon bounds).

    Mirrors the `extent:` block in the YAML. Whole-disk geostationary
    products (SEVIRI / FCI) use the ~±79° sub-satellite extent; polar /
    mirror products are global.

    Attributes:
        lat: `[lat_min, lat_max]` in degrees. Empty means unspecified.
        lon: `[lon_min, lon_max]` in degrees. Empty means unspecified.

    Examples:
        - A geostationary 0° disk extent:
            ```python
            >>> from earthlens.eumetsat.catalog import Extent
            >>> Extent(lat=[-79, 79], lon=[-79, 79]).lat
            [-79.0, 79.0]

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: list[float] = Field(default_factory=list)
    lon: list[float] = Field(default_factory=list)


class TemporalCoverage(BaseModel):
    """Temporal coverage of an EUMETSAT collection (start + optional end).

    Mirrors the `temporal:` block in the YAML. `end: null` (or a missing
    `end`) means the collection is ongoing / rolling.

    Attributes:
        start: First date with data, as a `YYYY-MM-DD` string. May be
            `None` when the start date is not pinned in the YAML.
        end: Last date with data, or `None` for an ongoing collection.

    Examples:
        - An ongoing collection:
            ```python
            >>> from earthlens.eumetsat.catalog import TemporalCoverage
            >>> tc = TemporalCoverage(start="2004-01-19", end=None)
            >>> tc.start, tc.end
            ('2004-01-19', None)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str | None = None
    end: str | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        """Accept a `datetime.date` (PyYAML's native parse) as ISO string.

        Args:
            value: Raw YAML value — a string, a `datetime.date`, or
                `None`.

        Returns:
            An ISO-format string (`"YYYY-MM-DD"`) or `None`.
        """
        if isinstance(value, _dt.date):
            return value.isoformat()
        return value


class EumetsatDataset(BaseModel):
    """One curated EUMETSAT Data Store dataset (collection) row.

    Mirrors a single `datasets.<key>:` block in one of the per-group
    `catalog/*.yaml` files. The friendly dataset key is the parent key
    in `Catalog.datasets` and is not stored on the row.

    Attributes:
        collection_id: The real Data Store collection id the search
            uses, e.g. `"EO:EUM:DAT:MSG:HRSEVIRI"`.
        group: The Data Store group (mission family) this dataset
            belongs to — used by the backend's `group=` disambiguation.
        mission: Short mission tag (`"msg"`, `"mtg"`, `"metop"`,
            `"sentinel-3"`, …). Advisory.
        output_kind: The per-dataset output shape — `"raster"`,
            `"vector"`, or `"tabular"`. Copied onto the backend
            instance's `OUTPUT_KIND` (`G1`).
        format: On-disk product format (`"native"`, `"netcdf"`,
            `"grib"`, `"bufr"`, …). `"native"` SEVIRI / FCI needs the
            satpy bridge (`PY-2`) to read; `"netcdf"` is pyramids-readable
            today (`G4`).
        cadence: Native temporal cadence (advisory).
        timeliness: Delivery timeliness of the dataset — `"nrt"`,
            `"reprocessed"`, or `"offline"` — or `None` when the
            distinction does not apply (e.g. geostationary L1.5 imagery).
            Recorded so a caller can tell a near-real-time stream from a
            reprocessed archive (the Sentinel-5P collections mix the two).
        selectors: Informational product-type / band selectors (`G2`).
            EUMETSAT delivers whole products, so selectors do not subset
            the download; they seed catalog metadata and the future Data
            Tailor `Chain`.
        tailor_product_type: The Data Tailor product-type id for the
            deferred server-side subset/reproject path (`H4`).
        extent: `Extent` — lat / lon coverage.
        temporal: `TemporalCoverage` — start / end dates.

    Examples:
        - Inspect a curated raster row:
            ```python
            >>> from earthlens.eumetsat import Catalog
            >>> ds = Catalog().get_dataset("msg-hrseviri")
            >>> ds.collection_id
            'EO:EUM:DAT:MSG:HRSEVIRI'
            >>> ds.output_kind
            'raster'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_id: str
    group: DataStoreGroup
    mission: str = ""
    output_kind: OutputKindLiteral = "raster"
    format: str = ""
    cadence: CadenceLiteral = "irregular"
    timeliness: TimelinessLiteral | None = None
    selectors: list[str] = Field(default_factory=list)
    tailor_product_type: str | None = None
    extent: Extent = Field(default_factory=Extent)
    temporal: TemporalCoverage = Field(default_factory=TemporalCoverage)


class Catalog(AbstractCatalog):
    """Dataset catalog for the EUMETSAT Data Store backend.

    Reads the bundled `catalog/` directory (shipped as package data) and
    exposes its consumed top-level sections as typed pydantic fields.
    Instantiate with no arguments (`Catalog()`) — `model_post_init`
    parses the YAML and populates every field in one pass. Mirrors the
    `earthlens.earthdata` / `earthlens.gee` / `earthlens.cmems` catalogs:
    `datasets` (the curated map) and `available_datasets` (the
    informational index).

    Attributes:
        available_datasets: Informational list of every Data Store
            collection id the browse walk found. Runtime code does not
            consume it.
        datasets: Structural map keyed by the curated dataset key. Each
            value is an `EumetsatDataset`.

    Examples:
        - Resolve a curated dataset:
            ```python
            >>> from earthlens.eumetsat import Catalog
            >>> "msg-hrseviri" in Catalog()
            True

            ```
    """

    _catalog_kind: str = "EUMETSAT catalog"

    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, EumetsatDataset] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args is sugar for `Catalog.load()` — it reads
        the bundled `catalog/` directory through the `(path, mtime)`-keyed
        cache so repeated construction is fast. If the caller passed
        `datasets=...`, the disk read is skipped.

        Raises:
            ValueError: When auto-loading, propagates the same errors as
                `load`.
        """
        if self.datasets:
            return
        loaded = Catalog.load()
        self.available_datasets = loaded.available_datasets
        self.datasets = loaded.datasets

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the EUMETSAT catalog from disk (cached).

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

    def get_catalog(self) -> dict[str, EumetsatDataset]:
        """Return the structural per-dataset map.

        Satisfies the abstract base's contract; the actual parsing is
        done in `model_post_init`.

        Returns:
            dict[str, EumetsatDataset]: One entry per curated dataset.
                Same object as `datasets`.
        """
        return self.datasets

    def resolve(
        self, key: str, group: DataStoreGroup | str | None = None
    ) -> EumetsatDataset:
        """Resolve a dataset key, optionally disambiguated by group.

        Most keys map one-to-one to a curated row; the inherited
        :meth:`get_dataset` (with its did-you-mean) handles those. The
        `group=` filter additionally asserts which Data Store group the
        resolved dataset belongs to (`G2`) — the EUMETSAT analog of the
        Earthdata backend's `daac=` filter.

        Args:
            key: Curated dataset key (a member of `datasets`).
            group: Optional `DataStoreGroup` (or its string value) the
                resolved row's `group` must match.

        Returns:
            EumetsatDataset: The resolved row.

        Raises:
            ValueError: When `key` is unknown (with a did-you-mean hint),
                or `group=` is given but does not match the row's group.

        Examples:
            - Resolve a key and read its group:
                ```python
                >>> from earthlens.eumetsat import Catalog
                >>> Catalog().resolve("msg-hrseviri").group.value
                'MSG'

                ```
        """
        dataset = self.get_dataset(key)
        if group is not None:
            wanted = group.value if isinstance(group, DataStoreGroup) else str(group)
            if dataset.group.value != wanted:
                raise ValueError(
                    f"dataset {key!r} is in group "
                    f"{dataset.group.value!r}, not the requested "
                    f"group={wanted!r}."
                )
        return dataset
