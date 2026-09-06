"""Theme/type dispatch table for the Overture Maps backend.

Overture publishes a small, fixed set of data themes (`buildings`,
`places`, `transportation`, `divisions`, plus the out-of-scope `base` /
`addresses`), each a parent partition on the public
`s3://overturemaps-us-west-2` GeoParquet. This "catalog" is therefore a
curated map — keyed by the friendly theme name a caller passes in
`variables={theme: [type, ...]}` — rather than a large remote index. It
mirrors `gdacs_data_catalog.yaml`: one curated block, no per-row data
variables, with each theme carrying its feature `types`, a
`default_type` (used when the caller passes an empty type list), the
geometry kind, representative key columns, and the licenses its rows
typically carry.

The one moving part Overture has is the monthly **release**
(`yyyy-mm-dd.x`). `earthlens datasets refresh overture --write` lists the
available releases — via the official `overturemaps` SDK (which reads
the Overture STAC catalog) — into the bundled YAML's informational
`available_releases:` block; the theme/type set itself is fixed and
hand-curated. The index is for discoverability, not dispatch: Overture
keeps only the newest release or two on S3, so a bundled release id goes
stale within weeks and the backend resolves the release live instead.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads
the bundled `overture_data_catalog.yaml` and exposes each theme as a
`Theme`, keyed by name under the inherited `datasets` field — which is
what gives it the `cat["buildings"]` / `"places" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free. `CATALOG_PATH` is
the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict
from earthlens.overture.releases import is_release_id

CATALOG_PATH: Path = Path(__file__).parent / "overture_data_catalog.yaml"


#: Module-level parse cache keyed on `(resolved_path, st_mtime_ns)` so a
#: repeated `Catalog()` skips the YAML parse + pydantic validation. Stores the
#: `(themes, available_releases, available_datasets)` triple. Mirrors the
#: FDSN / NWP / radar loaders.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


def _release_sort_key(release: str) -> tuple[str, int]:
    """Order an Overture release id by its date, then its ordinal.

    Release ids are `yyyy-mm-dd.n`, so a plain string sort mis-orders
    the ordinal once it reaches two digits (`"2026-07-22.10"` sorts
    below `"2026-07-22.9"`). Splitting the ordinal off and comparing it
    numerically fixes that. Callers filter through `is_release_id` first, so
    the ordinal is always numeric here.

    Args:
        release: A release id (`"2026-07-22.0"`).

    Returns:
        tuple[str, int]: The `(date, ordinal)` pair to sort on.

    Examples:
        - The ordinal compares numerically, not lexicographically:
            ```python
            >>> from earthlens.overture.catalog import _release_sort_key
            >>> _release_sort_key("2026-07-22.10") > _release_sort_key("2026-07-22.9")
            True

            ```
        - The key splits an id into the pair it sorts on:
            ```python
            >>> from earthlens.overture.catalog import _release_sort_key
            >>> _release_sort_key("2026-07-22.0")
            ('2026-07-22', 0)

            ```
        - Sorting a release list puts the newest last:
            ```python
            >>> from earthlens.overture.catalog import _release_sort_key
            >>> releases = ["2026-07-22.0", "2026-06-17.0", "2026-07-22.1"]
            >>> sorted(releases, key=_release_sort_key)[-1]
            '2026-07-22.1'

            ```

    See Also:
        Catalog.latest_release: Picks the newest indexed release with this key.
    """
    date, _, ordinal = release.partition(".")
    return date, int(ordinal)


class Theme(BaseModel):
    """One Overture theme's dispatch row.

    The friendly theme name (`"buildings"`, `"places"`, …) is the parent
    key in `Catalog.datasets` and is not stored on the row.

    Attributes:
        types: The Overture feature types this theme owns (e.g.
            `["building", "building_part"]`). The SDK's unit of download
            is the type, not the theme, so the backend calls the SDK
            once per requested type.
        default_type: The type fetched when the caller leaves the type
            list empty (e.g. `{"buildings": []}` resolves to
            `["building"]`). Must be a member of `types`.
        geometry: The dominant geometry kind of the theme's primary type
            (`"Polygon"`, `"Point"`, `"LineString"`) — informational,
            for docs and the catalog reference.
        key_columns: Representative columns the theme's rows carry, for
            docs and quick orientation; not an exhaustive schema.
        licenses: The licenses the theme's rows typically carry. Overture
            rows are `CDLA-Permissive-2.0` unless OSM-derived, in which
            case they carry `ODbL-1.0` — surfaced per-row by the backend.
        description: Short human-readable note on what the theme covers.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.overture import Theme
            >>> t = Theme(types=["place"], default_type="place", geometry="Point")
            >>> t.default_type
            'place'

            ```
        - `default_type` must be one of `types`:
            ```python
            >>> from earthlens.overture import Theme
            >>> try:
            ...     Theme(types=["building"], default_type="place", geometry="Polygon")
            ... except Exception as exc:  # pydantic ValidationError
            ...     print(type(exc).__name__)
            ValidationError

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    types: list[str] = Field(min_length=1)
    default_type: str
    geometry: str
    key_columns: list[str] = Field(default_factory=list)
    licenses: list[str] = Field(default_factory=list)
    description: str = ""

    def model_post_init(self, __context: Any) -> None:
        """Validate that `default_type` is a member of `types`.

        Raises:
            ValueError: If `default_type` is not in `types`.
        """
        if self.default_type not in self.types:
            raise ValueError(
                f"default_type {self.default_type!r} is not one of types {self.types}"
            )

    def resolve_types(self, requested: list[str] | None) -> list[str]:
        """Return the types to fetch for a request, defaulting to the primary.

        Args:
            requested: The type list the caller supplied for this theme.
                An empty list or `None` resolves to `[default_type]`.
                Duplicates are collapsed (order preserved) so a repeated
                type is fetched once rather than overwriting its own file.

        Returns:
            list[str]: The concrete Overture types to download, de-duplicated
                in first-seen order.

        Raises:
            ValueError: If a requested type is not one of this theme's
                `types`.

        Examples:
            - An empty request resolves to the theme's primary type:
                ```python
                >>> from earthlens.overture import Catalog
                >>> Catalog().get_theme("buildings").resolve_types([])
                ['building']

                ```
            - An explicit request is honoured and de-duplicated in order:
                ```python
                >>> from earthlens.overture import Catalog
                >>> theme = Catalog().get_theme("transportation")
                >>> theme.resolve_types(["connector", "segment", "connector"])
                ['connector', 'segment']

                ```
        """
        if not requested:
            return [self.default_type]
        unknown = [t for t in requested if t not in self.types]
        if unknown:
            raise ValueError(
                f"{unknown} are not valid types for this theme; "
                f"choose from {self.types}."
            )
        return list(dict.fromkeys(requested))


