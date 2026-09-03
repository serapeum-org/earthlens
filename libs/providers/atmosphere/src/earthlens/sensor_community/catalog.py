"""Pollutant dispatch table for the Sensor.Community backend.

Sensor.Community archives one CSV per (sensor, day); each CSV's
measurement columns depend on the sensor type (particulate sensors
report `P0`/`P1`/`P2`, climate sensors report
`temperature`/`humidity`/`pressure`). Users pass earthlens pollutant
names in `variables=[...]`; this module maps each name to the CSV
`column` it lives in and the `sensor_types` (archive slugs) whose files
carry it.

Like the OpenAQ / AirNow / EEA provider tables it is deliberately tiny
and fixed, so there is no `refresh` / `probe` / `audit` tooling and no
`tools/sensor_community/` directory; adding a pollutant later is a
hand-edit of one YAML row.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads
the bundled `sensor_community_data_catalog.yaml` and exposes each row as
a `Pollutant`. Resolve a single name with `Catalog.get_pollutant`
(raises with a did-you-mean hint on an unknown name), the union of the
sensor-type slugs for a list of names with `Catalog.sensor_types_for`,
or the `column` -> name reverse map for a list of names with
`Catalog.columns_for`. `CATALOG_PATH` is the path to the bundled YAML
and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "sensor_community_data_catalog.yaml"

#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


#: The pollutant groups a Sensor.Community `Pollutant` can belong to.
PollutantGroup = Literal["particulate", "meteorological", "other"]


class Pollutant(BaseModel):
    """One Sensor.Community pollutant's dispatch row.

    The user-facing name is the parent key in `Catalog.pollutants` and is
    also stored on the row as `name` so a resolved `Pollutant` is
    self-describing.

    Attributes:
        name: Short machine name (`"pm25"`, `"temperature"`); matches the
            catalog key.
        column: The CSV column this pollutant is read from (`"P2"` for
            PM2.5, `"temperature"`).
        sensor_types: Archive sensor-type slugs whose per-sensor CSV
            carries `column` (`["sds011", "pms5003", ...]`). Used to
            filter discovery and choose which archive files to fetch.
        units: The reporting unit (`"µg/m³"`, `"°C"`). Sensor.Community
            reports pressure in pascals.
        display_name: Human-readable label for docs / plots (`"PM2.5"`).
        group: Coarse classification — `"particulate"`, `"meteorological"`,
            or `"other"`.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.sensor_community import Pollutant
            >>> p = Pollutant(name="pm25", column="P2", sensor_types=["sds011"])
            >>> (p.column, p.sensor_types)
            ('P2', ['sds011'])

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    column: str
    sensor_types: list[str] = Field(min_length=1)
    units: str = ""
    display_name: str = ""
    group: PollutantGroup = "other"


def _parse_sensor_community_catalog(files: list[Path]):
    """Parse and validate the Sensor.Community catalog rows.

    Args:
        files: The contributing YAML files (Sensor.Community ships a single file).

    Returns:
        The validated rows, in the shape the catalog caches.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    pollutants_yaml = data.get("pollutants") or {}
    if not pollutants_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'pollutants:' "
            "block. The Sensor.Community catalog must list at least one "
            "pollutant."
        )
    pollutants: dict[str, Pollutant] = {}
    for name, body in pollutants_yaml.items():
        try:
            pollutants[name] = Pollutant(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} pollutant {name!r} failed validation:\n{exc}"
            ) from exc
    return pollutants


class Catalog(AbstractCatalog[Pollutant]):
    """Pollutant catalog for the Sensor.Community backend.

    Reads the bundled `sensor_community_data_catalog.yaml` (shipped as
    package data) and exposes its `pollutants:` block as a map of
    `Pollutant` rows. Instantiate with no arguments (`Catalog()`);
    `model_post_init` loads and validates the YAML in one pass.

    Attributes:
        pollutants: Map from the user-facing pollutant name to its
            `Pollutant` dispatch row.

    Examples:
        - Resolve names to the union of serving sensor types and to CSV
          columns:
            ```python
            >>> from earthlens.sensor_community import Catalog
            >>> cat = Catalog()
            >>> "sds011" in cat.sensor_types_for(["pm25"])
            True
            >>> cat.columns_for(["pm25", "pm10"])
            {'P2': 'pm25', 'P1': 'pm10'}

            ```
    """

    _catalog_kind: str = "Sensor.Community pollutant catalog"
    _entry_noun: str = "pollutants"

    #: The pollutant rows live in the base `datasets` field so the inherited
    #: dict surface works unchanged. `pollutants` is the domain-named alias.
    datasets: dict[str, Pollutant] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_pollutants_alias(cls, data: Any) -> Any:
        """Accept the `pollutants=` kwarg as an alias for `datasets`.

        Callers and tests construct `Catalog(pollutants={...})`. The rows
        live in the base `datasets` field, so rewrite that key on the way
        in. An explicit `datasets=` always wins.

        Args:
            data: The raw model input (a mapping when constructed with
                keyword arguments).

        Returns:
            The input with `pollutants` renamed to `datasets`, untouched
            otherwise.
        """
        if isinstance(data, dict) and "pollutants" in data and "datasets" not in data:
            data = dict(data)
            data["datasets"] = data.pop("pollutants")
        return data

    @property
    def pollutants(self) -> dict[str, Pollutant]:
        """The pollutant map — alias for the base `datasets` field.

        Returns:
            dict[str, Pollutant]: The same mapping stored in `datasets`.
        """
        return self.datasets

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` read from
                the bundled catalog.
        """
        return {"datasets": Catalog.load().datasets}

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Sensor.Community pollutant catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `pollutants:` block, or a row fails `Pollutant` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        cached = load_catalog(
            catalog_path,
            _CATALOG_CACHE,
            _parse_sensor_community_catalog,
            provider="Sensor.Community",
        )
        return cls(datasets=dict(cached))

    def get_pollutant(self, name: str) -> Pollutant:
        """Resolve a pollutant name to its `Pollutant` row.

        Thin wrapper over the inherited `get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            name: A user-facing pollutant name (`"pm25"`, `"temperature"`).

        Returns:
            Pollutant: The matching dispatch row.

        Raises:
            ValueError: If `name` is not a known pollutant.
        """
        return cast("Pollutant", self.get_dataset(name))

    def sensor_types_for(self, names: list[str]) -> set[str]:
        """Return the union of serving sensor-type slugs for `names`.

        Args:
            names: User-facing pollutant names to resolve.

        Returns:
            set[str]: Every archive sensor-type slug whose CSV carries at
                least one of the requested pollutants.

        Raises:
            ValueError: If any name is unknown (via `get_pollutant`).
        """
        types: set[str] = set()
        for name in names:
            types.update(self.get_pollutant(name).sensor_types)
        return types

    def columns_for(self, names: list[str]) -> dict[str, str]:
        """Return the CSV `column` -> pollutant name map for `names`.

        Used at parse time to pull every requested pollutant's value out
        of one sensor CSV in a single pass.

        Args:
            names: User-facing pollutant names to resolve.

        Returns:
            dict[str, str]: Each requested pollutant's CSV column mapped
                to its name (`{"P2": "pm25", "P1": "pm10"}`).

        Raises:
            ValueError: If any name is unknown (via `get_pollutant`).
        """
        return {self.get_pollutant(name).column: name for name in names}
