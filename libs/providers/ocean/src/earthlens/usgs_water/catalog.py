"""Parameter-code catalog for the USGS Water backend.

USGS NWIS filters its queries by 5-digit **parameter codes** (`00060`
discharge, `00065` gage height, `00010` water temperature, …), but
users think in names. This module is the name-to-code bridge plus light
metadata (units, display name, parameter group, and which services the
code is valid on).

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `usgs_water_data_catalog.yaml` and
exposes each row as a :class:`Parameter`. Resolve a single key with
:meth:`Catalog.resolve` — which accepts either a friendly name
(`"discharge"`) **or** a raw 5-digit code (`"00060"`, passed through
unmapped), with a did-you-mean hint on an unknown name. The full ~25k
USGS parameter-code table is *not* hand-curated here; the curated rows
cover the common codes, and the refresh tool builds an informational
`available_parameters` index from the live USGS reference table.

:data:`CATALOG_PATH` is the path to the bundled YAML.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "usgs_water_data_catalog.yaml"

#: Module-level cache of parsed catalog rows, keyed on the resolved path
#: plus the YAML's `st_mtime_ns`, so editing the file invalidates the
#: entry without re-parsing on every `Catalog()`. Mirrors the
#: `_CATALOG_CACHE` pattern in the GEE / ECMWF / CMEMS / FDSN loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Parameter]] = CatalogParseCache()

#: A 5-digit NWIS parameter code (the raw form `resolve` passes through).
_CODE_RE = re.compile(r"^\d{5}$")


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force
    a re-parse. Production callers do not need this — the cache key
    includes the file's `st_mtime_ns`, so any real edit invalidates the
    entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Parameter]:
    """Parse, validate, and cache the parameter catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        Mapping from friendly parameter name to its :class:`Parameter`.

    Raises:
        ValueError: If the file has no `parameters:` block, or a row
            fails :class:`Parameter` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    parameters_yaml = data.get("parameters") or {}
    if not parameters_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'parameters:' block. "
            "The USGS Water catalog must list at least one parameter."
        )
    rows: dict[str, Parameter] = {}
    for name, body in parameters_yaml.items():
        try:
            rows[name] = Parameter(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} parameter {name!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = rows
    return rows


#: The coarse USGS parameter groups a `Parameter` can belong to.
ParameterGroup = Literal[
    "Physical",
    "Inorganics",
    "Nutrients",
    "Biological",
    "Organics",
    "Information",
    "Other",
]


class Parameter(BaseModel):
    """One NWIS parameter code's catalog row.

    The user-facing name is the parent key in
    :attr:`Catalog.parameters` and is also stored on the row as
    :attr:`name` so a resolved :class:`Parameter` is self-describing.

    Attributes:
        code: The 5-digit NWIS parameter code (`"00060"`).
        name: Human-readable label (`"Discharge"`).
        units: The reporting units (`"ft3/s"`, `"degC"`).
        group: Coarse USGS classification (`"Physical"`, `"Nutrients"`,
            …).
        services: The :data:`earthlens.usgs_water.backend.SERVICES`
            this code is typically available on (`["daily",
            "instantaneous"]`).

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.usgs_water import Parameter
            >>> p = Parameter(code="00060", name="Discharge", units="ft3/s")
            >>> p.code
            '00060'
            >>> p.group
            'Physical'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^\d{5}$")
    name: str = ""
    units: str = ""
    group: ParameterGroup = "Physical"
    services: list[str] = Field(default_factory=list)


class Catalog(AbstractCatalog):
    """Parameter-code catalog for the USGS Water backend.

    Reads the bundled `usgs_water_data_catalog.yaml` (shipped as
    package data) and exposes its `parameters:` block as a map of
    :class:`Parameter` rows keyed by friendly name. Instantiate with no
    arguments (`Catalog()`). Resolve a name or raw code with
    :meth:`resolve`, or a single row with :meth:`get_parameter`.

    Attributes:
        parameters: Map from the friendly parameter name to its
            :class:`Parameter` row.

    Examples:
        - Resolve a friendly name and a raw code:
            ```python
            >>> from earthlens.usgs_water import Catalog
            >>> cat = Catalog()
            >>> cat.resolve("discharge")
            '00060'
            >>> cat.resolve("00060")
            '00060'

            ```
        - An unknown but close name raises with a did-you-mean hint:
            ```python
            >>> from earthlens.usgs_water import Catalog
            >>> Catalog().resolve("dischrge")
            Traceback (most recent call last):
                ...
            ValueError: 'dischrge' is not in the USGS Water parameter catalog. Known parameters: [...]. Did you mean 'discharge'?

            ```
    """

    _catalog_kind: str = "USGS Water parameter catalog"
    _entry_noun: str = "parameters"

    #: The parameter rows live in the base :attr:`datasets` field so the
    #: inherited dict surface (`len`, `in`, `[]`, iteration) and
    #: :meth:`get_dataset`'s did-you-mean hint work unchanged. The field
    #: is narrowed here to :class:`Parameter` values; :attr:`parameters`
    #: is the domain-named read alias.
    datasets: dict[str, Parameter] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_parameters_alias(cls, data: Any) -> Any:
        """Accept the legacy `parameters=` kwarg as an alias for `datasets`.

        Older callers (and tests) construct `Catalog(parameters={...})`.
        The rows now live in the base `datasets` field, so rewrite that
        key on the way in. An explicit `datasets=` always wins.

        Args:
            data: The raw model input (a mapping when constructed with
                keyword arguments).

        Returns:
            The input with `parameters` renamed to `datasets`, untouched
            otherwise.
        """
        if isinstance(data, dict) and "parameters" in data and "datasets" not in data:
            data = dict(data)
            data["datasets"] = data.pop("parameters")
        return data

    @property
    def parameters(self) -> dict[str, Parameter]:
        """The parameter map — alias for the base :attr:`datasets` field.

        Returns:
            dict[str, Parameter]: The same mapping stored in
                :attr:`datasets`.

        Examples:
            - The alias and the base field are the same object:
                ```python
                >>> from earthlens.usgs_water import Catalog
                >>> cat = Catalog()
                >>> cat.parameters is cat.datasets
                True
                >>> cat.parameters["discharge"].code
                '00060'

                ```
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
        """Read the USGS Water parameter catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `parameters:` block, or a row
                fails :class:`Parameter` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(datasets=dict(_load_catalog_data(catalog_path)))

    @property
    def available_parameters(self) -> list[str]:
        """The sorted list of curated friendly parameter names.

        Returns:
            list[str]: Every curated catalog key, sorted.

        Examples:
            - List the curated names and check membership:
                ```python
                >>> from earthlens.usgs_water import Catalog
                >>> names = Catalog().available_parameters
                >>> "discharge" in names
                True
                >>> names == sorted(names)
                True

                ```
        """
        return sorted(self.datasets)

    def get_parameter(self, name: str) -> Parameter:
        """Resolve a friendly name to its :class:`Parameter` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises
        a `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            name: A friendly parameter name (`"discharge"`).

        Returns:
            Parameter: The matching catalog row.

        Raises:
            ValueError: If `name` is not a known parameter; the message
                names the catalog kind and, when a close match exists,
                adds a did-you-mean hint.

        Examples:
            - Resolve a row and read its fields:
                ```python
                >>> from earthlens.usgs_water import Catalog
                >>> p = Catalog().get_parameter("gage_height")
                >>> p.code
                '00065'
                >>> p.units
                'ft'

                ```
        """
        return cast("Parameter", self.get_dataset(name))

    def resolve(self, code_or_name: str) -> str:
        """Resolve a friendly name or a raw 5-digit code to a code.

        A raw 5-digit code (`"00060"`) passes through unmapped — the
        full USGS code table is far larger than the curated catalog, so
        any valid code is accepted directly. A non-code string is
        looked up as a friendly name via :meth:`get_parameter`.

        Args:
            code_or_name: A friendly name (`"discharge"`) or a raw
                5-digit NWIS code (`"00060"`).

        Returns:
            str: The 5-digit parameter code.

        Raises:
            ValueError: If `code_or_name` is neither a 5-digit code nor
                a known friendly name (with a did-you-mean hint).
        """
        if _CODE_RE.match(code_or_name):
            return code_or_name
        return self.get_parameter(code_or_name).code
