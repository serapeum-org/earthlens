"""Catalog for the Aqueduct riverine flood-risk backend.

The Aqueduct backend fetches the WRI Aqueduct Global Flood Analyzer (2015)
riverine flood-risk shapefiles — expected exposure of GDP, population, and urban
area to river flooding, aggregated by admin unit (country / state / river basin)
across nine flood return periods, a 2010 baseline, and seven 2030 climate ×
socio-economic scenarios. This module is the bridge from an admin-level name
(`"country"`, `"state"`, `"basin"`) to its download URL and shapefile, plus the
column-grammar vocabularies (`indicators` / `years` / `scenarios` /
`return_periods`) that name a `.dbf` column: an attribute column is
`f"{indicator}{year}_{scenario}_{return_period}"`, e.g. `"P30_28_100"` for
population affected, 2030, SSP2-RCP8.5, 100-year flood.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog` subclass that
loads the bundled `aqueduct_data_catalog.yaml` and exposes its `admin_levels:`
block as a map of :class:`AdminLevel` rows under the inherited :attr:`datasets`
field (so `cat["country"]` / `"country" in cat` / `len(cat)` and the
did-you-mean error come for free), alongside the `base_url`, `license`,
`attribution`, and the three code-map vocabularies. Resolve one admin level with
:meth:`get`, list them with :meth:`available`.

:data:`CATALOG_PATH` is the path to the bundled YAML; it is monkey-patchable in
tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "aqueduct_data_catalog.yaml"

#: Module-level parse cache keyed on the resolved path plus the YAML's
#: `(mtime_ns, size)`, so a repeated `Catalog()` skips the parse + validation.
#: The cached value is every field the catalog is built from.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class AdminLevel(BaseModel):
    """One admin level's download + shapefile spec.

    The admin-level name (`"country"` / `"state"` / `"basin"`) is the parent key
    in :attr:`Catalog.datasets`, not stored on the row.

    Attributes:
        zip: The zip file name the shapefile lives in — a direct download under
            `base_url` when :attr:`container_zip` is `None`, otherwise the entry
            to extract from that outer bundle first.
        shapefile_stem: The shapefile name (without extension) inside `zip`; the
            `.shp` and its sidecars (`.dbf` / `.shx` / `.prj`) all share it.
        container_zip: For an admin level with no standalone URL (`state`), the
            outer bundle to download; :attr:`zip` is then extracted from it. `None`
            for a directly-downloadable level (`country`, `basin`).

    Examples:
        - A direct-download level and a nested one:
            ```python
            >>> from earthlens.aqueduct import AdminLevel
            >>> AdminLevel(zip="by_country.zip", shapefile_stem="by_country").container_zip is None
            True
            >>> AdminLevel(
            ...     zip="by_state.zip", shapefile_stem="by_state", container_zip="bundle.zip"
            ... ).container_zip
            'bundle.zip'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    zip: str
    shapefile_stem: str
    container_zip: str | None = None


class Scenario(BaseModel):
    """One climate × socio-economic scenario's code and valid years.

    Attributes:
        code: The two-character scenario code embedded in a column name
            (`"bh"`, `"24"`, `"28"`, `"38"`, `"b4"`, `"b8"`, `"2h"`, `"3h"`).
        years: The years this scenario is defined for — `["2010"]` for the
            `baseline`, `["2030"]` for every future scenario.

    Examples:
        - The 2010 baseline vs a 2030 future:
            ```python
            >>> from earthlens.aqueduct import Scenario
            >>> Scenario(code="bh", years=["2010"]).code
            'bh'
            >>> Scenario(code="28", years=["2030"]).years
            ['2030']

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    years: list[str] = Field(default_factory=list)


def _parse_rows(
    rows_yaml: dict[str, Any], model: type[BaseModel], path: Path, label: str
) -> dict[str, Any]:
    """Validate a `name -> body` mapping into `model` instances.

    Args:
        rows_yaml: The raw mapping from the catalog YAML.
        model: The pydantic model each body is validated against.
        path: The catalog path (for the error message).
        label: The row kind, named in a validation error (`"admin level"`).

    Returns:
        dict[str, Any]: One validated `model` instance per key.

    Raises:
        ValueError: If any row fails validation.
    """
    parsed: dict[str, Any] = {}
    for name, body in rows_yaml.items():
        try:
            parsed[name] = model(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} {label} {name!r} failed validation:\n{exc}"
            ) from exc
    return parsed


