"""Named-query dispatch table for the OpenStreetMap backend.

`earthlens.osm` queries OSM live by tag filter over two protocols, so this
"catalog" is not a large remote dataset index but a small curated map of
**named queries** — one row per `<protocol>:<name>` id passed in
`variables=[...]` (`overpass:hospitals`, `ohsome:buildings`, …). It mirrors
`gdacs_data_catalog.yaml` / `overture_data_catalog.yaml`: one curated block,
no `available_*` index (the named queries *are* the curated universe), and no
refresh / probe / audit tooling.

Two protocols share the one table (`G2`):

* `overpass` rows carry a `query_template` — an Overpass QL string with a
  single `{bbox}` placeholder (filled with the bounding box in Overpass order
  `S,W,N,E`) and a `{timeout}` placeholder (the server-side QL timeout).
* `ohsome` rows carry an `ohsome_filter` — an ohsome filter string; the
  backend supplies `bboxes` (order `W,S,E,N`) and a `time` window itself.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads the
bundled `osm_data_catalog.yaml` and exposes each row as a `Dataset`, keyed by
query id under the inherited `datasets` field — which gives it the
`cat["overpass:hospitals"]` / `"ohsome:buildings" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free. `CATALOG_PATH` is the
path to the bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "osm_data_catalog.yaml"

#: The two query protocols a `Dataset` row can route to.
Protocol = Literal["overpass", "ohsome"]

#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation. Mirrors
#: the GDACS / FDSN / overture loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Dataset]] = {}


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class Dataset(BaseModel):
    """One OSM named query's dispatch row.

    The `<protocol>:<name>` query id is the parent key in
    `Catalog.datasets` and is not stored on the row.

    Attributes:
        protocol: Which live query protocol routes this row — `"overpass"`
            (current-state features via Overpass QL) or `"ohsome"` (OSM
            history + analytics via the ohsome `elements/geometry`
            endpoint).
        query_template: Overpass QL string with a `{bbox}` placeholder (and
            an optional `{timeout}` placeholder). Required for `overpass`
            rows, must be absent for `ohsome` rows.
        ohsome_filter: ohsome filter string (e.g. `"building=* and
            geometry:polygon"`). Required for `ohsome` rows, must be absent
            for `overpass` rows.
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
    geometry_types: list[str] = Field(default_factory=list)
    description: str = ""

    def model_post_init(self, __context: Any) -> None:
        """Validate the protocol carries (only) its required query field.

        Raises:
            ValueError: If an `overpass` row has no `query_template` (or
                carries an `ohsome_filter`), or an `ohsome` row has no
                `ohsome_filter` (or carries a `query_template`).
        """
        if self.protocol == "overpass":
            if not self.query_template:
                raise ValueError("an 'overpass' row requires a 'query_template'")
            if self.ohsome_filter is not None:
                raise ValueError("an 'overpass' row must not carry an 'ohsome_filter'")
        else:  # ohsome
            if not self.ohsome_filter:
                raise ValueError("an 'ohsome' row requires an 'ohsome_filter'")
            if self.query_template is not None:
                raise ValueError("an 'ohsome' row must not carry a 'query_template'")


class Catalog(AbstractCatalog):
    """Named-query catalog for the OpenStreetMap backend.

    Reads the bundled `osm_data_catalog.yaml` (shipped as package data) and
    exposes its `datasets:` block as a map of `Dataset` rows, keyed by
    `<protocol>:<name>` query id under the inherited `datasets` field.
    Instantiate with no arguments (`Catalog()`); `model_post_init` loads and
    validates the YAML in one pass. Resolve a query with `get` (a thin alias
    over `AbstractCatalog.get_dataset`).

    Attributes:
        datasets: Map from the query id to its `Dataset` row.

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

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no queries were supplied.

        `Catalog()` with no args reads `CATALOG_PATH`; passing
        `datasets=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from `load` when the YAML is missing,
                empty, or has a malformed query row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the OSM named-query catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block, or a row
                fails `Dataset` validation.
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
        _CATALOG_CACHE[key] = datasets
        return cls(datasets=dict(datasets))

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the named-query map (satisfies the abstract contract).

        Returns:
            dict[str, Dataset]: Same object as `datasets`.
        """
        return self.datasets

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
        return self.get_dataset(query_id)

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
