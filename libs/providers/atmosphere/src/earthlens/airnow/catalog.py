"""Pollutant dispatch table for the AirNow backend.

AirNow's `/aq/data/` endpoint filters on a comma list of `parameters`
codes (`OZONE`, `PM25`, `PM10`, `CO`, `NO2`, `SO2`), but users think in
names (`pm25`, `o3`). This module is the name-to-code bridge plus light
metadata (units, display name, group). Like the OpenAQ / FDSN provider
tables it is deliberately tiny and fixed — a parameter list, not a
dataset universe — so there is no `refresh` / `probe` / `audit` tooling
and no `tools/airnow/` directory; adding a pollutant later is a
hand-edit of one YAML row.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that
loads the bundled `airnow_data_catalog.yaml` and exposes each row as a
`Pollutant`. Resolve a single name with `Catalog.get_pollutant` (raises
with a did-you-mean hint on an unknown name) or a list of names to
their AirNow codes with `Catalog.codes_for`. `CATALOG_PATH` is the path
to the bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "airnow_data_catalog.yaml"

#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation. Mirrors
#: the OpenAQ / FDSN / NWP loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Pollutant]] = {}


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


#: The pollutant groups an AirNow `Pollutant` can belong to.
PollutantGroup = Literal["criteria", "particulate", "other"]


class Pollutant(BaseModel):
    """One AirNow pollutant's dispatch row.

    The user-facing name is the parent key in `Catalog.pollutants` and is
    also stored on the row as `name` so a resolved `Pollutant` is
    self-describing.

    Attributes:
        name: Short machine name (`"pm25"`, `"o3"`); matches the catalog
            key.
        code: The exact spelling AirNow's `parameters` query argument
            accepts (`"PM25"`, `"OZONE"`).
        units: The reporting unit AirNow uses for this pollutant
            (`"UG/M3"`, `"PPB"`). Informational — the backend carries the
            real per-row `Unit` from the response.
        display_name: Human-readable label for docs / plots (`"PM2.5"`,
            `"Ozone"`).
        group: Coarse classification — `"criteria"`, `"particulate"`, or
            `"other"`.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.airnow import Pollutant
            >>> p = Pollutant(name="pm25", code="PM25", units="UG/M3")
            >>> p.code
            'PM25'
            >>> p.group
            'other'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    code: str
    units: str = ""
    display_name: str = ""
    group: PollutantGroup = "other"


class Catalog(AbstractCatalog):
    """Pollutant catalog for the AirNow backend.

    Reads the bundled `airnow_data_catalog.yaml` (shipped as package
    data) and exposes its `pollutants:` block as a map of `Pollutant`
    rows. Instantiate with no arguments (`Catalog()`); `model_post_init`
    loads and validates the YAML in one pass. Resolve a name with
    `get_pollutant` or a list of names to codes with `codes_for`.

    Attributes:
        pollutants: Map from the user-facing pollutant name to its
            `Pollutant` dispatch row.

    Examples:
        - List pollutants and resolve names to AirNow codes:
            ```python
            >>> from earthlens.airnow import Catalog
            >>> cat = Catalog()
            >>> cat.get_pollutant("pm25").code
            'PM25'
            >>> cat.codes_for(["pm25", "o3"])
            ['PM25', 'OZONE']

            ```
    """

    _catalog_kind: str = "AirNow pollutant catalog"
    _entry_noun: str = "pollutants"

    #: The pollutant rows live in the base `datasets` field so the inherited
    #: dict surface (`len`, `in`, `[]`, iteration) and `get_dataset`'s
    #: did-you-mean hint work unchanged. `pollutants` is the domain-named
    #: read alias.
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

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no pollutants were supplied.

        `Catalog()` with no args reads `CATALOG_PATH`; passing
        `pollutants=...` (or `datasets=...`) skips the disk read (used in
        tests).

        Raises:
            ValueError: Propagated from `load` when the YAML is missing,
                empty, or has a malformed pollutant row.
        """
        if not self.datasets:
            self.datasets = Catalog.load().datasets
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the AirNow pollutant catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If the file has no `pollutants:` block, or a row
                fails `Pollutant` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        resolved = str(catalog_path.resolve())
        try:
            mtime = catalog_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = 0
        key = (resolved, mtime)
        cached = _CATALOG_CACHE.get(key)
        if cached is not None:
            return cls(datasets=dict(cached))
        data = load_yaml_strict(catalog_path) or {}
        pollutants_yaml = data.get("pollutants") or {}
        if not pollutants_yaml:
            raise ValueError(
                f"{catalog_path} is missing or has an empty 'pollutants:' "
                "block. The AirNow catalog must list at least one pollutant."
            )
        pollutants: dict[str, Pollutant] = {}
        for name, body in pollutants_yaml.items():
            try:
                pollutants[name] = Pollutant(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{catalog_path} pollutant {name!r} failed validation:\n{exc}"
                ) from exc
        _CATALOG_CACHE[key] = pollutants
        return cls(datasets=dict(pollutants))

    def get_catalog(self) -> dict[str, Pollutant]:
        """Return the pollutant map (satisfies the abstract contract).

        Returns:
            dict[str, Pollutant]: Same object as `datasets` /
                `pollutants`.
        """
        return self.datasets

    def get_pollutant(self, name: str) -> Pollutant:
        """Resolve a pollutant name to its `Pollutant` row.

        Thin wrapper over the inherited `get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown name.

        Args:
            name: A user-facing pollutant name (`"pm25"`, `"o3"`).

        Returns:
            Pollutant: The matching dispatch row.

        Raises:
            ValueError: If `name` is not a known pollutant; the message
                names the catalog kind and, when a close match exists,
                adds a did-you-mean hint.
        """
        return cast("Pollutant", self.get_dataset(name))

    def codes_for(self, names: list[str]) -> list[str]:
        """Resolve names to their AirNow `parameters` codes, order-stable.

        Each name contributes its `Pollutant.code`; duplicates are dropped
        (first occurrence wins).

        Args:
            names: User-facing pollutant names to resolve.

        Returns:
            list[str]: The AirNow codes for `names`, de-duplicated,
                order-stable.

        Raises:
            ValueError: If any name is unknown (via `get_pollutant`).
        """
        codes: list[str] = []
        for name in names:
            code = self.get_pollutant(name).code
            if code not in codes:
                codes.append(code)
        return codes
