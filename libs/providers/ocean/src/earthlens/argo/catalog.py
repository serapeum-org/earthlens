"""Parameter-family catalog for the Argo float backend.

Argo measurements come in two `argopy` dataset families — `"phy"` (core
physical temperature / salinity / pressure) and `"bgc"` (biogeochemical
parameters). This module is the curated vocabulary of the canonical Argo
parameter names per family, used to validate a request's `variables=`
against the chosen `dataset=` family with a did-you-mean hint.

`Catalog` is a thin `earthlens.base.AbstractCatalog`
subclass that loads the bundled `argo_data_catalog.yaml` and exposes each
family as a `Family` row (keyed by family name under the inherited
`datasets` field, which gives it the `cat["phy"]` / `"phy" in cat` /
`len(cat)` dict-like surface). `CATALOG_PATH` is the path to the
bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, cast

from pydantic import ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog, SummarisedLeaf
from earthlens.base.catalog_source import load_catalog, row_fields_with_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "argo_data_catalog.yaml"

#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation.
#: Mirrors the FDSN / GDACS / usgs_water loaders.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class Family(SummarisedLeaf):
    """One Argo dataset family's parameter vocabulary.

    The family key (`"phy"` / `"bgc"`) is the parent key in
    `Catalog.datasets`; the loader copies it onto the row as
    `name` so a resolved family is self-describing.

    Attributes:
        name: The family's catalog key, injected by the loader; the row
            carries no other identifier. A body declaring a different value is
            rejected at load time, and the field is excluded from
            `model_dump()` so it does not repeat the row's own key.
        description: Short note on what the family covers.
        parameters: Map from canonical Argo parameter name (`"TEMP"`,
            `"DOXY"`) to its reporting units (`"degC"`).

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.argo import Family
            >>> f = Family(parameters={"TEMP": "degC", "PSAL": "psu"})
            >>> sorted(f.parameters)
            ['PSAL', 'TEMP']

            ```
    """

    _summary_fields = (
        "name",
        "description",
    )

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        default="", exclude=True
    )  # mirrors the catalog key; not part of the row's data
    description: str = ""
    parameters: dict[str, str] = Field(default_factory=dict)


def _parse_argo_catalog(files: list[Path]):
    """Parse and validate the Argo catalog rows.

    Args:
        files: The contributing YAML files (Argo ships a single file).

    Returns:
        The validated rows, in the shape the catalog caches.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    families_yaml = data.get("families") or {}
    if not families_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'families:' block. "
            "The Argo catalog must list at least one parameter family."
        )
    families: dict[str, Family] = {}
    for name, body in families_yaml.items():
        try:
            families[name] = Family(
                **row_fields_with_key(
                    body, "name", name, noun="family", source=catalog_path
                )
            )
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} family {name!r} failed validation:\n{exc}"
            ) from exc
    return families


class Catalog(AbstractCatalog[Family]):
    """Parameter-family catalog for the Argo backend.

    Reads the bundled `argo_data_catalog.yaml` (shipped as package data)
    and exposes its `families:` block as a map of `Family` rows
    keyed by family name under the inherited `datasets` field.
    Instantiate with no arguments (`Catalog()`); `model_post_init`
    loads and validates the YAML in one pass.

    Attributes:
        datasets: Map from the family name (`"phy"` / `"bgc"`) to its
            `Family` row.

    Examples:
        - List families and a family's parameters:
            ```python
            >>> from earthlens.argo import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.datasets)
            ['bgc', 'phy']
            >>> "TEMP" in cat.parameters_for("phy")
            True

            ```
        - An unknown parameter raises with a did-you-mean hint:
            ```python
            >>> from earthlens.argo import Catalog
            >>> Catalog().validate_parameters(["TEMPP"], "phy")
            Traceback (most recent call last):
                ...
            ValueError: 'TEMPP' is not an Argo 'phy' parameter. Known: ['PRES', 'PSAL', 'TEMP']. Did you mean 'TEMP'?

            ```
    """

    _catalog_kind: str = "Argo parameter catalog"
    _entry_noun: str = "families"

    datasets: dict[str, Family] = Field(default_factory=dict)

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
        """Read the Argo parameter catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `families:` block, or a row fails `Family` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        cached = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_argo_catalog, provider="Argo"
        )
        return cls(datasets=dict(cached))

    def get_family(self, name: str) -> Family:
        """Return the `Family` for `name`, with a did-you-mean hint.

        Thin alias over
        `get_dataset`.

        Args:
            name: A family name (`"phy"` / `"bgc"`).

        Returns:
            Family: The matching family row.

        Raises:
            ValueError: If `name` is not a registered family.
        """
        return cast("Family", self.get_dataset(name))

    def parameters_for(self, family: str) -> set[str]:
        """Return the valid parameter names for a family.

        Args:
            family: A family name (`"phy"` / `"bgc"`).

        Returns:
            set[str]: The canonical parameter names in that family.

        Raises:
            ValueError: If `family` is not a registered family.
        """
        return set(self.get_family(family).parameters)

    def validate_parameters(self, names: list[str], family: str) -> None:
        """Validate parameter `names` against a family, did-you-mean on miss.

        Args:
            names: Candidate parameter names (`["TEMP", "PSAL"]`).
            family: The family to validate against (`"phy"` / `"bgc"`).

        Raises:
            ValueError: If `family` is unknown, or any name is not one of
                the family's parameters (the message lists the known
                parameters and the closest match).
        """
        known = self.parameters_for(family)
        for name in names:
            if name not in known:
                close = difflib.get_close_matches(name, known, n=1)
                hint = f" Did you mean {close[0]!r}?" if close else ""
                raise ValueError(
                    f"{name!r} is not an Argo {family!r} parameter. "
                    f"Known: {sorted(known)}.{hint}"
                )
