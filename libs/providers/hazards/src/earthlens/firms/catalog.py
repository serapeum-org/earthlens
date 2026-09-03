"""Sensor dispatch table for the NASA FIRMS active-fire backend.

FIRMS is a fixed set of active-fire sensors queried through one area CSV
endpoint, not a curated dataset catalogue, so this "catalog" is small: a
handful of rows mapping a FIRMS source code (`"VIIRS_SNPP_NRT"`,
`"MODIS_NRT"`, …) to a little metadata. It follows the ECMWF / GEE
convention where a **sensor plays the "dataset" role and its CSV columns
play the "variable" role** — a :class:`Sensor` nests a `columns:` map of
:class:`SensorColumn` rows.

Like `gdacs_data_catalog.yaml` / `fdsn_data_catalog.yaml` there is no
`available_*` index: the listed sensors *are* the whole FIRMS universe,
so an "available vs curated" split would just duplicate the map. Unlike
those two, FIRMS *does* ship a probe + audit pair (`tools/firms/`)
because the per-sensor CSV column schema varies by sensor family (MODIS
reports a numeric `confidence`; VIIRS reports a categorical `l`/`n`/`h`),
which is exactly the kind of drift a probe pins down.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `firms_data_catalog.yaml` and exposes
each row as a :class:`Sensor`, keyed by code under the inherited
`datasets` field — which is what gives it the `cat["MODIS_NRT"]` /
`"MODIS_NRT" in cat` / `len(cat)` dict-like surface and the did-you-mean
error for free. :data:`CATALOG_PATH` is the path to the bundled YAML and
is monkey-patchable in tests.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "firms_data_catalog.yaml"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path
#: plus each contributing file's `(mtime_ns, size)`, so a repeated
#: `Catalog()` skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class SensorColumn(BaseModel):
    """One FIRMS CSV column's metadata (the "variable" analog).

    A frozen value object describing a single column a sensor emits in
    its area-CSV response. Mirrors the ECMWF / GEE per-variable row, but
    minimal: FIRMS CSV columns carry no request-shaping parameters, only
    descriptive metadata.

    Attributes:
        units: Physical unit of the column (`"K"`, `"MW"`, `"%"`, or
            `"1"` for the dimensionless VIIRS confidence token).
        long_name: Human-readable description used in docs and logs.

    Examples:
        - Build a column row directly:
            ```python
            >>> from earthlens.firms import SensorColumn
            >>> col = SensorColumn(units="MW", long_name="Fire radiative power")
            >>> col.units
            'MW'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str = ""
    long_name: str = ""


class Temporal(BaseModel):
    """A sensor's coverage window and quality tier.

    Attributes:
        start: First date the sensor has data for, or `None` if unknown.
        end: Last date covered, or `None` for an ongoing sensor.
        quality: `"NRT"` (near-real-time, last ~2 months) or `"SP"`
            (standard-quality archive). Drives the
            :class:`~earthlens.firms.FIRMS` out-of-coverage warning: a
            request for an old window against an `*_NRT` sensor is
            silently empty upstream, so the backend warns and names the
            `*_SP` variant.

    Examples:
        - An ongoing NRT sensor:
            ```python
            >>> from earthlens.firms.catalog import Temporal
            >>> t = Temporal(start="2012-01-20", quality="NRT")
            >>> t.end is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: dt.date | None = None
    end: dt.date | None = None
    quality: Literal["NRT", "SP"] = "NRT"


class Sensor(BaseModel):
    """One FIRMS sensor's dispatch row (the "dataset" analog).

    The FIRMS source code is the parent key in :attr:`Catalog.datasets`
    and is repeated here as :attr:`code` so a :class:`Sensor` carries its
    own identity when passed around outside the catalog.

    Attributes:
        code: FIRMS source code (`"VIIRS_SNPP_NRT"`, `"MODIS_NRT"`, …) —
            the value passed in `variables=[...]` and used as the URL
            `source` path segment.
        name: Human-readable sensor name used in logs and docs.
        family: `"MODIS"`, `"VIIRS"`, `"GOES"`, or `"LANDSAT"` — selects
            the confidence / brightness schema handling in
            :mod:`earthlens.firms.events`. MODIS and GOES report numeric
            confidence; VIIRS reports the categorical token `l`/`n`/`h`
            and LANDSAT reports `l`/`m`/`h`. Brightness comes from
            `brightness` (MODIS), `bright_ti4` (VIIRS / GOES), or is
            absent (LANDSAT carries no brightness or FRP column).
        resolution_m: Nominal nadir pixel size in metres (375 for VIIRS,
            1000 for MODIS).
        temporal: The sensor's coverage window and quality tier.
        columns: Per-column metadata keyed by CSV column name.

    Examples:
        - Inspect a sensor's resolution and a column:
            ```python
            >>> from earthlens.firms import Catalog
            >>> sensor = Catalog().get_sensor("VIIRS_SNPP_NRT")
            >>> sensor.resolution_m
            375
            >>> sensor.columns["frp"].units
            'MW'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    name: str = ""
    family: Literal["MODIS", "VIIRS", "GOES", "LANDSAT"]
    resolution_m: int
    temporal: Temporal = Field(default_factory=Temporal)
    columns: dict[str, SensorColumn] = Field(default_factory=dict)


def _parse_sensors(files: list[Path]) -> dict[str, Sensor]:
    """Parse the FIRMS catalog's `sensors:` block into validated rows.

    Args:
        files: The contributing YAML files (FIRMS ships a single file).

    Returns:
        dict[str, Sensor]: One row per FIRMS sensor code.

    Raises:
        ValueError: If the `sensors:` block is missing or empty, or a row
            fails :class:`Sensor` validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    sensors_yaml = data.get("sensors") or {}
    if not sensors_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'sensors:' block. "
            "The FIRMS catalog must list at least one sensor."
        )
    sensors: dict[str, Sensor] = {}
    for code, body in sensors_yaml.items():
        try:
            sensors[code] = Sensor(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} sensor {code!r} failed validation:\n{exc}"
            ) from exc
    return sensors


