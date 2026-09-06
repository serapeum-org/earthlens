"""Config + curated-vocabulary catalog for the CMIP6 backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`cmip6_data_catalog.yaml`. CMIP6 is addressed by a *facet tuple*
(`source_id`, `experiment_id`, `variable_id`, `table_id`, plus optional
`member_id` / `grid_label` / `version`), and the per-store index — one Zarr
store per facet combination — is far too large to inline (~515k rows). That
index is the consolidated-stores CSV at :attr:`Catalog.csv_url`, fetched and
cached by :mod:`earthlens.cmip6.resolver`; this catalog holds only the *config*
(CSV URL, bucket, facet columns, defaults) plus a *curated vocabulary* of the
common variables / experiments / tables / sources used for output metadata,
docs, and did-you-mean hints.

The curated `variables:` block is exposed under the inherited
:attr:`~earthlens.base.AbstractCatalog.datasets` field, so a variable resolves
with `cat["tas"]` / `"tas" in cat` / the did-you-mean error for free;
`experiments:` / `tables:` / `sources:` hang off parallel maps. Resolution
itself runs against the full CSV, so an *uncurated* facet still downloads — the
curated rows only enrich metadata and error messages.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "cmip6_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level CMIP6 catalog parse cache."""
    _CATALOG_CACHE.clear()


class Cmip6Variable(BaseModel):
    """One curated CMIP6 variable row (the `variable_id` leaf).

    A frozen value object with descriptive metadata only — CMIP6 variables carry
    no request-shaping parameters; the facet tuple selects the store and pyramids
    reads the array. Curated rows are optional: an uncurated `variable_id` still
    resolves against the CSV, it just lacks these labels.

    Attributes:
        units: CMIP6 CMOR unit (`"K"`, `"kg m-2 s-1"`, `"1"` for a
            dimensionless fraction).
        long_name: Human-readable description used in docs and logs.
        realm: Modelling realm the variable belongs to (`"atmos"`, `"ocean"`,
            `"land"`, `"seaIce"`, ...).

    Examples:
        - Build a variable row directly:
            ```python
            >>> from earthlens.cmip6 import Cmip6Variable
            >>> v = Cmip6Variable(units="K", long_name="Near-surface air temperature")
            >>> v.units
            'K'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str = ""
    long_name: str = ""
    realm: str = ""


class Experiment(BaseModel):
    """One curated CMIP6 experiment (scenario / diagnostic) row.

    Attributes:
        activity_id: The MIP the experiment belongs to (`"CMIP"` for the
            DECK / historical runs, `"ScenarioMIP"` for the SSPs).
        description: Human-readable summary.

    Examples:
        - Inspect an experiment's activity:
            ```python
            >>> from earthlens.cmip6 import Catalog
            >>> Catalog().get_experiment("ssp585").activity_id
            'ScenarioMIP'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_id: str = ""
    description: str = ""


class Table(BaseModel):
    """One curated CMIP6 MIP-table row (a realm x cadence bundle).

    Attributes:
        realm: Modelling realm (`"atmos"`, `"ocean"`, `"land"`, ...).
        cadence: Output cadence (`"monthly"`, `"daily"`, `"3-hourly"`,
            `"yearly"`, `"fixed"`).
        description: Human-readable summary.

    Examples:
        - Read a table's cadence:
            ```python
            >>> from earthlens.cmip6 import Catalog
            >>> Catalog().get_table("Amon").cadence
            'monthly'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    realm: str = ""
    cadence: str = ""
    description: str = ""


