"""Dataset catalog for the risk-indicators backend.

The risk-indicators backend routes a request to one of three country/admin
-indexed risk sources — GFDRR ThinkHazard!, INFORM Risk (JRC), and the Global
Forest Watch Data API. This module is the bridge from a dataset id
(`"thinkhazard:flood_river"`, `"inform:risk"`, `"gfw:tree_cover_loss"`, …) to
the provider, the output kind, and the provider-specific request detail needed
to fetch it.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `risk_indicators_data_catalog.yaml` and exposes each row as a
:class:`Dataset`. The YAML also ships an `admin_codes:` block — an ISO3 ->
ThinkHazard ADM0 division code (== FAO GAUL 2015 ADM0 code) lookup — which
:meth:`Catalog.resolve_admin` reads to turn a `country=` ISO3 into the numeric
division code ThinkHazard's API keys on. Resolve one dataset with
:meth:`Catalog.get` (a did-you-mean hint on an unknown id); list the shipped
ids with :meth:`Catalog.available`.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "risk_indicators_data_catalog.yaml"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus
#: the YAML's `st_mtime_ns`, so editing the file invalidates the entry without
#: re-parsing on every `Catalog()`. Mirrors the `_CATALOG_CACHE` pattern in the
#: climate_indices / usgs_water / gee loaders. The value is the
#: `(datasets, admin_codes)` pair both fields are built from.
_CATALOG_CACHE: dict[tuple[str, int], tuple[dict[str, Dataset], dict[str, int]]] = (
    CatalogParseCache()
)

#: The three risk providers a :class:`Dataset` row can name.
Provider = Literal["thinkhazard", "inform", "gfw"]

#: The two output shapes a risk dataset can emit (tabular -> DataFrame,
#: vector -> FeatureCollection). `OUTPUT_KIND` is set per instance from this.
OutputKind = Literal["tabular", "vector"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes the
    file's `st_mtime_ns`, so any real edit invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> tuple[dict[str, Dataset], dict[str, int]]:
    """Parse, validate, and cache the catalog at `path`.

    Reads the `datasets:` and `admin_codes:` blocks and validates each dataset
    row as a :class:`Dataset`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        A `(datasets, admin_codes)` pair: the dataset map keyed by id, and the
        ISO3 -> ADM0 code lookup.

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
            "The risk-indicators catalog must list at least one dataset."
        )
    rows: dict[str, Dataset] = {}
    for dataset_id, body in datasets_yaml.items():
        try:
            rows[dataset_id] = Dataset(id=dataset_id, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {dataset_id!r} failed validation:\n{exc}"
            ) from exc
    admin_codes = {
        str(iso): int(code) for iso, code in (data.get("admin_codes") or {}).items()
    }

    value = (rows, admin_codes)
    _CATALOG_CACHE[key] = value
    return value


