"""Pollutant dispatch table for the EEA (`eea_aq`) backend.

The EEA download service (wrapped by `airbase`) filters by pollutant
*notation* — `poll="PM2.5"` — while the downloaded Parquet identifies
each row's pollutant by a numeric EEA vocabulary code (`6001`). Users
pass earthlens pollutant names in `variables=[...]`; this module is the
two-way bridge: name -> `poll` notation (for the request) and code ->
name (to label the returned rows).

Like the OpenAQ / AirNow provider tables it is deliberately tiny and
fixed — a six-row parameter list, not a dataset universe — so there is
no `refresh` / `probe` / `audit` tooling and no `tools/eea_aq/`
directory; adding a pollutant later is a hand-edit of one YAML row.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads
the bundled `eea_aq_data_catalog.yaml` and exposes each row as a
`Pollutant`. Resolve a single name with `Catalog.get_pollutant` (raises
with a did-you-mean hint on an unknown name), a list of names to airbase
`poll` codes with `Catalog.polls_for`, or the code -> name reverse map
with `Catalog.code_to_name`. `CATALOG_PATH` is the path to the bundled
YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "eea_aq_data_catalog.yaml"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path
#: plus each contributing file's `(mtime_ns, size)`, so a repeated
#: `Catalog()` skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


#: The pollutant groups an EEA `Pollutant` can belong to.
PollutantGroup = Literal["criteria", "particulate", "other"]


class Pollutant(BaseModel):
    """One EEA pollutant's dispatch row.

    The user-facing name is the parent key in `Catalog.pollutants` and is
    also stored on the row as `name` so a resolved `Pollutant` is
    self-describing.

    Attributes:
        name: Short machine name (`"pm25"`, `"o3"`); matches the catalog
            key.
        poll: The pollutant notation airbase's `poll=` argument accepts
            (`"PM2.5"`, `"O3"`).
        code: The numeric EEA vocabulary code the downloaded Parquet uses
            in its `Pollutant` column (`6001` for PM2.5), used to label
            rows back to `name`.
        units: The reporting unit the EEA uses for this pollutant
            (`"ug.m-3"`, `"mg.m-3"`). Informational — the backend carries
            the real per-row `Unit` from the Parquet.
        display_name: Human-readable label for docs / plots (`"PM2.5"`,
            `"Ozone"`).
        group: Coarse classification — `"criteria"`, `"particulate"`, or
            `"other"`.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.eea_aq import Pollutant
            >>> p = Pollutant(name="pm25", poll="PM2.5", code=6001)
            >>> (p.poll, p.code)
            ('PM2.5', 6001)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    poll: str
    code: int
    units: str = ""
    display_name: str = ""
    group: PollutantGroup = "other"


def _parse_eea_aq_catalog(files: list[Path]):
    """Parse and validate the EEA-AQ catalog rows.

    Args:
        files: The contributing YAML files (EEA-AQ ships a single file).

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
            "block. The EEA catalog must list at least one pollutant."
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


class Catalog(AbstractCatalog):
    """Pollutant catalog for the EEA (`eea_aq`) backend.

    Reads the bundled `eea_aq_data_catalog.yaml` (shipped as package data)
    and exposes its `pollutants:` block as a map of `Pollutant` rows.
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads
    and validates the YAML in one pass. Resolve a name with
    `get_pollutant`, names to airbase `poll` codes with `polls_for`, or
    the numeric-code reverse map with `code_to_name`.

    Attributes:
        pollutants: Map from the user-facing pollutant name to its
            `Pollutant` dispatch row.

    Examples:
        - Resolve names to airbase `poll` codes and back from codes:
            ```python
            >>> from earthlens.eea_aq import Catalog
            >>> cat = Catalog()
            >>> cat.polls_for(["pm25", "o3"])
            ['PM2.5', 'O3']
            >>> cat.code_to_name()[6001]
            'pm25'

            ```
    """

    _catalog_kind: str = "EEA pollutant catalog"
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
        """Read the EEA pollutant catalog from disk.

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
            catalog_path, _CATALOG_CACHE, _parse_eea_aq_catalog, provider="EEA-AQ"
        )
        return cls(datasets=dict(cached))

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

    def polls_for(self, names: list[str]) -> list[str]:
        """Resolve names to their airbase `poll` notations, order-stable.

        Each name contributes its `Pollutant.poll`; duplicates are dropped
        (first occurrence wins).

        Args:
            names: User-facing pollutant names to resolve.

        Returns:
            list[str]: The airbase `poll` notations for `names`,
                de-duplicated, order-stable.

        Raises:
            ValueError: If any name is unknown (via `get_pollutant`).
        """
        polls: list[str] = []
        for name in names:
            poll = self.get_pollutant(name).poll
            if poll not in polls:
                polls.append(poll)
        return polls

    def code_to_name(self) -> dict[int, str]:
        """Return the numeric EEA code -> pollutant name reverse map.

        Used to label the downloaded Parquet's numeric `Pollutant` column
        back to the user-facing name.

        Returns:
            dict[int, str]: Every row's `code` mapped to its `name`.
        """
        return {row.code: row.name for row in self.datasets.values()}
