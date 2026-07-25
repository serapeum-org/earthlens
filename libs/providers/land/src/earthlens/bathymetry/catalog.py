"""DEM endpoint / transport catalog for the bathymetry backend.

The bathymetry backend fetches a handful of global topography / bathymetry
DEMs, each a static elevation grid subset on the server to a requested
bbox. The set is small and slow-changing, so it is curated as
config-as-code in the bundled `catalog/` directory — per-family `*.yaml`
files (`gebco.yaml`, `etopo.yaml`) plus an `_index.yaml` carrying the
informational `available_datasets:` list — and validated here against typed
pydantic rows. The loader merges every file at construction time (the
ghsl / cmems sharded pattern) through a `(path, mtime_ns)` parse cache.

Every shipped row uses the single `erddap-griddap` transport pinned in the
A1 gate (`planning/bathymetry/captures/bathymetry-sdk-facts.md`): a NOAA
ERDDAP `griddap` coverage subset by bbox to a NetCDF the backend reads with
pyramids and writes to GeoTIFF. The `transport` field still admits the
`gebco-api` / `opendap` values so a future row can carry a different
endpoint without a model change, but none ships today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-family `*.yaml` files plus the
#: `_index.yaml` informational index. Tests can monkey-patch this attribute to
#: redirect the loader at a temporary directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-family file invalidates the entry without re-parsing an unchanged tree.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset]]] = CatalogParseCache()

#: Transport mechanisms a catalog row may declare. Only `erddap-griddap`
#: ships today; the other two are reserved for a future GEBCO-API / OPeNDAP
#: row (see the module docstring).
Transport = Literal["erddap-griddap", "gebco-api", "opendap"]

#: Longitude conventions an ERDDAP DEM may serve its grid in.
LonConvention = Literal["-180..180", "0..360"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys include
    every contributing file's `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One curated DEM row — an endpoint, a coverage id, and a band.

    Attributes:
        id: Catalog key for the row (`"gebco_2020"`, `"etopo1_ice"`). Set
            from the catalog key by the loader.
        title: Human-readable one-line description.
        transport: How the grid is fetched. Every shipped row is
            `"erddap-griddap"`; `"gebco-api"` / `"opendap"` are reserved.
        endpoint: Base URL of the server (an ERDDAP base, no trailing
            `/griddap`). A trailing slash is tolerated by the URL builder.
        dataset_id: The coverage / dataset id on that server
            (`"GEBCO_2020"`, `"etopo1_ice"`).
        variable: The single elevation band name in the grid (`"elevation"`
            for GEBCO, `"z"` for ETOPO1).
        native_resolution: Human-readable native cell size
            (`"15 arc-second"`, `"1 arc-minute"`).
        lon_convention: Whether the server's longitude axis runs
            `"-180..180"` or `"0..360"`. The URL builder normalises the
            request bbox to this.
        license_note: Attribution / licence text surfaced in docs and logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    title: str = ""
    transport: Transport = "erddap-griddap"
    endpoint: str
    dataset_id: str
    variable: str
    native_resolution: str = ""
    lon_convention: LonConvention = "-180..180"
    license_note: str = ""


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files that contribute to a catalog load.

    Args:
        path: A catalog directory of per-family `*.yaml` files (the default
            layout, including `_index.yaml`) or a single `*.yaml` file
            (back-compat for tests / a monolithic catalog).

    Returns:
        list[Path]: Sorted YAML paths — every `*.yaml` for a directory, or
            just the one file.

    Raises:
        ValueError: If `path` is neither an existing directory nor file.
    """
    if path.is_dir():
        return sorted(path.glob("*.yaml"))
    if path.is_file():
        return [path]
    raise ValueError(
        f"bathymetry catalog path {path} does not exist (expected a directory "
        "of per-family *.yaml files, or a single YAML file)."
    )


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Dataset]]:
    """Parse, validate, and cache the bathymetry catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (an id declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`, so a second `Catalog()` on an unchanged tree skips both
    YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Dataset]]: The merged `available_datasets:`
            index and the curated dataset map (keyed by id).

    Raises:
        ValueError: If no file has a `datasets:` block, an id is declared in
            two files, a row fails validation, or a curated id is absent from
            `available_datasets:`.
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
        for code, body in (data.get("datasets") or {}).items():
            if code in merged_datasets_yaml:
                raise ValueError(
                    f"dataset {code!r} declared in two catalog files: "
                    f"{origin[code]} and {file_path}"
                )
            merged_datasets_yaml[code] = body
            origin[code] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The bathymetry catalog must list at least one DEM."
        )

    available = set(merged_available)
    datasets: dict[str, Dataset] = {}
    for code, body in merged_datasets_yaml.items():
        try:
            datasets[code] = Dataset(id=code, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[code]} dataset {code!r} failed validation:\n{exc}"
            ) from exc
        if available and code not in available:
            raise ValueError(
                f"dataset {code!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[code]}); add it to "
                "_index.yaml too."
            )

    _CATALOG_CACHE[key] = (merged_available, datasets)
    return _CATALOG_CACHE[key]


class Catalog(AbstractCatalog):
    """DEM endpoint / transport catalog for the bathymetry backend.

    Merges the bundled `catalog/` directory's per-family `*.yaml` files and
    exposes their `datasets:` blocks as a map of `Dataset` rows keyed by id
    under the inherited `datasets` field (giving `cat["gebco_2020"]`,
    `"gebco_2020" in cat`, `len(cat)`, and the did-you-mean error for free).
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads and
    validates the catalog through the parse cache.

    Attributes:
        datasets: Map from DEM id to its `Dataset` row.
        available_datasets: Every DEM id from `_index.yaml`. For bathymetry
            the curated set is the full shipped surface, so this equals the
            curated keys.
    """

    _catalog_kind: str = "bathymetry DEM catalog"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    _alias_index: dict[str, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args reads `CATALOG_PATH` (through the
        `(path, mtime_ns)`-keyed parse cache); passing `datasets=...` skips
        the disk read (used in tests).

        Raises:
            ValueError: Propagated from `load` when the catalog is missing,
                empty, or has a malformed row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.available_datasets = loaded.available_datasets
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the DEM catalog from disk (directory or single file).

        Args:
            catalog_path: Catalog directory or single YAML file. Defaults to
                the module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If no file has a `datasets:` block, an id is declared
                in two files, a row fails `Dataset` validation, or a curated
                id is absent from `available_datasets:`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, datasets = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(datasets),
            available_datasets=list(available),
        )

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the dataset map (satisfies the abstract contract)."""
        return self.datasets

    def get(self, dataset_id: str) -> Dataset:
        """Return the `Dataset` for a curated id, did-you-mean on miss.

        Args:
            dataset_id: A curated DEM id (`"gebco_2020"`, `"etopo1_ice"`).

        Returns:
            Dataset: The matching row.

        Raises:
            ValueError: If `dataset_id` is not a curated DEM; the message
                lists the known ids with a did-you-mean hint.

        Examples:
            - A known id resolves to its row:
                ```python
                >>> from earthlens.bathymetry import Catalog
                >>> Catalog().get("etopo1_ice").transport
                'erddap-griddap'
                >>> Catalog().get("gebco_2020").variable
                'elevation'

                ```
        """
        return cast("Dataset", self.get_dataset(dataset_id))