class Dataset(BaseModel):
    """One risk-indicators catalog row.

    The dataset id is the parent key in :attr:`Catalog.datasets` and is also
    stored on the row as :attr:`id` so a resolved :class:`Dataset` is
    self-describing. Which provider-specific fields are populated depends on
    :attr:`provider`; a cross-field validator enforces that the right ones are
    present.

    Attributes:
        id: The dataset id (`"thinkhazard:flood_river"`).
        provider: Which source serves it — `"thinkhazard"`, `"inform"`, or
            `"gfw"`.
        output_kind: `"tabular"` (a `DataFrame`) or `"vector"` (a
            `FeatureCollection`). Copied onto the backend's `OUTPUT_KIND` per
            instance.
        selector: The required request selector — `"country"` (an ISO3).
        long_name: Human-readable label.
        citation: The source's citation string, logged once on use.
        hazard: ThinkHazard hazard mnemonic (`"FL"`, `"EQ"`, …); `None` for the
            `thinkhazard:all` row that returns every hazard.
        workflow_id: INFORM model WorkflowId (e.g. `503` for INFORM Risk Mid
            2025).
        indicator_id: INFORM indicator id (`"INFORM"`, `"HA"`, `"VU"`, `"CC"`).
        release_column: Column holding this indicator's score in the INFORM
            Risk release workbook (`"INFORM RISK"`, `"VULNERABILITY"`, …).
            Set on the rows the published spreadsheet covers; a row without
            it can only be read from the API.
        gfw_dataset: GFW Data API dataset id (`"gadm__tcl__iso_change"`).
        gfw_version: GFW dataset version (`"v20260424"`).
        sql_template: GFW SQL template parameterised by `{iso}`.
        gfw_geostore: GFW geostore kind for a vector dataset (`"admin"`).

    Examples:
        - Build a ThinkHazard row directly:
            ```python
            >>> from earthlens.risk_indicators import Dataset
            >>> row = Dataset(
            ...     id="thinkhazard:flood_river",
            ...     provider="thinkhazard",
            ...     output_kind="tabular",
            ...     hazard="FL",
            ... )
            >>> row.provider
            'thinkhazard'
            >>> row.output_kind
            'tabular'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    provider: Provider
    output_kind: OutputKind
    selector: str = "country"
    long_name: str = ""
    citation: str = ""

    # ThinkHazard
    hazard: str | None = None
    # INFORM
    workflow_id: int | None = None
    indicator_id: str | None = None
    release_column: str | None = None
    # GFW
    gfw_dataset: str | None = None
    gfw_version: str | None = None
    sql_template: str | None = None
    gfw_geostore: str | None = None

    @model_validator(mode="after")
    def _check_provider_fields(self) -> Dataset:
        """Enforce the per-provider required fields.

        Returns:
            The validated row.

        Raises:
            ValueError: If the row omits a field its `provider` needs (an INFORM
                row without `workflow_id`/`indicator_id`, a tabular GFW row
                without `gfw_dataset`/`gfw_version`/`sql_template`, or a vector
                GFW row without `gfw_geostore`).
        """
        if self.provider in ("thinkhazard", "inform") and self.output_kind != "tabular":
            raise ValueError(
                f"{self.provider} dataset {self.id!r} must be output_kind 'tabular'"
            )
        if self.provider == "inform":
            if self.workflow_id is None or not self.indicator_id:
                raise ValueError(
                    f"inform dataset {self.id!r} needs workflow_id and indicator_id"
                )
        if self.provider == "gfw":
            if self.output_kind == "vector":
                if not self.gfw_geostore:
                    raise ValueError(
                        f"vector gfw dataset {self.id!r} needs gfw_geostore"
                    )
            elif not (self.gfw_dataset and self.gfw_version and self.sql_template):
                raise ValueError(
                    f"tabular gfw dataset {self.id!r} needs gfw_dataset, "
                    "gfw_version and sql_template"
                )
        return self


class Catalog(AbstractCatalog):
    """Dataset catalog for the risk-indicators backend.

    Reads the bundled `risk_indicators_data_catalog.yaml` (shipped as package
    data) and exposes its `datasets:` block as a map of :class:`Dataset` rows
    keyed by id, plus an `admin_codes:` ISO3 -> ADM0 lookup. Instantiate with no
    arguments (`Catalog()`). Resolve one row with :meth:`get`, list the shipped
    ids with :meth:`available`, and turn a country ISO3 into a ThinkHazard
    division code with :meth:`resolve_admin`.

    Attributes:
        datasets: Map from dataset id to its :class:`Dataset` row.
        admin_codes: Map from ISO3 country code to ThinkHazard ADM0 division
            code (== FAO GAUL 2015 ADM0 code).

    Examples:
        - Resolve a row and a country code:
            ```python
            >>> from earthlens.risk_indicators import Catalog
            >>> cat = Catalog()
            >>> cat.get("thinkhazard:flood_river").provider
            'thinkhazard'
            >>> cat.get("gfw:admin_boundary").output_kind
            'vector'
            >>> cat.resolve_admin("KEN")
            '133'

            ```
        - An unknown but close id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.risk_indicators import Catalog
            >>> Catalog().get("inform:rsk")
            Traceback (most recent call last):
                ...
            ValueError: 'inform:rsk' is not in the risk-indicators catalog. Known datasets: [...]. Did you mean 'inform:risk'?

            ```
    """

    _catalog_kind: str = "risk-indicators catalog"
    _entry_noun: str = "datasets"

    #: The dataset rows live in the base :attr:`datasets` field so the inherited
    #: dict surface (`len`, `in`, `[]`, iteration) and :meth:`get_dataset`'s
    #: did-you-mean hint work unchanged.
    datasets: dict[str, Dataset] = Field(default_factory=dict)

    #: ISO3 -> ThinkHazard ADM0 division code lookup (== FAO GAUL 2015 ADM0).
    admin_codes: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `admin_codes` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "admin_codes": loaded.admin_codes,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the risk-indicators catalog from disk.

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
        datasets, admin_codes = _load_catalog_data(catalog_path)
        return cls(datasets=dict(datasets), admin_codes=dict(admin_codes))

    def get(self, dataset_id: str) -> Dataset:
        """Resolve a dataset id to its :class:`Dataset` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown id.

        Args:
            dataset_id: A shipped dataset id (`"thinkhazard:flood_river"`).

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

    def resolve_admin(self, country: str, level: int = 0) -> str:
        """Turn a country ISO3 into a ThinkHazard ADM0 division code.

        ThinkHazard keys its API on numeric division codes, which at country
        level are the FAO GAUL 2015 ADM0 codes. This maps a `country=` ISO3
        (case-insensitive) onto that code via the shipped `admin_codes:` table.
        Only the country level (`level=0`) is resolvable from the table; for a
        sub-national division, pass a raw `admin_code=` to the backend instead.

        Args:
            country: An ISO3 country code (`"KEN"`).
            level: The admin level; only `0` (country) is supported here.

        Returns:
            str: The ThinkHazard ADM0 division code as a string (`"133"`).

        Raises:
            ValueError: If `level` is not `0`, or `country` is not a known ISO3
                (with a did-you-mean hint).
        """
        import difflib

        if level != 0:
            raise ValueError(
                f"resolve_admin only resolves country level (level=0), got "
                f"level={level}; pass a raw admin_code= for a sub-national "
                "ThinkHazard division."
            )
        iso = country.strip().upper()
        code = self.admin_codes.get(iso)
        if code is None:
            close = difflib.get_close_matches(iso, self.admin_codes, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{country!r} is not a known ISO3 country code in the "
                f"ThinkHazard admin table.{hint}"
            )
        return str(code)
