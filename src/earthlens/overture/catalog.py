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
(`yyyy-mm-dd.x`). `tools/overture/refresh_overture_catalog.py` lists the
available releases — via the official `overturemaps` SDK (which reads
the Overture STAC catalog) — into the bundled YAML's informational
`available_releases:` block; the theme/type set itself is fixed and
hand-curated. The SDK auto-targets the newest release when `release` is
`None`, so the index is for discoverability, not dispatch.

`Catalog` is a thin `earthlens.base.AbstractCatalog` subclass that loads
the bundled `overture_data_catalog.yaml` and exposes each theme as a
`Theme`, keyed by name under the inherited `datasets` field — which is
what gives it the `cat["buildings"]` / `"places" in cat` / `len(cat)`
dict-like surface and the did-you-mean error for free. `CATALOG_PATH` is
the path to the bundled YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "overture_data_catalog.yaml"


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
                f"default_type {self.default_type!r} is not one of types "
                f"{self.types}"
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


class Catalog(AbstractCatalog):
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
            newest first, from the bundled YAML's informational index.

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
            ValueError: 'building' is not in the Overture theme catalog. Known datasets: ['addresses', 'base', 'buildings', 'divisions', 'places', 'transportation']. Did you mean 'buildings'?

            ```
    """

    _catalog_kind: str = "Overture theme catalog"

    datasets: dict[str, Theme] = Field(default_factory=dict)
    available_releases: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no themes were supplied.

        `Catalog()` with no args reads `CATALOG_PATH`; passing
        `datasets=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from `load` when the YAML is missing,
                empty, or has a malformed theme row.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            self.available_releases = loaded.available_releases
            self.available_datasets = loaded.available_datasets
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the Overture theme catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If the file has no `themes:` block, or a row
                fails `Theme` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        data = load_yaml_strict(catalog_path) or {}
        themes_yaml = data.get("themes") or {}
        if not themes_yaml:
            raise ValueError(
                f"{catalog_path} is missing or has an empty 'themes:' block. "
                "The Overture catalog must list at least one theme."
            )
        themes: dict[str, Theme] = {}
        for name, body in themes_yaml.items():
            try:
                themes[name] = Theme(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{catalog_path} theme {name!r} failed validation:\n{exc}"
                ) from exc
        releases = list(data.get("available_releases") or [])
        available = list(data.get("available_datasets") or [])
        return cls(
            datasets=themes,
            available_releases=releases,
            available_datasets=available,
        )

    def get_catalog(self) -> dict[str, Theme]:
        """Return the theme map (satisfies the abstract contract).

        Returns:
            dict[str, Theme]: Same object as `datasets`.
        """
        return self.datasets

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
        return self.get_dataset(name)

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

        Reads only the bundled `available_releases:` index (newest
        first); it makes no network call. When the index is empty the
        backend leaves `release=None` and lets the `overturemaps` SDK
        auto-target the newest release at fetch time.

        Returns:
            str | None: The newest release id (e.g. `"2026-05-20.0"`), or
                `None` when the index is empty.

        Examples:
            - The newest release is the head of the index (no network):
                ```python
                >>> from earthlens.overture import Catalog
                >>> release = Catalog().latest_release()
                >>> release is None or release[:2] == "20"
                True

                ```
        """
        return self.available_releases[0] if self.available_releases else None
