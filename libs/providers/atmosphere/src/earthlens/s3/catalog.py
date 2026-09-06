"""Dataset registry for the AWS Open-Data S3 backend.

Hosts the pydantic models (`Variable`, `Dataset`) and the `Catalog`
loader for the bundled `s3_data_catalog.yaml`. The catalog is the single
place every per-provider difference lives: each registered dataset
(`era5`, `sentinel-2-l2a`, `goes`, `copernicus-dem`, `esa-worldcover`)
carries its bucket, on-disk format, CRS, key-layout family, and a map of
friendly variable/band names to the native tokens used to build S3 keys.

`Catalog.resolve` is the entry point the backend calls. It accepts either
a **registered dataset name** (`resolve("era5")`) or an **inline spec
dict** (`resolve({"bucket": ..., "format": "cog", ...})`) — the
passthrough path — and returns a validated `Dataset` either way, so a
registered dataset and an ad-hoc public bucket are handled by identical
downstream code.

`CATALOG_PATH` is the path to the bundled YAML; the loader caches parsed
data keyed on `(path, mtime_ns)` so repeated `Catalog()` construction is
cheap and a file edit invalidates the entry naturally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

__all__ = ["Catalog", "Dataset", "Variable", "CATALOG_PATH"]

CATALOG_PATH: Path = Path(__file__).parent / "s3_data_catalog.yaml"

#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation. Stores the
#: `(datasets, available_datasets)` pair. Mirrors the Overture / FDSN loaders.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class Variable(BaseModel):
    """One selectable variable / band of a dataset.

    The friendly key (`"t2m"`, `"B04"`, `"elevation"`) is the map key in
    `Dataset.variables` and is not stored on the row. A request resolves a
    user-supplied token (the friendly key, any alias, or the raw `native`
    token) to one of these rows.

    Attributes:
        native: The token used to build the S3 key for this variable — an
            ERA5 `table_param_short` code (`128_167_2t`), a Sentinel-2
            band file stem (`B04`), a GOES channel code (`C02`), or a
            tile-file suffix (`DEM`, `Map`).
        aliases: Alternate names that resolve to this variable (CF names,
            ECMWF short names, common synonyms).
        units: Physical units, for docs and the catalog reference.
        description: Short human-readable note.
        stream: ERA5-only — the NCAR product stream the variable lives in
            (`e5.oper.an.sfc`, `e5.oper.fc.sfc.accumu`, ...). `None` for
            datasets without streams.
        nc_variable: In-file variable name inside the NetCDF granule, used
            by `aggregate=` to pick the variable. `None` falls back to
            `native`.

    Examples:
        - Build a row and read its native token:
            ```python
            >>> from earthlens.s3.catalog import Variable
            >>> Variable(native="B04", aliases=["red"]).native
            'B04'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    native: str
    aliases: list[str] = Field(default_factory=list)
    units: str = ""
    description: str = ""
    stream: str | None = None
    nc_variable: str | None = None


class Dataset(BaseModel):
    """One registered (or passthrough) AWS Open-Data S3 dataset.

    Carries everything the backend needs to turn a uniform request into S3
    keys and a cropped local file: the bucket, the on-disk `format`, the
    `crs` (an EPSG int, or `None` when per-tile/dynamic such as Sentinel-2
    UTM or GOES geostationary), the key-`layout` family, and the
    `variables` map. `params` holds layout-specific configuration the
    resolver reads (ERA5 default stream, Sentinel-2 collection prefix,
    GOES product/satellite map, tile-grid degrees, epoch/resolution maps).

    Attributes:
        bucket: The S3 bucket name.
        format: `"netcdf"` or `"cog"`.
        layout: `"prefix_listing"` (list a computed prefix, match tokens)
            or `"deterministic_tiles"` (compute tile names from the bbox).
        crs: EPSG code of the dataset's native CRS, or `None` when the CRS
            is per-file (reprojected to the AOI on crop).
        temporal: `"temporal"` (has a time axis) or `"static"`.
        cadence: Informational granule cadence (`"monthly"`, `"scene"`,
            `"static"`).
        lon_convention: `"0-360"` when longitudes need wrapping to
            `-180..180` before a bbox crop (ERA5); `None` otherwise.
        region: AWS region of the bucket (e.g. `"us-west-2"`); `None`
            uses the client default. Required for some buckets.
        requester_pays: `True` for requester-pays buckets (e.g.
            `usgs-landsat`, `naip-source`) — the backend then signs the
            client and passes `RequestPayer="requester"`, which bills the
            caller's AWS account. Requires valid AWS credentials.
        description: Human-readable summary.
        params: Layout-specific configuration consumed by `layouts.py`.
        default_variables: Variables fetched when the caller passes none.
        variables: Map of friendly name to `Variable` row.

    Examples:
        - Resolve a friendly name, an alias, and a raw token to one row:
            ```python
            >>> from earthlens.s3 import Catalog
            >>> ds = Catalog().get_dataset("era5")
            >>> ds.resolve_variable("t2m").native
            '128_167_2t'
            >>> ds.resolve_variable("2m_temperature").native
            '128_167_2t'
            >>> ds.resolve_variable("128_167_2t").native
            '128_167_2t'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bucket: str
    format: Literal["netcdf", "cog"]
    layout: Literal["prefix_listing", "deterministic_tiles"]
    crs: int | None = None
    temporal: Literal["temporal", "static"] = "temporal"
    cadence: str | None = None
    lon_convention: str | None = None
    region: str | None = None
    requester_pays: bool = False
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    default_variables: list[str] = Field(default_factory=list)
    variables: dict[str, Variable] = Field(default_factory=dict)

    def resolve_variable(self, key: str) -> Variable:
        """Resolve a friendly key, alias, or raw native token to its `Variable`.

        Args:
            key: The variable token the caller supplied — a friendly name
                (`"t2m"`), an alias (`"2m_temperature"`), or the raw
                `native` token (`"128_167_2t"`).

        Returns:
            The matching `Variable`.

        Raises:
            ValueError: If `key` matches no variable; the message lists the
                known variables and a did-you-mean hint.
        """
        name = self._match_variable(key)
        if name is None:
            import difflib

            pool = list(self.variables) + [
                a for v in self.variables.values() for a in v.aliases
            ]
            close = difflib.get_close_matches(key, pool, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not a variable of this dataset. "
                f"Known variables: {sorted(self.variables)}.{hint}"
            )
        return self.variables[name]

    def resolve_variables(self, keys: list[str] | None) -> list[Variable]:
        """Resolve a list of variable tokens, defaulting to `default_variables`.

        Args:
            keys: The variable tokens the caller supplied. `None` or an
                empty list resolves to `default_variables` (and, failing
                that, every variable).

        Returns:
            The resolved `Variable` rows, de-duplicated in first-seen
            order by native token.

        Examples:
            - Resolve a list of friendly names / aliases to native tokens:
                ```python
                >>> from earthlens.s3 import Catalog
                >>> s2 = Catalog().get_dataset("sentinel-2-l2a")
                >>> [v.native for v in s2.resolve_variables(["red", "nir"])]
                ['B04', 'B08']

                ```
            - `None` falls back to the dataset defaults:
                ```python
                >>> from earthlens.s3 import Catalog
                >>> s2 = Catalog().get_dataset("sentinel-2-l2a")
                >>> [v.native for v in s2.resolve_variables(None)]
                ['B04', 'B03', 'B02']

                ```
        """
        if not keys:
            keys = self.default_variables or list(self.variables)
        # A passthrough dataset has no curated variable map; treat the
        # supplied tokens as raw native tokens.
        if not self.variables:
            seen_raw: dict[str, Variable] = {}
            for key in keys:
                seen_raw.setdefault(key, Variable(native=key))
            return list(seen_raw.values())
        seen: dict[str, Variable] = {}
        for key in keys:
            var = self.resolve_variable(key)
            seen.setdefault(var.native, var)
        return list(seen.values())

    def _match_variable(self, key: str) -> str | None:
        """Return the friendly name matching `key`, or `None`."""
        if key in self.variables:
            return key
        for name, var in self.variables.items():
            if key == var.native or key in var.aliases:
                return name
        return None


