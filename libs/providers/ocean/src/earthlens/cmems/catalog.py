"""Variable-catalog loader for the Copernicus Marine backend.

Hosts `Catalog`, the pydantic-backed reader for the bundled
CMEMS catalog. Mirrors the shape of `earthlens.gee.catalog`: the
catalog ships as a directory of per-domain YAML files at
`src/earthlens/cmems/catalog/` (`global.yaml`, `mediterranean.yaml`,
`black-sea.yaml`, `arctic.yaml`, …) plus a single `_index.yaml`
carrying the merged `available_datasets:` list. Each per-domain file
contributes its `datasets:` block; the loader unions them into one
`Catalog` at construction time, the same way
`earthlens.gee.catalog` merges its per-category files.

A `(dataset_id, variable_short_name)` pair resolves to a
`Variable` via `Catalog.get_variable`, and the full
`Dataset` shape (cadence, temporal coverage, domain) is
available via `AbstractCatalog.get_dataset` /
`Catalog()["..."]`.

`available_datasets:` is the informational index of every dataset id
the toolbox publishes (~1,251 today, the full
`copernicusmarine.describe()` walk); the curated `datasets:` map is a
subset of it (those that carry downloadable variables). Every curated
dataset id must appear in `available_datasets:` — the loader enforces
this. Users who need a dataset outside the curated list can still
call `CMEMS.download` with the id directly; the catalog lookup
is a metadata convenience, not a gate.

The path to the bundled catalog directory lives at
`CATALOG_PATH`; tests can monkey-patch that module attribute to
redirect the loader at a temporary directory or single YAML file.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from earthlens.base import AbstractCatalog, FluxableLeaf, SummarisedLeaf
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"

# Module-level cache of parsed catalog data, keyed on the resolved
# path plus a tuple of `(file, mtime_ns)` for every YAML the load
# touched, so editing any per-domain file invalidates the entry
# without inspecting every row. Mirrors the GEE multi-file pattern.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Dataset]]] = CatalogParseCache()

CadenceLiteral = Literal[
    "hourly",
    "6hourly",
    "daily",
    "weekly",
    "monthly",
    "annual",
    "climatology",
    "irregular",
]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to
    force a re-parse. Production callers do not need this — the
    cache keys include every contributing file's `st_mtime_ns`, so
    any real file mutation invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='CMEMS', shard_noun='per-domain')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Dataset]]:
    """Parse, validate, and cache the CMEMS catalog at `path`.

    Returns a `(available_datasets, datasets)` tuple of the same
    shape `Catalog` exposes. When `path` is a directory, every
    `*.yaml` file is merged: `available_datasets:` lists are
    concatenated and `datasets:` maps are unioned (a dataset id
    declared in two files is an error). Cached on the resolved path
    plus every contributing file's `mtime_ns`, so a second
    `Catalog()` on an unchanged tree skips both YAML parsing and
    pydantic validation.

    Args:
        path: Catalog directory (default
            `src/earthlens/cmems/catalog/`) or a single `*.yaml`
            file.

    Returns:
        Tuple of `(list[str], dict[str, Dataset])` — the merged
            `available_datasets:` index and the curated `datasets:`
            map.

    Raises:
        ValueError: If no file has a `datasets:` block, a dataset is
            declared in two files, a dataset has no `variables:`, a
            variable fails validation, or a curated dataset id is
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
        for ds_id, ds_body in (data.get("datasets") or {}).items():
            if ds_id in merged_datasets_yaml:
                raise ValueError(
                    f"dataset {ds_id!r} declared in two catalog files: "
                    f"{origin[ds_id]} and {file_path}"
                )
            merged_datasets_yaml[ds_id] = ds_body
            origin[ds_id] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one dataset with one "
            "variable."
        )

    available = set(merged_available)
    structural: dict[str, Dataset] = {}
    total_vars = 0
    for ds_id, ds_body in merged_datasets_yaml.items():
        variables_yaml = (ds_body or {}).get("variables") or {}
        if not variables_yaml:
            raise ValueError(
                f"{origin[ds_id]} dataset {ds_id!r} has no `variables:`. "
                "Every curated dataset must list at least one variable."
            )
        ds_vars: dict[str, Variable] = {}
        for var_name, var_body in variables_yaml.items():
            payload = dict(var_body or {})
            try:
                payload.setdefault("name", var_name)
                ds_vars[var_name] = Variable(**payload)
            except ValidationError as exc:
                raise ValueError(
                    f"{origin[ds_id]} dataset {ds_id!r} variable "
                    f"{var_name!r} failed validation:\n{exc}"
                ) from exc
            total_vars += 1
        temporal_body = ds_body.get("temporal") or {}
        try:
            structural[ds_id] = Dataset(
                product=ds_body.get("product", ""),
                title=ds_body.get("title", ""),
                cadence=ds_body.get("cadence", "irregular"),
                domain=ds_body.get("domain", "global"),
                temporal=TemporalCoverage(
                    start=temporal_body.get("start"),
                    end=temporal_body.get("end"),
                ),
                variables=ds_vars,
            )
        except ValidationError as exc:
            raise ValueError(
                f"{origin[ds_id]} dataset {ds_id!r} failed validation:\n{exc}"
            ) from exc
        if available and ds_id not in available:
            raise ValueError(
                f"dataset {ds_id!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[ds_id]}); add it to "
                "_index.yaml too."
            )

    if total_vars == 0:
        raise ValueError(
            f"{path} has no variables under any dataset. "
            "The catalog must contain at least one variable."
        )

    _CATALOG_CACHE[key] = (merged_available, structural)
    return _CATALOG_CACHE[key]


