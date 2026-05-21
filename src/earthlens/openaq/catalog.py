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

import difflib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "openaq_data_catalog.yaml"

#: The pollutant groups a `Parameter` can belong to.
ParameterGroup = Literal["criteria", "particulate", "meteorological", "other"]


class Parameter(BaseModel):
    """One OpenAQ pollutant parameter's dispatch row.

    The user-facing name is the parent key in
    :attr:`Catalog.parameters` and is also stored on the row as
    :attr:`name` so a resolved :class:`Parameter` is self-describing.

    Attributes:
        id: OpenAQ numeric `parameters_id` — the value the v3
            `locations` / `measurements` endpoints filter on.
        name: Short machine name (`"pm25"`, `"no2"`); matches the
            catalog key.
        units: Reporting units (`"µg/m³"`, `"ppm"`).
        display_name: Human-readable label for docs / plots
            (`"PM2.5"`, `"Nitrogen dioxide"`).
        group: Coarse classification — `"criteria"` (criteria
            pollutant), `"particulate"`, `"meteorological"`, or
            `"other"`.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.openaq import Parameter
            >>> p = Parameter(id=2, name="pm25", units="µg/m³")
            >>> p.id, p.group
            (2, 'other')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    units: str
    display_name: str = ""
    group: ParameterGroup = "other"


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
        - List parameters and resolve one to its OpenAQ id:
            ```python
            >>> from earthlens.openaq import Catalog
            >>> cat = Catalog()
            >>> cat.get_parameter("pm25").id
            2
            >>> cat.ids_for(["pm25", "no2"])
            [2, 5]

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

    parameters: dict[str, Parameter] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no parameters were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH`; passing
        `parameters=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed parameter row.
        """
        if self.parameters:
            return
        loaded = Catalog.load()
        self.parameters = loaded.parameters

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the OpenAQ parameter catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `parameters:` block, or a
                row fails :class:`Parameter` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
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
        return cls(parameters=parameters)

    def get_catalog(self) -> dict[str, Parameter]:
        """Return the parameter map (satisfies the abstract contract).

        Returns:
            dict[str, Parameter]: Same object as :attr:`parameters`.
        """
        return self.parameters

    def get_parameter(self, name: str) -> Parameter:
        """Resolve a pollutant name to its :class:`Parameter` row.

        Args:
            name: A user-facing parameter name (`"pm25"`, `"no2"`).

        Returns:
            Parameter: The matching dispatch row.

        Raises:
            ValueError: If `name` is not a known parameter; the
                message lists the known names and, when a close match
                exists, a did-you-mean hint.
        """
        try:
            return self.parameters[name]
        except KeyError:
            close = difflib.get_close_matches(name, self.parameters, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{name!r} is not in the {self._catalog_kind}. "
                f"Known parameters: {sorted(self.parameters)}.{hint}"
            ) from None

    def ids_for(self, names: list[str]) -> list[int]:
        """Resolve a list of parameter names to their OpenAQ ids, in order.

        Args:
            names: User-facing parameter names to resolve.

        Returns:
            list[int]: The OpenAQ `parameters_id` for each name, in the
                same order as `names`.

        Raises:
            ValueError: If any name is unknown (via
                :meth:`get_parameter`).
        """
        return [self.get_parameter(name).id for name in names]
