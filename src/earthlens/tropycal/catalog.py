"""Basin -> track-field catalog for the Tropycal cyclone-track backend.

Tropycal's `tracks.TrackDataset` is keyed on `(basin, source)`, so this
catalog plays the ECMWF/GEE "dataset -> variable" role with **basin** as
the dataset analog and the per-fix **track fields** (`vmax` / `mslp` /
`category`) as the variable analog. Unlike ECMWF, the field set is
uniform across basins — tropycal's `Storm.to_dataframe()` emits the same
columns for every basin — so the catalog's value is recording the valid
basin codes and which `source`s serve each one.

`Basin` is the "Dataset" analog (`name`, `sources`, `fields`);
`TrackField` is the "Variable" analog (`units`, `long_name`).
:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `tropycal_data_catalog.yaml` and stores
the basin map under the inherited `datasets` field — which gives it the
`cat["north_atlantic"]` / `"north_atlantic" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free.

There is deliberately no `available_*` index: the basins *are* the whole
tropycal universe (a documented deviation, mirroring GDACS).
:data:`CATALOG_PATH` is the path to the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "tropycal_data_catalog.yaml"


class TrackField(BaseModel):
    """One per-fix track field (the "Variable" analog).

    The field's short code is the parent key in :attr:`Basin.fields` and
    is not stored on the row.

    Attributes:
        units: Unit string for the field (`"kt"`, `"hPa"`, `"1"`).
        long_name: Human-readable description used in docs.

    Examples:
        - Build a field directly:
            ```python
            >>> from earthlens.tropycal import TrackField
            >>> f = TrackField(units="kt", long_name="Maximum sustained wind")
            >>> f.units
            'kt'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str
    long_name: str = ""


class Basin(BaseModel):
    """One ocean basin's catalog row (the "Dataset" analog).

    The basin code is the parent key in :attr:`Catalog.datasets` and is
    not stored on the row.

    Attributes:
        name: Human-readable basin name (`"North Atlantic"`, …).
        sources: tropycal data sources that serve this basin — a subset
            of `["ibtracs", "hurdat"]`. HURDAT2 covers only the North
            Atlantic / East Pacific (and the `both` aggregate); IBTrACS
            covers every basin.
        fields: Per-fix track fields available for the basin, keyed by
            short code (`"vmax"`, `"mslp"`, `"category"`).

    Examples:
        - Inspect a basin's sources and fields:
            ```python
            >>> from earthlens.tropycal import Catalog
            >>> na = Catalog().get_basin("north_atlantic")
            >>> na.sources
            ['ibtracs', 'hurdat']
            >>> "vmax" in na.fields
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sources: list[str] = Field(default_factory=list)
    fields: dict[str, TrackField] = Field(default_factory=dict)


class Catalog(AbstractCatalog):
    """Basin -> track-field catalog for the Tropycal backend.

    Reads the bundled `tropycal_data_catalog.yaml` (shipped as package
    data) and exposes its `basins:` block as a map of :class:`Basin`
    rows, keyed by basin code under the inherited :attr:`datasets`
    field. Instantiate with no arguments (`Catalog()`);
    :func:`model_post_init` loads and validates the YAML in one pass.
    Resolve a basin with :meth:`get_basin` (a thin alias over
    :meth:`~earthlens.base.AbstractCatalog.get_dataset`) and a single
    track field with :meth:`get_field`.

    Attributes:
        datasets: Map from the basin code to its :class:`Basin` row.

    Examples:
        - List basin codes and resolve one:
            ```python
            >>> from earthlens.tropycal import Catalog
            >>> cat = Catalog()
            >>> "north_atlantic" in cat
            True
            >>> cat.get_basin("north_atlantic").name
            'North Atlantic'
            >>> cat.sources_for("west_pacific")
            ['ibtracs']

            ```
        - An unknown basin raises with a did-you-mean hint:
            ```python
            >>> from earthlens.tropycal import Catalog
            >>> Catalog().get_basin("north_altantic")
            Traceback (most recent call last):
                ...
            ValueError: 'north_altantic' is not in the Tropycal basin catalog. Known datasets: ['all', 'australia', 'both', 'east_pacific', 'north_atlantic', 'north_indian', 'south_atlantic', 'south_indian', 'south_pacific', 'west_pacific']. Did you mean 'north_atlantic'?

            ```
    """

    _catalog_kind: str = "Tropycal basin catalog"

    datasets: dict[str, Basin] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no basins were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH`; passing
        `datasets=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed basin row.
        """
        if self.datasets:
            return
        loaded = Catalog.load()
        self.datasets = loaded.datasets

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Tropycal basin catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `basins:` block, or a row
                fails :class:`Basin` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        data = load_yaml_strict(catalog_path) or {}
        basins_yaml = data.get("basins") or {}
        if not basins_yaml:
            raise ValueError(
                f"{catalog_path} is missing or has an empty 'basins:' block. "
                "The Tropycal catalog must list at least one basin."
            )
        basins: dict[str, Basin] = {}
        for code, body in basins_yaml.items():
            try:
                basins[code] = Basin(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{catalog_path} basin {code!r} failed validation:\n{exc}"
                ) from exc
        return cls(datasets=basins)

    def get_catalog(self) -> dict[str, Basin]:
        """Return the basin map (satisfies the abstract contract).

        Returns:
            dict[str, Basin]: Same object as :attr:`datasets`.
        """
        return self.datasets

    def get_basin(self, code: str) -> Basin:
        """Return the :class:`Basin` for `code`, with a did-you-mean hint.

        Thin alias over
        :meth:`~earthlens.base.AbstractCatalog.get_dataset`.

        Args:
            code: A tropycal basin code (`"north_atlantic"`, …).

        Returns:
            Basin: The matching basin row.

        Raises:
            ValueError: If `code` is not a registered basin.
        """
        return self.get_dataset(code)

    def get_field(self, code: str, field: str) -> TrackField:
        """Return one track field of one basin.

        Args:
            code: A tropycal basin code.
            field: A track-field short code (`"vmax"`, `"mslp"`,
                `"category"`).

        Returns:
            TrackField: The matching field metadata.

        Raises:
            ValueError: If `code` is not a registered basin.
            KeyError: If `field` is not a field of that basin.

        Examples:
            - Read a field's units:
                ```python
                >>> from earthlens.tropycal import Catalog
                >>> Catalog().get_field("north_atlantic", "mslp").units
                'hPa'

                ```
        """
        return self.get_basin(code).fields[field]

    def sources_for(self, code: str) -> list[str]:
        """Return the data sources that serve a basin.

        Args:
            code: A tropycal basin code.

        Returns:
            list[str]: The basin's sources (subset of
                `["ibtracs", "hurdat"]`).

        Raises:
            ValueError: If `code` is not a registered basin.
        """
        return list(self.get_basin(code).sources)

    def codes(self) -> list[str]:
        """Return the registered basin codes, sorted.

        Returns:
            list[str]: The basin codes
                (`["all", "australia", "both", ...]`).
        """
        return sorted(self.datasets)