def _load_catalog_data(path: Path) -> dict[str, Any]:
    """Parse, validate, and cache the catalog at `path`.

    Reads the `admin_levels:` rows plus the `base_url`, `license`,
    `attribution`, and the `indicators:` / `years:` / `scenarios:` /
    `return_periods:` vocabularies.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        dict[str, Any]: Every field a :class:`Catalog` is built from.

    Raises:
        ValueError: If the file has no `admin_levels:` block, or a row / scenario
            fails validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cast("dict[str, Any]", cached)

    data = load_yaml_strict(path) or {}
    levels_yaml = data.get("admin_levels") or {}
    if not levels_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'admin_levels:' block. "
            "The Aqueduct catalog must list at least one admin level."
        )
    levels = _parse_rows(levels_yaml, AdminLevel, path, "admin level")
    scenarios = _parse_rows(data.get("scenarios") or {}, Scenario, path, "scenario")

    value: dict[str, Any] = {
        "datasets": levels,
        "base_url": str(data.get("base_url") or "").rstrip("/"),
        "license": str(data.get("license") or ""),
        "attribution": str(data.get("attribution") or ""),
        "indicators": {
            str(k): str(v) for k, v in (data.get("indicators") or {}).items()
        },
        "years": {str(k): str(v) for k, v in (data.get("years") or {}).items()},
        "scenarios": scenarios,
        "return_periods": {
            int(k): str(v) for k, v in (data.get("return_periods") or {}).items()
        },
    }
    _CATALOG_CACHE[key] = value
    return value


class Catalog(AbstractCatalog):
    """Catalog for the Aqueduct riverine flood-risk backend.

    Reads the bundled `aqueduct_data_catalog.yaml` (shipped as package data) and
    exposes its `admin_levels:` block as a map of :class:`AdminLevel` rows keyed
    by name under the inherited :attr:`datasets` field, plus the `base_url`,
    `license`, `attribution`, and the `indicators` / `years` / `scenarios` /
    `return_periods` vocabularies. Instantiate with no arguments (`Catalog()`);
    resolve one admin level with :meth:`get`, list them with :meth:`available`,
    and build the download URL for a level with :meth:`download_url`.

    Attributes:
        datasets: Map from admin-level name (`"country"` / `"state"` / `"basin"`)
            to its :class:`AdminLevel` row.
        base_url: Host prefix every download URL is built on.
        license: SPDX-style redistribution licence (`"CC-BY-4.0"`).
        attribution: Required attribution string.
        indicators: Public metric name -> `.dbf` column prefix (`gdp_affected`
            -> `G`).
        years: Year -> column code (`"2010"` -> `"10"`).
        scenarios: Scenario name -> its :class:`Scenario` (code + valid years).
        return_periods: Return period (years, int) -> column code (`1000` ->
            `"1T"`).

    Examples:
        - Resolve an admin level and read a vocabulary:
            ```python
            >>> from earthlens.aqueduct import Catalog
            >>> cat = Catalog()
            >>> cat.available()
            ['basin', 'country', 'state']
            >>> cat.get("country").shapefile_stem
            'aqueduct_global_flood_risk_data_by_country_20150304'
            >>> cat.indicators["population_affected"]
            'P'
            >>> cat.license
            'CC-BY-4.0'

            ```
        - An unknown level raises with a did-you-mean hint:
            ```python
            >>> from earthlens.aqueduct import Catalog
            >>> Catalog().get("countries")
            Traceback (most recent call last):
                ...
            ValueError: 'countries' is not in the Aqueduct catalog. Known admin levels: ['basin', 'country', 'state']. Did you mean 'country'?

            ```
    """

    _catalog_kind: str = "Aqueduct catalog"
    _entry_noun: str = "admin levels"

    datasets: dict[str, AdminLevel] = Field(default_factory=dict)
    base_url: str = ""
    license: str = ""
    attribution: str = ""
    indicators: dict[str, str] = Field(default_factory=dict)
    years: dict[str, str] = Field(default_factory=dict)
    scenarios: dict[str, Scenario] = Field(default_factory=dict)
    return_periods: dict[int, str] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: Every field read from the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "base_url": loaded.base_url,
            "license": loaded.license,
            "attribution": loaded.attribution,
            "indicators": loaded.indicators,
            "years": loaded.years,
            "scenarios": loaded.scenarios,
            "return_periods": loaded.return_periods,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Aqueduct catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `admin_levels:` block, or a row /
                scenario fails validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(**_load_catalog_data(catalog_path))

    def get(self, admin_level: str) -> AdminLevel:
        """Resolve an admin-level name to its :class:`AdminLevel` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            admin_level: A shipped admin level (`"country"` / `"state"` /
                `"basin"`).

        Returns:
            AdminLevel: The matching catalog row.

        Raises:
            ValueError: If `admin_level` is not a known admin level.
        """
        return cast("AdminLevel", self.get_dataset(admin_level))

    def available(self) -> list[str]:
        """Return the sorted admin-level names.

        Returns:
            list[str]: `["basin", "country", "state"]`.
        """
        return sorted(self.datasets)

    def download_url(self, admin_level: str) -> str:
        """Return the download URL of the zip the admin level's shapefile lives in.

        For a nested level (`state`) this is the outer `container_zip` bundle;
        for a direct level it is the shapefile's own `zip`. Either way the bytes
        come from `base_url`.

        Args:
            admin_level: A shipped admin level.

        Returns:
            str: The absolute download URL.

        Raises:
            ValueError: If `admin_level` is not a known admin level.
        """
        row = self.get(admin_level)
        return f"{self.base_url}/{row.container_zip or row.zip}"
