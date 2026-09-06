"""Named-query dispatch table for the OpenStreetMap backend.

`earthlens.osm` queries OSM live by tag filter over two protocols, so this
"catalog" is not a large remote dataset index but a small curated map of
**named queries** — one row per `<protocol>:<name>` id passed in
`variables=[...]` (`overpass:hospitals`, `ohsome:buildings`, …). It mirrors
`gdacs_data_catalog.yaml` / `overture_data_catalog.yaml`: one curated block,
no `available_*` index (the named queries *are* the curated universe), and no
refresh / probe / audit tooling.

Three protocols share the one table (`G2`, `G10`):

* `overpass` rows carry a `query_template` — an Overpass QL string with a
  single `{bbox}` placeholder (filled with the bounding box in Overpass order
  `S,W,N,E`) and a `{timeout}` placeholder (the server-side QL timeout).
* `ohsome` rows carry an `ohsome_filter` — an ohsome filter string; the
  backend supplies `bboxes` (order `W,S,E,N`) and a `time` window itself.
* `pbf` rows carry a `pyrosm_method` — the `pyrosm.OSM` reader method the
  layer maps to (`get_buildings`, `get_network`, …) — and, for `get_network`,
  an optional `network_type`. The extract itself is picked by the backend's
  `region=` kwarg against the `regions:` block (`G12`), which this loader
  exposes as `Catalog.regions` (key → Geofabrik path).

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads the
bundled `osm_data_catalog.yaml` and exposes each row as a `Dataset`, keyed by
query id under the inherited `datasets` field — which gives it the
`cat["overpass:hospitals"]` / `"ohsome:buildings" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free. `CATALOG_PATH` is the
path to the bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "osm_data_catalog.yaml"

#: The three query protocols a `Dataset` row can route to.
Protocol = Literal["overpass", "ohsome", "pbf"]

#: The `pyrosm.OSM` reader methods a `pbf` row's `pyrosm_method` may name.
#: Validated at load so a typo in the catalog fails fast rather than at read.
_PYROSM_METHODS: frozenset[str] = frozenset(
    {
        "get_buildings",
        "get_network",
        "get_pois",
        "get_landuse",
        "get_natural",
        "get_boundaries",
    }
)

#: Module-level parse cache, keyed by `load_catalog` on the resolved path
#: plus each contributing file's `(mtime_ns, size)`, so a repeated
#: `Catalog()` skips the YAML parse + pydantic validation.
#: `(datasets, regions)` pair the loader assembles.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One OSM named query's dispatch row.

    The `<protocol>:<name>` query id is the parent key in
    `Catalog.datasets` and is not stored on the row.

    Attributes:
        protocol: Which query protocol routes this row — `"overpass"`
            (current-state features via Overpass QL), `"ohsome"` (OSM
            history + analytics via the ohsome `elements/geometry`
            endpoint), or `"pbf"` (bulk read from a Geofabrik `.osm.pbf`
            extract via `pyrosm`).
        query_template: Overpass QL string with a `{bbox}` placeholder (and
            an optional `{timeout}` placeholder). Required for `overpass`
            rows, must be absent for `ohsome` / `pbf` rows.
        ohsome_filter: ohsome filter string (e.g. `"building=* and
            geometry:polygon"`). Required for `ohsome` rows, must be absent
            for `overpass` / `pbf` rows.
        pyrosm_method: The `pyrosm.OSM` reader method this layer maps to —
            one of `get_buildings`, `get_network`, `get_pois`,
            `get_landuse`, `get_natural`, `get_boundaries`. Required for
            `pbf` rows, must be absent for `overpass` / `ohsome` rows.
        network_type: The `pyrosm` `get_network(network_type=...)` argument
            (e.g. `"driving"`, `"walking"`, `"all"`); only meaningful on a
            `pbf` row whose `pyrosm_method` is `get_network`, ignored
            otherwise.
        geometry_types: The geometry kinds the query is expected to yield
            (`["Point"]`, `["Polygon"]`, `["Point", "Polygon"]`, …) —
            informational, for docs and the catalog reference.
        description: Short human-readable note on what the query returns.

    Examples:
        - Build an Overpass row directly:
            ```python
            >>> from earthlens.osm import Dataset
            >>> row = Dataset(
            ...     protocol="overpass",
            ...     query_template="[out:json];(node({bbox}););out geom;",
            ... )
            >>> row.protocol
            'overpass'

            ```
        - A row must carry the field its protocol needs:
            ```python
            >>> from earthlens.osm import Dataset
            >>> try:
            ...     Dataset(protocol="overpass")
            ... except Exception as exc:  # pydantic ValidationError
            ...     print(type(exc).__name__)
            ValidationError

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Protocol
    query_template: str | None = None
    ohsome_filter: str | None = None
    pyrosm_method: str | None = None
    network_type: str | None = None
    geometry_types: list[str] = Field(default_factory=list)
    description: str = ""

    def model_post_init(self, __context: Any) -> None:
        """Validate the protocol carries (only) its required query field.

        Raises:
            ValueError: If an `overpass` row has no `query_template`, an
                `ohsome` row has no `ohsome_filter`, or a `pbf` row has no
                (or an unknown) `pyrosm_method` — or a row carries a field
                belonging to a different protocol.
        """
        if self.protocol == "overpass":
            if not self.query_template:
                raise ValueError("an 'overpass' row requires a 'query_template'")
            if self.ohsome_filter is not None or self.pyrosm_method is not None:
                raise ValueError(
                    "an 'overpass' row must not carry an 'ohsome_filter' or "
                    "'pyrosm_method'"
                )
        elif self.protocol == "ohsome":
            if not self.ohsome_filter:
                raise ValueError("an 'ohsome' row requires an 'ohsome_filter'")
            if self.query_template is not None or self.pyrosm_method is not None:
                raise ValueError(
                    "an 'ohsome' row must not carry a 'query_template' or "
                    "'pyrosm_method'"
                )
        else:  # pbf
            if not self.pyrosm_method:
                raise ValueError("a 'pbf' row requires a 'pyrosm_method'")
            if self.pyrosm_method not in _PYROSM_METHODS:
                raise ValueError(
                    f"a 'pbf' row's 'pyrosm_method' must be one of "
                    f"{sorted(_PYROSM_METHODS)}, got {self.pyrosm_method!r}"
                )
            if self.query_template is not None or self.ohsome_filter is not None:
                raise ValueError(
                    "a 'pbf' row must not carry a 'query_template' or 'ohsome_filter'"
                )


def _parse_osm_catalog(files: list[Path]):
    """Parse and validate the OSM catalog rows.

    Args:
        files: The contributing YAML files (OSM ships a single file).

    Returns:
        The validated rows, in the shape the catalog caches.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    datasets_yaml = data.get("datasets") or {}
    if not datasets_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'datasets:' block. "
            "The OSM catalog must list at least one named query."
        )
    datasets: dict[str, Dataset] = {}
    for query_id, body in datasets_yaml.items():
        try:
            datasets[query_id] = Dataset(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} query {query_id!r} failed validation:\n{exc}"
            ) from exc
    regions: dict[str, str] = {
        str(name): str(path) for name, path in (data.get("regions") or {}).items()
    }
    return (datasets, regions)