class Catalog(AbstractCatalog):
    """Sensor catalog for the NASA FIRMS backend.

    Reads the bundled `firms_data_catalog.yaml` (shipped as package
    data) and exposes its `sensors:` block as a map of :class:`Sensor`
    rows, keyed by FIRMS source code under the inherited :attr:`datasets`
    field. Instantiate with no arguments (`Catalog()`);
    :func:`model_post_init` loads and validates the YAML in one pass.
    Resolve a sensor with :meth:`get_sensor` (a thin alias over
    :meth:`~earthlens.base.AbstractCatalog.get_dataset`) and a single
    column with :meth:`get_column`.

    There is no `available_*` index — the listed sensors are the whole
    FIRMS universe (a deliberate deviation from the ECMWF/GEE catalogs,
    shared with GDACS/FDSN).

    Attributes:
        datasets: Map from the FIRMS source code to its :class:`Sensor`
            row.

    Examples:
        - List sensor codes and resolve one:
            ```python
            >>> from earthlens.firms import Catalog
            >>> cat = Catalog()
            >>> cat.codes()  # doctest: +NORMALIZE_WHITESPACE
            ['GOES_NRT', 'LANDSAT_NRT', 'MODIS_NRT', 'MODIS_SP',
             'VIIRS_NOAA20_NRT', 'VIIRS_NOAA20_SP', 'VIIRS_NOAA21_NRT',
             'VIIRS_SNPP_NRT', 'VIIRS_SNPP_SP']
            >>> cat.get_sensor("MODIS_NRT").family
            'MODIS'
            >>> "MODIS_NRT" in cat
            True

            ```
        - An unknown code raises with a did-you-mean hint:
            ```python
            >>> from earthlens.firms import Catalog
            >>> Catalog().get_sensor("MODIS_NR")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'MODIS_NR' is not in the FIRMS sensor catalog. Known sensors: [...]. Did you mean 'MODIS_NRT'?

            ```
    """

    _catalog_kind: str = "FIRMS sensor catalog"
    _entry_noun: str = "sensors"

    datasets: dict[str, Sensor] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {"datasets": loaded.datasets}

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the FIRMS sensor catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `sensors:` block, or a row fails :class:`Sensor` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        sensors = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_sensors, provider="FIRMS"
        )
        return cls(datasets=dict(sensors))

    def get_sensor(self, code: str) -> Sensor:
        """Return the :class:`Sensor` for `code`, with a did-you-mean hint.

        Thin alias over
        :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            code: A FIRMS source code (`"VIIRS_SNPP_NRT"`, `"MODIS_NRT"`,
                …).

        Returns:
            Sensor: The matching sensor row.

        Raises:
            ValueError: If `code` is not a registered FIRMS sensor.
        """
        return cast("Sensor", self.get_dataset(code))

    def get_column(self, code: str, column: str) -> SensorColumn:
        """Return one column's metadata for a `(sensor, column)` pair.

        Args:
            code: A FIRMS source code as it appears in :attr:`datasets`.
            column: A CSV column name declared under that sensor.

        Returns:
            SensorColumn: The matching column metadata.

        Raises:
            ValueError: If `code` is not a registered sensor.
            KeyError: If `column` is not declared under that sensor.

        Examples:
            - Read a column's units:
                ```python
                >>> from earthlens.firms import Catalog
                >>> Catalog().get_column("MODIS_NRT", "confidence").units
                '%'

                ```
        """
        return self.get_sensor(code).columns[column]

    def get_variable(self, code: str, column: str) -> SensorColumn:
        """Leaf accessor for the shared two-arg get_variable contract.

        Alias of :meth:`get_column` so the FIRMS leaf is reachable under
        the same `get_variable(dataset_key, variable_name)` verb the
        other two-level catalogs use.

        Args:
            code: A FIRMS source code.
            column: A CSV column name declared under that sensor.

        Returns:
            SensorColumn: The matching column metadata.
        """
        return self.get_column(code, column)

    def codes(self) -> list[str]:
        """Return the registered FIRMS sensor codes, sorted.

        Returns:
            list[str]: The sensor codes (`["MODIS_NRT", "MODIS_SP", ...]`).
        """
        return sorted(self.datasets)