class Variable(FluxableLeaf):
    """One CMEMS variable's metadata row.

    Carries the minimum fields the backend needs to label the
    output file, plus the flux/state marker the aggregator reads
    when `op="auto"`. Source-of-truth for units / long-names lives
    in the toolbox's `describe()` output; the curated rows here
    mirror what `describe()` would report for the same
    `(dataset_id, variable)` pair. Inherits `types` + `is_flux`
    from `earthlens.base.FluxableLeaf`, the same shared base
    CHIRPS and ECMWF variable rows use.

    Attributes:
        units: CF-style unit string (e.g. `"degrees_C"`, `"m s-1"`,
            `"mmol m-3"`). Echoed in logs and surfaced via
            `var_info.units` for users who want to label downstream
            plots without re-reading the NetCDF metadata.
        long_name: CF long-name of the variable (e.g. `"Sea water
            potential temperature"`). Mainly for human-readable
            logging.
        name: The variable's catalog key, injected by the loader. Many
            rows carry no `long_name`, so without this a summary would
            identify itself by its unit alone.
        types: Optional `"flux"` or `"state"` marker (inherited).
            Currently advisory only — CMEMS variables are
            overwhelmingly state (instantaneous fields); the marker
            is provided so future flux-style aggregation can route
            to `"sum"` instead of `"mean"` when `op="auto"`.

    Examples:
        - Build a row and read its labels:

            ```python
            >>> from earthlens.cmems import Variable
            >>> v = Variable(units="degrees_C", long_name="Sea water potential temperature")
            >>> v.units
            'degrees_C'
            >>> v.long_name
            'Sea water potential temperature'
            >>> v.is_flux
            False

            ```
        - Mark a variable as a flux quantity:

            ```python
            >>> from earthlens.cmems import Variable
            >>> v = Variable(units="kg m-2 s-1", types="flux")
            >>> v.is_flux
            True

            ```
    """

    _summary_fields = (
        "name",
        "long_name",
        "units",
    )

    name: str = ""
    units: str
    long_name: str = ""


