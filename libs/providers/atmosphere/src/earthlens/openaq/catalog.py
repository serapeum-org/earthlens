"""Pollutant-parameter dispatch table for the OpenAQ backend.

OpenAQ filters its `locations` query by integer `parameters_id`, but
users think in names (`pm25`, `no2`). This module is the name-to-id
bridge plus light metadata (units, display name, group). Like the
FDSN provider table, it is deliberately tiny and fixed — a parameter
list, not a dataset universe — so there is no `refresh` / `probe` /
`audit` tooling and no `tools/openaq/` directory; adding a parameter
later is a hand-edit of one YAML row.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `openaq_data_catalog.yaml` and exposes
each row as a :class:`Parameter`. Resolve a single name with
:meth:`Catalog.get_parameter` (raises with a did-you-mean hint on an
unknown name) or a list of names to their ids with
:meth:`Catalog.ids_for`. :data:`CATALOG_PATH` is the path to the
bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "openaq_data_catalog.yaml"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path
#: plus each contributing file's `(mtime_ns, size)`, so a repeated
#: `Catalog()` skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


#: The pollutant groups a `Parameter` can belong to.
ParameterGroup = Literal["criteria", "particulate", "meteorological", "other"]


class Parameter(BaseModel):
    """One OpenAQ pollutant parameter's dispatch row.

    The user-facing name is the parent key in
    :attr:`Catalog.parameters` and is also stored on the row as
    :attr:`name` so a resolved :class:`Parameter` is self-describing.

    OpenAQ assigns a **separate** `parameters_id` per reporting unit, so
    one pollutant name maps to several ids (`no2` is 5=µg/m³, 7=ppm,
    15=ppb). :attr:`ids` holds them all; the backend matches sensors by
    name (stable across units) and uses the id list only as a
    server-side narrowing hint.

    Attributes:
        name: Short machine name (`"pm25"`, `"no2"`); matches the
            catalog key.
        ids: All OpenAQ numeric `parameters_id`s that share this name —
            one per reporting unit. At least one.
        units: The reporting units across :attr:`ids` (`["ppb", "ppm",
            "µg/m³"]`). Informational — the backend reads the real
            per-sensor unit at fetch time.
        display_name: Human-readable label for docs / plots
            (`"PM2.5"`, `"Nitrogen dioxide"`).
        group: Coarse classification — `"criteria"` (criteria
            pollutant), `"particulate"`, `"meteorological"`, or
            `"other"`.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.openaq import Parameter
            >>> p = Parameter(name="no2", ids=[5, 7, 15], units=["µg/m³", "ppm", "ppb"])
            >>> p.ids
            [5, 7, 15]
            >>> p.group
            'other'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ids: list[int] = Field(min_length=1)
    units: list[str] = Field(default_factory=list)
    display_name: str = ""
    group: ParameterGroup = "other"


def _parse_parameters(files: list[Path]) -> dict[str, Parameter]:
    """Parse the OpenAQ catalog's `parameters:` block into validated rows.

    Args:
        files: The contributing YAML files (OpenAQ ships a single file).

    Returns:
        dict[str, Parameter]: One row per measured parameter name.

    Raises:
        ValueError: If the `parameters:` block is missing or empty, or a row
            fails :class:`Parameter` validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    parameters_yaml = data.get("parameters") or {}
    if not parameters_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'parameters:' "
            "block. The OpenAQ catalog must list at least one parameter."
        )
    parameters: dict[str, Parameter] = {}
    for name, body in parameters_yaml.items():
        try:
            parameters[name] = Parameter(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} parameter {name!r} failed validation:\n{exc}"
            ) from exc
    return parameters


class Catalog(AbstractCatalog):
    """Pollutant-parameter catalog for the OpenAQ backend.

    Reads the bundled `openaq_data_catalog.yaml` (shipped as package
    data) and exposes its `parameters:` block as a map of
    :class:`Parameter` rows. Instantiate with no arguments
    (`Catalog()`); :func:`model_post_init` loads and validates the
    YAML in one pass. Resolve a name with :meth:`get_parameter` or a
    list of names to ids with :meth:`ids_for`.

    Attributes:
        parameters: Map from the user-facing parameter name to its
            :class:`Parameter` dispatch row.

    Examples:
        - List parameters and resolve names to OpenAQ ids:
            ```python
            >>> from earthlens.openaq import Catalog
            >>> cat = Catalog()
            >>> cat.get_parameter("pm25").ids
            [2]
            >>> cat.ids_for(["pm25", "no2"])
            [2, 5, 7, 15]

            ```
        - An unknown but close name raises with a did-you-mean hint:
            ```python
            >>> from earthlens.openaq import Catalog
            >>> Catalog().get_parameter("pm2.5")
            Traceback (most recent call last):
                ...
            ValueError: 'pm2.5' is not in the OpenAQ parameter catalog. Known parameters: [...]. Did you mean 'pm25'?

            ```
    """

    _catalog_kind: str = "OpenAQ parameter catalog"
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
                >>> from earthlens.openaq import Catalog
                >>> cat = Catalog()
                >>> cat.parameters is cat.datasets
                True
                >>> cat.parameters["pm25"].ids
                [2]

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
        """Read the OpenAQ parameter catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `parameters:` block, or a row fails :class:`Parameter`
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        parameters = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_parameters, provider="OpenAQ"
        )
        return cls(datasets=dict(parameters))

    def get_parameter(self, name: str) -> Parameter:
        """Resolve a pollutant name to its :class:`Parameter` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises
        a `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            name: A user-facing parameter name (`"pm25"`, `"no2"`).

        Returns:
            Parameter: The matching dispatch row.

        Raises:
            ValueError: If `name` is not a known parameter; the
                message names the catalog kind and, when a close match
                exists, adds a did-you-mean hint.
        """
        return cast("Parameter", self.get_dataset(name))

    def ids_for(self, names: list[str]) -> list[int]:
        """Resolve names to the union of their OpenAQ ids (all unit variants).

        Each name contributes every id in its :attr:`Parameter.ids`
        (one per reporting unit), so a request for `"no2"` yields all of
        `no2`'s ids regardless of the unit a station reports in. Order
        follows `names` then each parameter's own id order; duplicates
        across names are dropped (first occurrence wins).

        Args:
            names: User-facing parameter names to resolve.

        Returns:
            list[int]: The union of OpenAQ `parameters_id`s across
                `names`, de-duplicated, order-stable.

        Raises:
            ValueError: If any name is unknown (via
                :meth:`get_parameter`).
        """
        ids: list[int] = []
        for name in names:
            for pid in self.get_parameter(name).ids:
                if pid not in ids:
                    ids.append(pid)
        return ids
