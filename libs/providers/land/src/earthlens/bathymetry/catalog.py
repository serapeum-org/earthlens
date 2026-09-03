"""DEM endpoint / transport catalog for the bathymetry backend.

The bathymetry backend fetches a handful of global topography / bathymetry
DEMs, each a static elevation grid subset on the server to a requested
bbox. The set is small and slow-changing, so it is curated as
config-as-code in the bundled `catalog/` directory — per-family `*.yaml`
files (`gebco.yaml`, `etopo.yaml`) plus an `_index.yaml` carrying the
informational `available_datasets:` list — and validated here against typed
pydantic rows. The loader merges every file at construction time (the
ghsl / cmems sharded pattern) through a `(path, mtime_ns)` parse cache.

Two transports ship today, both pinned live in an A1 gate:

* `erddap-griddap` (GEBCO / ETOPO — the A1 gate captures): a NOAA
  ERDDAP `griddap` coverage subset by bbox to a NetCDF the backend reads with
  pyramids and writes to GeoTIFF.
* `wcs` (EMODnet Bathymetry — the A1 gate captures): an OGC WCS
  coverage read over pyramids `Dataset.from_wcs` and cropped to the AOI.

The `transport` field also admits the reserved `gebco-api` / `opendap` values
so a future row can carry a different endpoint without a model change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_validator,
)

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-family `*.yaml` files plus the
#: `_index.yaml` informational index. Tests can monkey-patch this attribute to
#: redirect the loader at a temporary directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-family file invalidates the entry without re-parsing an unchanged tree.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset]]] = CatalogParseCache()

#: Transport mechanisms a catalog row may declare. `erddap-griddap` (GEBCO /
#: ETOPO) and `wcs` (EMODnet Bathymetry) ship today; `gebco-api` / `opendap`
#: are reserved for a future row (see the module docstring).
Transport = Literal["erddap-griddap", "wcs", "gebco-api", "opendap"]

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
        id: Catalog key for the row (`"gebco_2020"`, `"etopo1_ice"`,
            `"emodnet"`). Set from the catalog key by the loader.
        title: Human-readable one-line description.
        transport: How the grid is fetched — `"erddap-griddap"` (GEBCO /
            ETOPO) or `"wcs"` (EMODnet Bathymetry); `"gebco-api"` /
            `"opendap"` are reserved.
        endpoint: Base URL of the server — an ERDDAP base (no trailing
            `/griddap`) for `erddap-griddap`, or the OGC WCS endpoint for
            `wcs`. A trailing slash is tolerated.
        dataset_id: The coverage / dataset id on that server
            (`"GEBCO_2020"`, `"etopo1_ice"`, or the WCS coverage
            `"emodnet:mean"`).
        variable: The single elevation band name in the grid (`"elevation"`
            for GEBCO / EMODnet, `"z"` for ETOPO1).
        native_resolution: Human-readable native cell size
            (`"15 arc-second"`, `"1 arc-minute"`, `"3.75 arc-second"`).
        lon_convention: Whether the server's longitude axis runs
            `"-180..180"` or `"0..360"`. The griddap URL builder normalises
            the request bbox to this.
        wcs_version: The OGC WCS protocol version pyramids `from_wcs` must
            negotiate (`"1.0.0"` for EMODnet — its GeoServer only subsets
            cleanly at 1.0.0). Empty for non-`wcs` rows.
        crs: The request / native CRS for a `wcs` row (`"EPSG:4326"`).
            Ignored by the griddap path.
        native_bbox: The coverage's advertised extent as
            `(west, south, east, north)` in `crs`, used by the `wcs` branch
            to guard out-of-domain requests (the WCS server returns an
            all-zeros grid, not an error, outside coverage). `None` for
            griddap rows (ERDDAP rejects an out-of-coverage bbox itself).
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
    wcs_version: str = ""
    crs: str = "EPSG:4326"
    native_bbox: tuple[float, float, float, float] | None = None
    license_note: str = ""

    @model_validator(mode="after")
    def _check_wcs_fields(self) -> Dataset:
        """Require a `wcs` row to carry the fields its transport needs.

        A `transport: wcs` row is read through pyramids `from_wcs`, which
        needs the WCS protocol version and (for the out-of-domain guard) the
        coverage's advertised extent. Enforce both here — present, and not
        degenerate (a blank version, or a zero/negative-area `native_bbox`) —
        so a malformed row fails at catalog load, not mid-download.

        Returns:
            Dataset: The validated row (unchanged).

        Raises:
            ValueError: If a `wcs` row is missing / blank `wcs_version` or
                `native_bbox`, or `native_bbox` has non-positive area.
        """
        if self.transport != "wcs":
            return self
        missing = [
            name
            for name, value in (
                ("wcs_version", (self.wcs_version or "").strip()),
                ("native_bbox", self.native_bbox),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"wcs row {self.id or self.dataset_id!r} is missing "
                f"required field(s): {', '.join(missing)}."
            )
        bbox = self.native_bbox
        if bbox is not None and (bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ValueError(
                f"wcs row {self.id or self.dataset_id!r} has a degenerate "
                f"native_bbox {bbox}; expected west < east and south < north."
            )
        return self


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='bathymetry', shard_noun='per-family')


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


class Catalog(AbstractCatalog[Dataset]):
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

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `available_datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "available_datasets": loaded.available_datasets,
        }

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
