"""Variable-catalog loader for the Copernicus Marine backend.

Hosts :class:`Catalog`, the pydantic-backed reader for
`cmems_data_catalog.yaml`. Mirrors the shape of
:mod:`earthlens.ecmwf.catalog` so the two backends feel identical to
callers — a `(dataset_id, variable_short_name)` pair resolves to a
:class:`Variable` via :meth:`Catalog.get_variable`, and the full
:class:`Dataset` shape (cadence, temporal coverage, domain) is
available via :meth:`AbstractCatalog.get_dataset` /
``Catalog()["..."]``.

The catalog is intentionally **curated, not exhaustive**: CMEMS
hosts ~600 datasets across ~50 products and that index moves
weekly. The bundled YAML covers the highest-leverage marine
datasets (global physics + biogeochem, OSTIA SST, sea-level
altimetry, sea ice, four regional reanalyses). Users who need a
dataset outside the curated list can still call
:meth:`CMEMS.download` with the dataset id directly — the catalog
lookup is a metadata convenience, not a gate; the toolbox itself
is the source of truth.

The path to the bundled YAML lives at :data:`CATALOG_PATH`; tests
can monkey-patch that module attribute to redirect the loader at a
temporary file.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "cmems_data_catalog.yaml"

# Module-level cache of parsed catalog data, keyed on
# `(resolved_path, mtime_ns)` so any real file mutation invalidates
# the entry naturally. Mirrors the ECMWF / GEE pattern.
_CATALOG_CACHE: dict[tuple[str, int], tuple[list[str], dict[str, "Dataset"]]] = {}

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
    cache keys include `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, "Dataset"]]:
    """Parse, validate, and cache the CMEMS catalog at `path`.

    Returns a `(available_products, datasets)` tuple of the same
    shape :class:`Catalog` exposes. Cached on
    `(resolved-path, mtime_ns)` so a second `Catalog()` on an
    unchanged file skips both YAML parsing and pydantic validation.

    Raises:
        ValueError: If the YAML is missing, has no `datasets:` block,
            has no variables under any dataset, or declares the same
            key twice anywhere.
    """
    resolved = str(path.resolve())
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        mtime_ns = 0
    key = (resolved, mtime_ns)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    datasets_yaml = data.get("datasets")
    if not datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one dataset with one "
            "variable. See the schema header at the top of the file."
        )

    structural: dict[str, Dataset] = {}
    total_vars = 0
    for ds_id, ds_body in datasets_yaml.items():
        variables_yaml = (ds_body or {}).get("variables") or {}
        if not variables_yaml:
            raise ValueError(
                f"{path} dataset {ds_id!r} has no `variables:`. "
                "Every curated dataset must list at least one variable."
            )
        ds_vars: dict[str, Variable] = {}
        for var_name, var_body in variables_yaml.items():
            payload = dict(var_body or {})
            try:
                ds_vars[var_name] = Variable(**payload)
            except ValidationError as exc:
                raise ValueError(
                    f"{path} dataset {ds_id!r} variable {var_name!r} "
                    f"failed validation:\n{exc}"
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
                f"{path} dataset {ds_id!r} failed validation:\n{exc}"
            ) from exc

    if total_vars == 0:
        raise ValueError(
            f"{path} has no variables under any dataset. "
            "The catalog must contain at least one variable."
        )

    available = list(data.get("available_products") or [])
    _CATALOG_CACHE[key] = (available, structural)
    return _CATALOG_CACHE[key]


class Variable(BaseModel):
    """One CMEMS variable's metadata row.

    Carries the minimum fields the backend needs to label the
    output file, plus the flux/state marker the aggregator reads
    when `op="auto"`. Source-of-truth for units / long-names lives
    in the toolbox's `describe()` output; the curated rows here
    mirror what `describe()` would report for the same
    `(dataset_id, variable)` pair.

    Attributes:
        units: CF-style unit string (e.g. `"degrees_C"`, `"m s-1"`,
            `"mmol m-3"`). Echoed in logs and surfaced via
            `var_info.units` for users who want to label downstream
            plots without re-reading the NetCDF metadata.
        long_name: CF long-name of the variable (e.g. `"Sea water
            potential temperature"`). Mainly for human-readable
            logging.
        types: Optional `"flux"` or `"state"` marker. Currently
            advisory only — CMEMS variables are overwhelmingly
            state (instantaneous fields); the marker is provided so
            future flux-style aggregation can route to `"sum"`
            instead of `"mean"` when `op="auto"`.

    Examples:
        - Build a row directly:

            ```python
            >>> from earthlens.cmems import Variable
            >>> v = Variable(units="degrees_C", long_name="Sea water potential temperature")
            >>> v.units
            'degrees_C'
            >>> v.long_name
            'Sea water potential temperature'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str
    long_name: str = ""
    types: Literal["state", "flux"] | None = None

    @property
    def is_flux(self) -> bool:
        """Return `True` when `types == "flux"`."""
        return self.types == "flux"


class TemporalCoverage(BaseModel):
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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str | None = None
    end: str | None = None

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


class Dataset(BaseModel):
    """One curated CMEMS dataset row.

    Mirrors a single `datasets.<dataset_id>:` block in
    `cmems_data_catalog.yaml`. The dataset id itself is the parent
    key in :attr:`Catalog.datasets` and is not stored on the row.

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
        temporal: :class:`TemporalCoverage` — start / end dates of
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

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str = ""
    title: str = ""
    cadence: CadenceLiteral = "irregular"
    domain: str = "global"
    temporal: TemporalCoverage = Field(default_factory=TemporalCoverage)
    variables: dict[str, Variable] = Field(default_factory=dict)


class Catalog(AbstractCatalog):
    """Variable catalog for the Copernicus Marine backend.

    Reads `cmems_data_catalog.yaml` (shipped as package data) and
    exposes its consumed top-level sections as typed pydantic
    fields. Instantiate with no arguments (`Catalog()`) —
    :func:`model_post_init` parses the YAML and populates every
    field in one pass.

    Variables are addressed by the `(dataset_id, variable_name)`
    pair via :meth:`get_variable`. The same short name (e.g.
    `"thetao"`, `"so"`) appears across multiple datasets so the
    dataset id is part of the identity.

    Attributes:
        available_products: Informational list of every CMEMS
            product id worth knowing about. Mirrors the
            `available_products:` block in the YAML; runtime code
            does not consume it.
        datasets: Structural map keyed by CMEMS dataset id. Each
            value is a :class:`Dataset` carrying that dataset's
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
            ['ESACCI-GLO-SST-L4-REP-OBS-SST', 'METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2', 'cmems_mod_arc_phy_my_topaz4_P1D-m']

            ```
    """

    _catalog_kind: str = "CMEMS catalog"

    available_products: list[str] = Field(default_factory=list)
    datasets: dict[str, Dataset] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load `cmems_data_catalog.yaml` when no datasets were supplied.

        `Catalog()` with no args is sugar for `Catalog.load()` — it
        reads the bundled YAML through the `(path, mtime_ns)`-keyed
        cache so repeated construction is ~1 ms. If the caller
        passed `datasets=...`, the disk read is skipped.

        Raises:
            ValueError: When auto-loading, propagates the same
                errors as :meth:`load`.
        """
        if self.datasets:
            return
        loaded = Catalog.load()
        self.available_products = loaded.available_products
        self.datasets = loaded.datasets

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the CMEMS catalog from disk (cached).

        Args:
            catalog_path: Path to a `cmems_data_catalog.yaml`-shaped
                file. Defaults to module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from :func:`_load_catalog_data`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available_products, datasets = _load_catalog_data(catalog_path)
        return cls(
            available_products=list(available_products),
            datasets=dict(datasets),
        )

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the structural per-dataset map.

        Satisfies the abstract base's contract; the actual parsing
        is done in :func:`model_post_init`.

        Returns:
            dict[str, Dataset]: One entry per curated CMEMS dataset.
                Same object as :attr:`datasets`.
        """
        return self.datasets

    def get_variable(self, dataset_id: str, variable_name: str) -> Variable:
        """Return the :class:`Variable` for a `(dataset_id, name)` pair.

        Args:
            dataset_id: CMEMS dataset id as it appears as a key in
                :attr:`datasets` (e.g.
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