def _parse_overture_catalog(files: list[Path]):
    """Parse and validate the Overture catalog rows.

    Args:
        files: The contributing YAML files (Overture ships a single file).

    Returns:
        The validated rows, in the shape the catalog caches.

    Raises:
        ValueError: If a required block is missing or a row fails
            validation.
    """
    catalog_path = files[0]
    data = load_yaml_strict(catalog_path) or {}
    themes_yaml = data.get("themes") or {}
    if not themes_yaml:
        raise ValueError(
            f"{catalog_path} is missing or has an empty 'themes:' block. "
            "The Overture catalog must list at least one theme."
        )
    themes = {}
    for name, body in themes_yaml.items():
        try:
            themes[name] = Theme(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{catalog_path} theme {name!r} failed validation:\n{exc}"
            ) from exc
    releases = list(data.get("available_releases") or [])
    available = list(data.get("available_datasets") or [])
    return (themes, releases, available)


class Catalog(AbstractCatalog[Theme]):
    """Theme/type catalog for the Overture Maps backend.

    Reads the bundled `overture_data_catalog.yaml` (shipped as package
    data) and exposes its `themes:` block as a map of `Theme` rows, keyed
    by friendly theme name under the inherited `datasets` field, plus the
    informational `available_releases:` index. Instantiate with no
    arguments (`Catalog()`); `model_post_init` loads and validates the
    YAML in one pass. Resolve a theme with `get_theme` (a thin alias over
    `AbstractCatalog.get_dataset`).

    Attributes:
        datasets: Map from the friendly theme name to its `Theme` row (the
            curated subset).
        available_datasets: Every Overture feature type the SDK exposes,
            across all themes (including the uncurated `base` / `addresses`
            themes) — the full queryable universe the curated themes are a
            subset of. Rebuilt by the refresh tool.
        available_releases: Overture release identifiers (`yyyy-mm-dd.x`),
            from the bundled YAML's informational index, in whatever
            order the refresh tooling wrote them. `latest_release`
            picks the newest by date and ordinal rather than by
            position, so the order here is not load-bearing.

    Examples:
        - List themes and resolve one:
            ```python
            >>> from earthlens.overture import Catalog
            >>> cat = Catalog()
            >>> cat.themes()
            ['addresses', 'base', 'buildings', 'divisions', 'places', 'transportation']
            >>> cat.get_theme("buildings").default_type
            'building'
            >>> "places" in cat
            True

            ```
        - An unknown theme raises with a did-you-mean hint:
            ```python
            >>> from earthlens.overture import Catalog
            >>> Catalog().get_theme("building")
            Traceback (most recent call last):
                ...
            ValueError: 'building' is not in the Overture theme catalog. Known themes: ['addresses', 'base', 'buildings', 'divisions', 'places', 'transportation']. Did you mean 'buildings'?

            ```
    """

    _catalog_kind: str = "Overture theme catalog"
    _entry_noun: str = "themes"

    datasets: dict[str, Theme] = Field(default_factory=dict)
    available_releases: list[str] = Field(default_factory=list)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `available_releases`, `available_datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "available_releases": loaded.available_releases,
            "available_datasets": loaded.available_datasets,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Overture theme catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file has no
                `themes:` block, or a row fails `Theme` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        themes, releases, available = load_catalog(
            catalog_path, _CATALOG_CACHE, _parse_overture_catalog, provider="Overture"
        )
        return cls(
            datasets=dict(themes),
            available_releases=list(releases),
            available_datasets=list(available),
        )

    def get_theme(self, name: str) -> Theme:
        """Return the `Theme` for `name`, with a did-you-mean hint on miss.

        Thin alias over `AbstractCatalog.get_dataset`.

        Args:
            name: A friendly theme name (`"buildings"`, `"places"`, …).

        Returns:
            Theme: The matching theme row.

        Raises:
            ValueError: If `name` is not a registered theme.

        Examples:
            - Resolve a theme and read its primary type and geometry:
                ```python
                >>> from earthlens.overture import Catalog
                >>> theme = Catalog().get_theme("places")
                >>> theme.default_type
                'place'
                >>> theme.geometry
                'Point'

                ```
        """
        return cast("Theme", self.get_dataset(name))

    def themes(self) -> list[str]:
        """Return the registered theme names, sorted.

        Returns:
            list[str]: The theme names
                (`["addresses", "base", "buildings", ...]`).

        Examples:
            - List the curated themes:
                ```python
                >>> from earthlens.overture import Catalog
                >>> Catalog().themes()[:3]
                ['addresses', 'base', 'buildings']

                ```
        """
        return sorted(self.datasets)

    def available_types(self) -> list[str]:
        """Return every Overture feature type in the available index, sorted.

        This is the provider's full queryable universe (all themes,
        including the uncurated `base` / `addresses`); every curated
        theme's `types` are a subset of it.

        Returns:
            list[str]: All Overture types
                (`["address", "bathymetry", "building", ...]`).

        Examples:
            - The full type universe includes every curated theme's types:
                ```python
                >>> from earthlens.overture import Catalog
                >>> types = Catalog().available_types()
                >>> "building" in types and "address" in types
                True
                >>> len(types)
                15

                ```
        """
        return sorted(self.available_datasets)

    def latest_release(self) -> str | None:
        """Return the newest indexed Overture release, or `None` if unindexed.

        Reads only the bundled `available_releases:` index; it makes no
        network call. The newest entry is chosen by date and ordinal
        rather than by position, so the index may be stored in any order
        — `earthlens datasets refresh overture --write` persists it
        ascending, while it was originally hand-written newest-first.
        Entries that are not shaped like a release id are ignored, so a
        malformed index yields `None` rather than a bogus release.

        The result is a stale-by-construction fallback: Overture prunes
        old releases from S3, so the backend prefers the release the SDK
        reports live and only falls back to this when that read fails.

        Returns:
            str | None: The newest release id (e.g. `"2026-07-22.0"`), or
                `None` when the index is empty.

        Examples:
            - The newest release wins regardless of index order (no network):
                ```python
                >>> from earthlens.overture import Catalog
                >>> release = Catalog().latest_release()
                >>> release is None or release[:2] == "20"
                True

                ```
            - Position in the index does not decide the winner (these ids
              are deliberately unlike the bundled ones, so the answer can
              only have come from the list supplied here):
                ```python
                >>> from earthlens.overture import Catalog
                >>> cat = Catalog(
                ...     datasets={},
                ...     available_releases=["2031-03-03.0", "2030-01-01.0"],
                ... )
                >>> cat.latest_release()
                '2031-03-03.0'

                ```

        See Also:
            earthlens.overture.backend.Overture._resolve_release: Prefers the
                live release and falls back to this one.
        """
        return max(
            (r for r in self.available_releases if is_release_id(r)),
            key=_release_sort_key,
            default=None,
        )