class TemporalCoverage(SummarisedLeaf):
    """Temporal coverage of a CMEMS dataset (start + optional end).

    Mirrors the `temporal:` block in the YAML. `end: null` (or a
    missing `end`) means the dataset is near-real-time / rolling.

    Attributes:
        start: First date for which data is available, as a string
            in `YYYY-MM-DD` form. May be `None` for catalogues whose
            start date is not pinned in the YAML.
        end: Last date with data, or `None` for an NRT product.

    Examples:
        - Bounded coverage:

            ```python
            >>> from earthlens.cmems.catalog import TemporalCoverage
            >>> tc = TemporalCoverage(start="1993-01-01", end="2023-12-31")
            >>> tc.start, tc.end
            ('1993-01-01', '2023-12-31')

            ```
        - NRT (rolling end):

            ```python
            >>> from earthlens.cmems.catalog import TemporalCoverage
            >>> tc = TemporalCoverage(start="2007-01-01")
            >>> tc.end is None
            True

            ```
        - The summary reads as a range, and says so when neither date is
          pinned:

            ```python
            >>> from earthlens.cmems.catalog import TemporalCoverage
            >>> print(TemporalCoverage(start="2007-01-01"))
            TemporalCoverage(2007-01-01 to present)
            >>> print(TemporalCoverage())
            TemporalCoverage(dates unknown)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str | None = None
    end: str | None = None

    def summary_parts(self) -> list[str]:
        """Render the coverage as one date range rather than two bare dates.

        Both fields are unset on about half the shipped rows, which as two
        independent fragments would summarise to nothing at all. A range says
        what the row means: an open end is the rolling NRT case, and no dates
        at all is a catalogue that never pinned them.

        Returns:
            list[str]: A single fragment naming the range.
        """
        if self.start is None and self.end is None:
            return ["dates unknown"]
        return [f"{self.start or 'start unknown'} to {self.end or 'present'}"]

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        """Accept a `datetime.date` (PyYAML's native parse result) as ISO string.

        Args:
            value: Raw YAML value — either a string, a `datetime.date`,
                or `None`.

        Returns:
            An ISO-format string (`"YYYY-MM-DD"`) or `None`.
        """
        if isinstance(value, _dt.date):
            return value.isoformat()
        return value


class Dataset(SummarisedLeaf):
    """One curated CMEMS dataset row.

    Mirrors a single `datasets.<dataset_id>:` block in one of the
    per-domain `catalog/*.yaml` files. The dataset id itself is the
    parent key in `Catalog.datasets` and is not stored on the
    row.

    Attributes:
        product: Parent CMEMS product id (e.g.
            `"GLOBAL_MULTIYEAR_PHY_001_030"`). Mostly for human
            reference — the toolbox addresses datasets by their
            `dataset_id`, not by product id.
        title: Human-readable description used in log messages.
        cadence: Native temporal cadence of the dataset (`"daily"`,
            `"monthly"`, `"hourly"`, …). Advisory; CMEMS handles
            cadence server-side and accepts any sub-window of the
            data's actual cadence.
        domain: Spatial domain (`"global"`, `"mediterranean"`,
            `"black-sea"`, `"baltic-sea"`, `"arctic"`, `"ibi"`,
            `"polar"`). Mainly for catalogue browsing.
        temporal: `TemporalCoverage` — start / end dates of
            the dataset.
        variables: Per-variable map keyed by the short variable
            name as it appears inside the NetCDF (`"thetao"`,
            `"so"`, `"chl"`, …).

    Examples:
        - Inspect a curated dataset row:

            ```python
            >>> from earthlens.cmems import Catalog
            >>> ds = Catalog().get_dataset("cmems_mod_glo_phy_my_0.083deg_P1D-m")
            >>> ds.cadence
            'daily'
            >>> ds.domain
            'global'
            >>> "thetao" in ds.variables
            True

            ```
    """

    _summary_fields = (
        "product",
        "title",
        "cadence",
    )

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str = ""
    title: str = ""
    cadence: CadenceLiteral = "irregular"
    domain: str = "global"
    temporal: TemporalCoverage = Field(default_factory=TemporalCoverage)
    variables: dict[str, Variable] = Field(default_factory=dict)


class Catalog(AbstractCatalog[Dataset]):
    """Variable catalog for the Copernicus Marine backend.

    Reads the bundled `catalog/` directory (shipped as package data)
    and exposes its consumed top-level sections as typed pydantic
    fields. Instantiate with no arguments (`Catalog()`) —
    `model_post_init` parses the YAML and populates every
    field in one pass.

    Variables are addressed by the `(dataset_id, variable_name)`
    pair via `get_variable`. The same short name (e.g.
    `"thetao"`, `"so"`) appears across multiple datasets so the
    dataset id is part of the identity.

    Attributes:
        available_datasets: Informational list of every CMEMS dataset
            id the toolbox publishes (the full
            `copernicusmarine.describe()` index, including ids that
            carry no downloadable variables). Mirrors the
            `available_datasets:` block in `catalog/_index.yaml`;
            every curated `datasets:` key is a member of this list.
            Runtime code does not consume it.
        datasets: Structural map keyed by CMEMS dataset id. Each
            value is a `Dataset` carrying that dataset's
            cadence / domain / temporal coverage / variables map.

    Examples:
        - Look up a variable:

            ```python
            >>> from earthlens.cmems import Catalog
            >>> v = Catalog().get_variable(
            ...     "cmems_mod_glo_phy_my_0.083deg_P1D-m", "thetao"
            ... )
            >>> v.units
            'degrees_C'

            ```
        - Iterate curated datasets:

            ```python
            >>> from earthlens.cmems import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.datasets)[:3]
            ['C3S-GLO-SST-L4-REP-OBS-SST', 'CERSAT-GLO-SEAICE_30DAYS_DRIFT_ASCAT_SSMI_MERGED_RAN-OBS_FULL_TIME_SERIE', 'CERSAT-GLO-SEAICE_30DAYS_DRIFT_QUICKSCAT_SSMI_MERGED_RAN-OBS_FULL_TIME_SERIE']

            ```
    """

    _catalog_kind: str = "CMEMS catalog"

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
        """Read the CMEMS catalog from disk (cached).

        Args:
            catalog_path: Path to the `catalog/` directory or a
                single `*.yaml` file. Defaults to module-level
                `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: Propagated from `_load_catalog_data`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available_datasets, datasets = _load_catalog_data(catalog_path)
        return cls(
            available_datasets=list(available_datasets),
            datasets=dict(datasets),
        )

    def get_variable(self, dataset_id: str, variable_name: str) -> Variable:
        """Return the `Variable` for a `(dataset_id, name)` pair.

        Args:
            dataset_id: CMEMS dataset id as it appears as a key in
                `datasets` (e.g.
                `"cmems_mod_glo_phy_my_0.083deg_P1D-m"`).
            variable_name: Short variable name as it appears inside
                the NetCDF (e.g. `"thetao"`, `"so"`).

        Returns:
            Variable: Per-variable metadata.

        Raises:
            KeyError: If `dataset_id` is not curated, or if
                `variable_name` is not declared under that dataset.

        Examples:
            - Look up a curated variable:

                ```python
                >>> from earthlens.cmems import Catalog
                >>> v = Catalog().get_variable(
                ...     "cmems_mod_glo_phy_my_0.083deg_P1D-m", "thetao"
                ... )
                >>> v.units
                'degrees_C'

                ```
            - Unknown dataset raises `KeyError`:

                ```python
                >>> from earthlens.cmems import Catalog
                >>> Catalog().get_variable("not-a-dataset", "thetao")
                Traceback (most recent call last):
                    ...
                KeyError: 'not-a-dataset'

                ```
        """
        return self.datasets[dataset_id].variables[variable_name]
