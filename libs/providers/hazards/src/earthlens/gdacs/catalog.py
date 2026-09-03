"""Hazard-type dispatch table for the GDACS multi-hazard backend.

GDACS is a single fixed query feed, not a curated dataset catalogue, so
this "catalog" is deliberately tiny: a six-row map from a GDACS
event-type code (`"EQ"`, `"TC"`, `"FL"`, `"VO"`, `"WF"`, `"DR"`) to a
little metadata. It mirrors `fdsn_data_catalog.yaml` / the FDSN
:class:`~earthlens.fdsn.catalog.Catalog`: there is no `available_*`
index (the six codes *are* the whole GDACS universe, so an "available
vs curated" split would just duplicate the map) and no
`refresh` / `probe` / `audit` tooling.

The per-event severity payload GDACS returns is uniform across hazard
types — every event carries the same flat
`{severity, severitytext, severityunit}` triple, where `severityunit`
already names the hazard-specific unit (`"M"` for an earthquake, wind
speed for a cyclone, …). There is therefore no per-hazard property
schema to model: a single `severity` / `severity_unit` / `severity_text`
column trio in :mod:`earthlens.gdacs.events` covers every hazard.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `gdacs_data_catalog.yaml` and exposes
each row as a :class:`HazardType`, keyed by code under the inherited
`datasets` field — which is what gives it the `cat["EQ"]` /
`"EQ" in cat` / `len(cat)` dict-like surface and the did-you-mean error
for free. :data:`CATALOG_PATH` is the path to the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "gdacs_data_catalog.yaml"

#: Module-level parse cache, keyed by `load_catalog` on the resolved path
#: plus each contributing file's `(mtime_ns, size)`, so a repeated
#: `Catalog()` skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class HazardType(BaseModel):
    """One GDACS hazard type's dispatch row.

    The GDACS event-type code is the parent key in
    :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        name: Human-readable hazard name (`"Earthquake"`,
            `"Tropical cyclone"`, …) used in logs and docs.
        description: Short note on what the hazard covers and what its
            severity value means.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.gdacs import HazardType
            >>> h = HazardType(name="Earthquake")
            >>> h.name
            'Earthquake'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""


def _parse_hazard_types(files: list[Path]) -> dict[str, HazardType]:
    """Parse the GDACS catalog's `hazard_types:` block into validated rows.

    Args:
        files: The contributing YAML files (GDACS ships a single file).

    Returns:
        dict[str, HazardType]: One row per GDACS event-type code.

    Raises:
        ValueError: If the `hazard_types:` block is missing or empty, or a
            row fails :class:`HazardType` validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    hazards_yaml = data.get("hazard_types") or {}
    if not hazards_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'hazard_types:' "
            "block. The GDACS catalog must list at least one hazard type."
        )
    hazards: dict[str, HazardType] = {}
    for code, body in hazards_yaml.items():
        try:
            hazards[code] = HazardType(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} hazard type {code!r} failed validation:\n{exc}"
            ) from exc
    return hazards


class Catalog(AbstractCatalog[HazardType]):
    """Hazard-type catalog for the GDACS backend.

    Reads the bundled `gdacs_data_catalog.yaml` (shipped as package
    data) and exposes its `hazard_types:` block as a map of
    :class:`HazardType` rows, keyed by GDACS event-type code under the
    inherited :attr:`datasets` field. Instantiate with no arguments
    (`Catalog()`); :func:`model_post_init` loads and validates the YAML
    in one pass. Resolve a hazard with :meth:`get_hazard` (a thin alias
    over :meth:`~earthlens.base.AbstractCatalog.get_dataset`).

    Attributes:
        datasets: Map from the GDACS event-type code to its
            :class:`HazardType` row.

    Examples:
        - List hazard codes and resolve one:
            ```python
            >>> from earthlens.gdacs import Catalog
            >>> cat = Catalog()
            >>> cat.codes()
            ['DR', 'EQ', 'FL', 'TC', 'VO', 'WF']
            >>> cat.get_hazard("EQ").name
            'Earthquake'
            >>> "EQ" in cat
            True

            ```
        - An unknown code raises with a did-you-mean hint:
            ```python
            >>> from earthlens.gdacs import Catalog
            >>> Catalog().get_hazard("EQK")
            Traceback (most recent call last):
                ...
            ValueError: 'EQK' is not in the GDACS hazard catalog. Known hazard types: ['DR', 'EQ', 'FL', 'TC', 'VO', 'WF']. Did you mean 'EQ'?

            ```
    """

    _catalog_kind: str = "GDACS hazard catalog"
    _entry_noun: str = "hazard types"

    datasets: dict[str, HazardType] = Field(default_factory=dict)

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
        """Read the GDACS hazard catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `hazard_types:` block, or a row fails :class:`HazardType`
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        hazards = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_hazard_types, provider="GDACS"
        )
        return cls(datasets=dict(hazards))

    def get_hazard(self, code: str) -> HazardType:
        """Return the :class:`HazardType` for `code`, with a did-you-mean hint.

        Thin alias over
        :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            code: A GDACS event-type code (`"EQ"`, `"TC"`, …).

        Returns:
            HazardType: The matching hazard row.

        Raises:
            ValueError: If `code` is not a registered hazard type.
        """
        return cast("HazardType", self.get_dataset(code))

    def codes(self) -> list[str]:
        """Return the registered GDACS hazard codes, sorted.

        Returns:
            list[str]: The hazard codes (`["DR", "EQ", "FL", ...]`).
        """
        return sorted(self.datasets)