class Catalog(AbstractCatalog[Dataset]):
    """Named-query catalog for the OpenStreetMap backend.

    Reads the bundled `osm_data_catalog.yaml` (shipped as package data) and
    exposes its `datasets:` block as a map of `Dataset` rows, keyed by
    `<protocol>:<name>` query id under the inherited `datasets` field.
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads and
    validates the YAML in one pass. Resolve a query with `get` (a thin alias
    over `AbstractCatalog.get_dataset`).

    Attributes:
        datasets: Map from the query id to its `Dataset` row.
        regions: Map from a Geofabrik region key (`"malta"`, …) to its
            Geofabrik path segment (`"europe/malta"`), read from the YAML's
            `regions:` block. Used by the `pbf` protocol (`G12`).

    Examples:
        - List query ids and resolve one:
            ```python
            >>> from earthlens.osm import Catalog
            >>> cat = Catalog()
            >>> "overpass:hospitals" in cat
            True
            >>> cat.get("overpass:hospitals").protocol
            'overpass'
            >>> cat.get("ohsome:buildings").ohsome_filter
            'building=* and geometry:polygon'
            >>> cat.get("pbf:buildings").pyrosm_method
            'get_buildings'
            >>> cat.region_path("malta")
            'europe/malta'

            ```
        - An unknown id raises with a did-you-mean hint:
            ```python
            >>> from earthlens.osm import Catalog
            >>> Catalog().get("overpass:hospital")
            Traceback (most recent call last):
                ...
            ValueError: 'overpass:hospital' is not in the OSM query catalog. Known queries: [...]. Did you mean 'overpass:hospitals'?

            ```
    """

    _catalog_kind: str = "OSM query catalog"
    _entry_noun: str = "queries"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    regions: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `regions` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "regions": loaded.regions,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the OSM named-query catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `datasets:` block, or a row fails `Dataset` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        datasets, regions = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_osm_catalog, provider="OSM"
        )
        return cls(datasets=dict(datasets), regions=dict(regions))

    def get(self, query_id: str) -> Dataset:
        """Return the `Dataset` for `query_id`, with a did-you-mean hint on miss.

        Thin alias over `AbstractCatalog.get_dataset`.

        Args:
            query_id: A named-query id (`"overpass:hospitals"`,
                `"ohsome:buildings"`, …).

        Returns:
            Dataset: The matching query row.

        Raises:
            ValueError: If `query_id` is not a registered named query.
        """
        return cast("Dataset", self.get_dataset(query_id))

    def query_ids(self) -> list[str]:
        """Return the registered named-query ids, sorted.

        Returns:
            list[str]: The query ids (`["ohsome:amenities", ...]`).

        Examples:
            - List the curated named queries:
                ```python
                >>> from earthlens.osm import Catalog
                >>> "overpass:roads" in Catalog().query_ids()
                True

                ```
        """
        return sorted(self.datasets)

    def region_path(self, region: str) -> str:
        """Resolve a `region=` value to its Geofabrik path segment (`G12`).

        A `region` containing a `/` is taken as a raw Geofabrik path (the
        power-user escape hatch, e.g. `"europe/andorra"`) and returned
        unchanged; otherwise it is looked up in the `regions:` table, with a
        did-you-mean hint on a miss.

        Args:
            region: A region key from the `regions:` table (`"malta"`, …), or
                a raw Geofabrik path (any string containing a `/`).

        Returns:
            str: The Geofabrik path segment, e.g. `"europe/malta"`.

        Raises:
            ValueError: If `region` is neither a raw path nor a known region
                key.

        Examples:
            - A known key resolves; a raw path passes through:
                ```python
                >>> from earthlens.osm import Catalog
                >>> cat = Catalog()
                >>> cat.region_path("malta")
                'europe/malta'
                >>> cat.region_path("europe/andorra")
                'europe/andorra'

                ```
        """
        if "/" in region:
            return region
        path = self.regions.get(region)
        if path is not None:
            return path
        known = sorted(self.regions)
        suggestion = get_close_matches(region, known, n=1)
        hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
        raise ValueError(
            f"{region!r} is not a known Geofabrik region. Known regions: "
            f"{known}.{hint} You may also pass a raw Geofabrik path containing "
            "a '/', e.g. 'europe/andorra'."
        )

    def region_ids(self) -> list[str]:
        """Return the registered Geofabrik region keys, sorted.

        Returns:
            list[str]: The region keys (`["andorra", "belgium", ...]`).

        Examples:
            - The primary test extract is listed:
                ```python
                >>> from earthlens.osm import Catalog
                >>> "malta" in Catalog().region_ids()
                True

                ```
        """
        return sorted(self.regions)
