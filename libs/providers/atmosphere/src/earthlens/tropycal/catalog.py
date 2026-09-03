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
:class:`Catalog` is an :class:`earthlens.base.AbstractCatalog` subclass
that loads the bundled `tropycal_data_catalog.yaml` and stores the basin
map under the inherited `datasets` field — which gives it the
`cat["north_atlantic"]` / `"north_atlantic" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free.

It mirrors the ECMWF / GEE catalogs' shape: it exposes an
:attr:`Catalog.available_datasets` index (every basin tropycal serves —
here that *is* the curated set, since the basins are the whole universe),
a :meth:`Catalog.health` hygiene report, and a `(path, mtime_ns)` parse
cache (see :func:`clear_catalog_cache`) so repeated `Catalog()`
construction is ~1 ms. :data:`CATALOG_PATH` is the path to the bundled
YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "tropycal_data_catalog.yaml"

#: The data sources tropycal 1.4 exposes (no `jtwc`); used by
#: :meth:`Catalog.health` to flag a basin row referencing an unknown one.
_KNOWN_SOURCES: frozenset[str] = frozenset({"ibtracs", "hurdat"})

# Module-level cache of parsed basin maps, keyed on `(resolved_path,
# mtime_ns)` so any real file mutation invalidates the entry naturally.
# Mirrors the ECMWF / GEE catalog cache so repeated `Catalog()`
# construction skips the YAML parse + pydantic validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level basin parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache key includes
    `st_mtime_ns`, so any real file mutation invalidates the entry on its
    own.
    """
    _CATALOG_CACHE.clear()


def _parse_basins(files: list[Path]) -> dict[str, Basin]:
    """Parse and validate the Tropycal catalog rows.

    Args:
        files: The contributing YAML files (Tropycal ships a single file).

    Returns:
        dict[str, Basin]: The validated rows.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    basins_yaml = data.get("basins") or {}
    if not basins_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'basins:' block. "
            "The Tropycal catalog must list at least one basin."
        )
    basins: dict[str, Basin] = {}
    for code, body in basins_yaml.items():
        try:
            basins[code] = Basin(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} basin {code!r} failed validation:\n{exc}"
            ) from exc
    return basins


def _load_basins(path: Path) -> dict[str, Basin]:
    """Return the parsed Tropycal catalog at `path`, memoised on its mtime.

    Args:
        path: The catalog file.

    Returns:
        dict[str, Basin]: From the cache when the file is unchanged.

    Raises:
        ValueError: If the path does not exist, or parsing fails.
    """
    return load_catalog(path, _CATALOG_CACHE, _parse_basins, provider="Tropycal")


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
            ValueError: 'north_altantic' is not in the Tropycal basin catalog. Known basins: ['all', 'australia', 'both', 'east_pacific', 'north_atlantic', 'north_indian', 'south_atlantic', 'south_indian', 'south_pacific', 'west_pacific']. Did you mean 'north_atlantic'?

            ```
    """

    _catalog_kind: str = "Tropycal basin catalog"
    _entry_noun: str = "basins"

    datasets: dict[str, Basin] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no basins were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (through the
        `(path, mtime_ns)` cache); passing `datasets=...` skips the disk
        read (used in tests). Either way, :attr:`available_datasets` is
        populated with the basin codes — the whole tropycal universe.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed basin row.
        """
        if not self.datasets:
            self.datasets = dict(_load_basins(CATALOG_PATH))
        if not self.available_datasets:
            self.available_datasets = sorted(self.datasets)
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Tropycal basin catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog` with `datasets` and the
            `available_datasets` index set.

        Raises:
            ValueError: If the file has no `basins:` block, or a row
                fails :class:`Basin` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        basins = _load_basins(catalog_path)
        return cls(datasets=dict(basins), available_datasets=sorted(basins))

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
        return cast("Basin", self.get_dataset(code))

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

    def get_variable(self, code: str, field: str) -> TrackField:
        """Leaf accessor for the shared two-arg get_variable contract.

        Alias of :meth:`get_field` so the tropycal leaf is reachable
        under the same `get_variable(dataset_key, variable_name)` verb
        the other two-level catalogs use.

        Args:
            code: A tropycal basin code.
            field: A track-field short code.

        Returns:
            TrackField: The matching field metadata.
        """
        return self.get_field(code, field)

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

    def describe(self, code: str) -> dict[str, Any]:
        """Return a structured introspection record for a basin.

        Mirrors :meth:`earthlens.ecmwf.Catalog.describe`: a runtime "what
        does basin X expose?" helper a CLI / notebook can dump without
        walking the YAML.

        Args:
            code: A tropycal basin code (e.g. `"north_atlantic"`).

        Returns:
            dict[str, Any]: Keys `basin` (the code), `name`, `sources`
            (the serving data sources), and `fields` (sorted track-field
            codes).

        Raises:
            ValueError: If `code` is not a registered basin.

        Examples:
            - Describe the North Atlantic at a glance:
                ```python
                >>> from earthlens.tropycal import Catalog
                >>> info = Catalog().describe("north_atlantic")
                >>> info["name"]
                'North Atlantic'
                >>> info["sources"]
                ['ibtracs', 'hurdat']
                >>> info["fields"]
                ['category', 'mslp', 'vmax']

                ```
        """
        basin = self.get_basin(code)
        return {
            "basin": code,
            "name": basin.name,
            "sources": list(basin.sources),
            "fields": sorted(basin.fields),
        }

    def health(self) -> dict[str, list[str]]:
        """Report structural hygiene issues across the loaded catalog.

        Mirrors :meth:`earthlens.ecmwf.Catalog.health` /
        :meth:`earthlens.gee.Catalog.health`: returns a mapping
        `check_name -> sorted list of offenders`. An empty list means the
        check passes; an empty dict means the catalog is clean. Schema-level
        invariants (duplicate keys, unknown fields) are already enforced at
        load time — these are the residual data-quality checks the pydantic
        schema cannot express.

        Checks reported:

        * `basin_without_sources` — basins whose `sources` list is empty
          (no tropycal source could serve them).
        * `basin_without_fields` — basins carrying zero track fields.
        * `basin_unknown_source` — `"<basin>:<source>"` for any source not
          in tropycal's known set (`ibtracs` / `hurdat`).

        Returns:
            dict[str, list[str]]: The per-check offender lists.

        Examples:
            - The bundled catalog is clean:
                ```python
                >>> from earthlens.tropycal import Catalog
                >>> Catalog().health()
                {'basin_without_sources': [], 'basin_without_fields': [], 'basin_unknown_source': []}

                ```
        """
        no_sources: list[str] = []
        no_fields: list[str] = []
        unknown_source: list[str] = []
        for code, basin in self.datasets.items():
            if not basin.sources:
                no_sources.append(code)
            if not basin.fields:
                no_fields.append(code)
            for source in basin.sources:
                if source not in _KNOWN_SOURCES:
                    unknown_source.append(f"{code}:{source}")
        return {
            "basin_without_sources": sorted(no_sources),
            "basin_without_fields": sorted(no_fields),
            "basin_unknown_source": sorted(unknown_source),
        }