def _parse_s3_catalog(files: list[Path]):
    """Parse and validate the ERA5-S3 catalog rows.

    Args:
        files: The contributing YAML files (ERA5-S3 ships a single file).

    Returns:
        The validated rows, in the shape the catalog caches.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'datasets:' block. "
            "The S3 registry must list at least one dataset."
        )
    datasets = {}
    for name, body in datasets_yaml.items():
        try:
            datasets[name] = Dataset(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} dataset {name!r} failed validation:\n{exc}"
            ) from exc
    available = list(data.get("available_datasets") or [])
    return (datasets, available)


class Catalog(AbstractCatalog[Dataset]):
    """Registry of the AWS Open-Data S3 datasets the backend can fetch.

    Reads the bundled `s3_data_catalog.yaml` and exposes its `datasets:`
    block as a map of `Dataset` rows keyed by name (giving the dict-like
    `cat["era5"]` / `"goes" in cat` / did-you-mean surface for free).
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads
    and validates the YAML in one pass with no network call.

    Attributes:
        datasets: Map from registered dataset name to its `Dataset` row.
        available_datasets: Informational index of every reachable dataset
            name; rebuilt by `tools/s3/refresh_s3_catalog.py`.

    Examples:
        - List datasets and resolve one (registered name):
            ```python
            >>> from earthlens.s3 import Catalog
            >>> Catalog().dataset_names()
            ['copernicus-dem', 'era5', 'esa-worldcover', 'goes', 'naip-source', 'sentinel-2-l2a', 'usgs-landsat']
            >>> Catalog().resolve("era5").bucket
            'nsf-ncar-era5'

            ```
        - Resolve an inline passthrough spec (an unregistered bucket):
            ```python
            >>> from earthlens.s3 import Catalog
            >>> ds = Catalog().resolve(
            ...     {"bucket": "my-bucket", "format": "cog",
            ...      "layout": "deterministic_tiles"}
            ... )
            >>> ds.bucket
            'my-bucket'

            ```
        - An unknown dataset raises with a did-you-mean hint:
            ```python
            >>> from earthlens.s3 import Catalog
            >>> Catalog().resolve("era-5")
            Traceback (most recent call last):
                ...
            ValueError: 'era-5' is not in the S3 dataset registry. Known datasets: ['copernicus-dem', 'era5', 'esa-worldcover', 'goes', 'naip-source', 'sentinel-2-l2a', 'usgs-landsat']. Did you mean 'era5'?

            ```
    """

    _catalog_kind: str = "S3 dataset registry"
    _entry_noun: str = "datasets"

    datasets: dict[str, Dataset] = Field(default_factory=dict)

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
        """Read the dataset registry from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `datasets:` block, or a row fails `Dataset` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        datasets, available = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_s3_catalog, provider="ERA5-S3"
        )
        return cls(datasets=dict(datasets), available_datasets=list(available))

    def resolve(self, dataset: str | dict[str, Any] | Dataset) -> Dataset:
        """Resolve a dataset selector to a validated `Dataset`.

        The single entry point the backend calls. Accepts a registered
        dataset name, an inline spec dict (the passthrough path), or an
        already-built `Dataset` — returning a `Dataset` in every case so
        registered and ad-hoc datasets share identical downstream code.

        Args:
            dataset: A registered name (`"era5"`), an inline spec dict
                (`{"bucket": ..., "format": "cog", "layout": ...}`), or a
                `Dataset` instance.

        Returns:
            The resolved `Dataset`.

        Raises:
            ValueError: If `dataset` is an unknown name, or an inline spec
                that fails `Dataset` validation.
        """
        if isinstance(dataset, Dataset):
            return dataset
        if isinstance(dataset, dict):
            try:
                return Dataset(**dataset)
            except ValidationError as exc:
                raise ValueError(
                    f"inline dataset spec failed validation:\n{exc}"
                ) from exc
        return cast("Dataset", self.get_dataset(dataset))

    def dataset_names(self) -> list[str]:
        """Return the registered dataset names, sorted.

        Returns:
            The sorted registry names.

        Examples:
            - List every registered dataset:
                ```python
                >>> from earthlens.s3 import Catalog
                >>> Catalog().dataset_names()
                ['copernicus-dem', 'era5', 'esa-worldcover', 'goes', 'naip-source', 'sentinel-2-l2a', 'usgs-landsat']

                ```
        """
        return sorted(self.datasets)