class Source(BaseModel):
    """One curated CMIP6 source-model (GCM) row.

    Attributes:
        institution_id: The modelling centre that produced the model.
        terms_note: Any per-model licence / attribution nuance (most CMIP6
            models are CC BY 4.0, cited via the source GCM).
        description: Optional human-readable summary.

    Examples:
        - Read a source's institution:
            ```python
            >>> from earthlens.cmip6 import Catalog
            >>> Catalog().get_source("CanESM5").institution_id
            'CCCma'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    institution_id: str = ""
    terms_note: str = ""
    description: str = ""


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the CMIP6 catalog YAML into a populated :class:`Catalog`.

    Args:
        files: The contributing YAML files (CMIP6 ships a single file).

    Returns:
        dict[str, Any]: The validated construction kwargs. The payload is
            cached, not a built Catalog, so `load()` makes a fresh instance per
            call and one caller doing `datasets.pop(...)` cannot reach another's
            mapping. The row objects inside it *are* shared and are frozen
            pydantic models: treat them as read-only. A frozen model still
            permits in-place mutation of a mutable field (`row.columns[...] =`),
            which would reach every holder — deep-copying every row per load
            would cost more than that edge is worth.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    csv_url = data.get("csv_url")
    if not csv_url:
        raise ValueError(
            f"{path} is missing its 'csv_url:'. The CMIP6 catalog must name "
            "the consolidated-stores CSV."
        )
    variables = Catalog._parse_block(path, data.get("variables"), Cmip6Variable)
    experiments = Catalog._parse_block(path, data.get("experiments"), Experiment)
    tables = Catalog._parse_block(path, data.get("tables"), Table)
    sources = Catalog._parse_block(path, data.get("sources"), Source)
    defaults = data.get("defaults") or {}
    return {
        "csv_url": csv_url,
        "bucket": data.get("bucket", "cmip6"),
        "facet_columns": list(data.get("facet_columns") or []),
        "default_member_id": defaults.get("member_id", "r1i1p1f1"),
        "default_version": defaults.get("version", "latest"),
        "default_terms_note": data.get("default_terms_note", ""),
        "datasets": variables,
        "experiments": experiments,
        "tables": tables,
        "sources": sources,
    }


class Catalog(AbstractCatalog[Cmip6Variable]):
    """Config + curated-vocabulary catalog for the CMIP6 backend.

    Reads the bundled `cmip6_data_catalog.yaml` (shipped as package data) and
    exposes its `variables:` block as a map of :class:`Cmip6Variable` rows keyed
    by `variable_id` under the inherited :attr:`datasets` field, plus parallel
    :attr:`experiments`, :attr:`tables`, and :attr:`sources` maps and the
    resolution config (:attr:`csv_url`, :attr:`bucket`, :attr:`facet_columns`,
    :attr:`default_member_id`, :attr:`default_version`). Instantiate with no
    arguments (`Catalog()`); :func:`model_post_init` loads and validates the YAML
    in one pass and caches it by `(path, mtime)`.

    Attributes:
        csv_url: URL of the consolidated-stores CSV (the full per-store index).
        bucket: The public GCS bucket the `zstore` URIs live on (`"cmip6"`).
        facet_columns: The CSV facet columns, in file order.
        default_member_id: Member label applied when a request omits it.
        default_version: Version-selection policy (`"latest"`).
        default_terms_note: Attribution fallback for an uncurated source.
        datasets: Map from `variable_id` to its :class:`Cmip6Variable` row.
        experiments: Map from `experiment_id` to its :class:`Experiment` row.
        tables: Map from `table_id` to its :class:`Table` row.
        sources: Map from `source_id` to its :class:`Source` row.

    Examples:
        - List curated variables and resolve one:
            ```python
            >>> from earthlens.cmip6 import Catalog
            >>> cat = Catalog()
            >>> "tas" in cat
            True
            >>> cat.get_dataset("tas").units
            'K'
            >>> cat.bucket
            'cmip6'

            ```
        - An unknown variable raises with a did-you-mean hint:
            ```python
            >>> from earthlens.cmip6 import Catalog
            >>> Catalog().get_dataset("rainfall")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'rainfall' is not in the CMIP6 catalog. Known variables: [...].

            ```
    """

    _catalog_kind: str = "CMIP6 catalog"
    _entry_noun: str = "variables"

    csv_url: str = ""
    bucket: str = "cmip6"
    facet_columns: list[str] = Field(default_factory=list)
    default_member_id: str = "r1i1p1f1"
    default_version: str = "latest"
    default_terms_note: str = ""

    datasets: dict[str, Cmip6Variable] = Field(default_factory=dict)
    experiments: dict[str, Experiment] = Field(default_factory=dict)
    tables: dict[str, Table] = Field(default_factory=dict)
    sources: dict[str, Source] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no variables were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read (used in
        tests). Either way the `available_datasets` index is derived from the
        loaded variable map.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is missing,
                empty, or has a malformed row.
        """
        if not self.datasets and not self.csv_url:
            loaded = Catalog.load()
            self.csv_url = loaded.csv_url
            self.bucket = loaded.bucket
            self.facet_columns = loaded.facet_columns
            self.default_member_id = loaded.default_member_id
            self.default_version = loaded.default_version
            self.default_terms_note = loaded.default_terms_note
            self.datasets = loaded.datasets
            self.experiments = loaded.experiments
            self.tables = loaded.tables
            self.sources = loaded.sources
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the CMIP6 catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file is missing
                its `csv_url`, or any curated row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="CMIP6")
        return cls(**payload)

    @staticmethod
    def _parse_block(path: Path, block: Any, model: type[BaseModel]) -> dict[str, Any]:
        """Validate one YAML mapping block into `{key: model(...)}`.

        Args:
            path: Catalog path, for error messages.
            block: The raw mapping from the YAML (or `None` when absent).
            model: The pydantic row type to build.

        Returns:
            dict[str, Any]: The validated rows keyed by their YAML key.

        Raises:
            ValueError: If any row fails validation.
        """
        out: dict[str, Any] = {}
        for key, body in (block or {}).items():
            try:
                out[str(key)] = model(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} {model.__name__} {key!r} failed validation:\n{exc}"
                ) from exc
        return out

    def get_experiment(self, key: str) -> Experiment:
        """Return the :class:`Experiment` for `key`, with a did-you-mean hint.

        Args:
            key: An `experiment_id` (`"ssp585"`, `"historical"`).

        Returns:
            Experiment: The matching experiment row.

        Raises:
            ValueError: If `key` is not a curated experiment.
        """
        return cast("Experiment", self._get_from(self.experiments, key, "experiment"))

    def get_table(self, key: str) -> Table:
        """Return the :class:`Table` for `key`, with a did-you-mean hint.

        Args:
            key: A `table_id` (`"Amon"`, `"day"`, `"Omon"`).

        Returns:
            Table: The matching table row.

        Raises:
            ValueError: If `key` is not a curated table.
        """
        return cast("Table", self._get_from(self.tables, key, "table"))

    def get_source(self, key: str) -> Source:
        """Return the :class:`Source` for `key`, with a did-you-mean hint.

        Args:
            key: A `source_id` (`"CanESM5"`, `"GFDL-ESM4"`).

        Returns:
            Source: The matching source row.

        Raises:
            ValueError: If `key` is not a curated source.
        """
        return cast("Source", self._get_from(self.sources, key, "source"))

    def terms_note(self, source_id: str) -> str:
        """Return the attribution note for `source_id`.

        Falls back to :attr:`default_terms_note` for an uncurated source (or a
        curated one with no per-model note).

        Args:
            source_id: The model key (`"CanESM5"`).

        Returns:
            str: The per-model `terms_note`, else the catalog default.
        """
        source = self.sources.get(source_id)
        if source is not None and source.terms_note:
            return source.terms_note
        return self.default_terms_note

    @staticmethod
    def _get_from(mapping: dict[str, Any], key: str, noun: str) -> Any:
        """Look up `key` in `mapping`, raising a did-you-mean `ValueError`.

        Args:
            mapping: The curated map to search.
            key: The requested key.
            noun: Singular noun for the error message (`"experiment"`).

        Returns:
            The matching row.

        Raises:
            ValueError: If `key` is absent.
        """
        try:
            return mapping[key]
        except KeyError:
            import difflib

            close = difflib.get_close_matches(key, mapping, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not a curated CMIP6 {noun}. "
                f"Known {noun}s: {sorted(mapping)}.{hint}"
            ) from None
