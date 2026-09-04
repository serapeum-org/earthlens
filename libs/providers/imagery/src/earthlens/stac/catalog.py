"""Endpoint × collection × asset catalog for the STAC backend.

Mirrors the multi-file layout of `earthlens.gee.catalog` /
`earthlens.cmems.catalog`: the catalog ships as a directory of per-endpoint
YAML files at `src/earthlens/stac/catalog/` (`planetary-computer.yaml`,
`cdse.yaml`, `earth-search.yaml`, …) plus a single `_index.yaml` carrying the
`endpoints:` map and the per-endpoint `available_collections:` index. The
loader unions the per-endpoint `collections:` blocks into one
:class:`Catalog` at construction time (a collection key declared in two files
is a load-time error), the same way the GEE and CMEMS catalogs merge.

A collection is addressed by a single *logical* key (e.g. `"sentinel-2-l2a"`);
:meth:`Catalog.resolve` maps that key to the actual collection id a given
endpoint serves (e.g. Earth Search calls it `"sentinel-2-c1-l2a"`) via the
collection's per-endpoint `aliases`. Endpoints carry their URL + signer type +
region, so the backend can open the right client and build the right signer
from catalog data alone.

The bundled catalog directory lives at :data:`CATALOG_PATH`; tests can
monkey-patch that attribute to redirect the loader at a temporary directory or
single YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"

# Module-level parse cache, keyed on the resolved path plus a tuple of
# `(file, st_mtime_ns)` for every YAML the load touched, so editing any
# per-endpoint file invalidates the entry without inspecting every row.
# Mirrors the GEE / CMEMS multi-file caches.
_CATALOG_CACHE: dict[
    Any, tuple[dict[str, Endpoint], dict[str, list[str]], dict[str, Collection]]
] = CatalogParseCache()

SignerType = Literal[
    "anonymous",
    "aws-requester-pays",
    "mpc-sas",
    "earthdata",
    "cdse",
    "cdse-s3",
    "bdc-token",
]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML on disk)."""
    _CATALOG_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='STAC', shard_noun='per-endpoint')


class Asset(BaseModel):
    """Per-asset (band) metadata for one asset of a STAC collection.

    Frozen value object; the asset key is the parent mapping key and is not
    repeated in the body.

    Attributes:
        common_name: STAC `eo:bands` common name (`"red"`, `"nir"`), or `None`.
        dtype: On-disk data type (`"uint16"`, `"int16"`, `"float32"`, …), or `None`.
        nodata: Nodata / fill value, or `None`.
        title: Human description of the asset, or `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    common_name: str | None = None
    dtype: str | None = None
    nodata: float | int | None = None
    title: str | None = None


class Extent(BaseModel):
    """Spatial/temporal coverage of a STAC collection.

    Attributes:
        start_date: First available date (`YYYY-MM-DD`), or `None`.
        end_date: Last available date, or `None` for a rolling collection.
        bbox: `[west, south, east, north]` in EPSG:4326, or `None` for global.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_date: str | None = None
    end_date: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class Endpoint(BaseModel):
    """One STAC API endpoint: its URL, signer type, and optional region.

    Attributes:
        key: Endpoint key (`"planetary-computer"`, `"cdse"`, `"earth-search"`,
            `"deafrica"`, `"dea"`, `"veda"`, `"usgs-landsat"`, `"bdc"`).
        url: STAC API root URL.
        signer: Signer type used for this endpoint's assets — one of the
            `SignerType` literals (`"anonymous"`, `"aws-requester-pays"`,
            `"mpc-sas"`, `"earthdata"`, `"cdse"`, `"cdse-s3"`, `"bdc-token"`).
        region: Optional AWS region for requester-pays / S3 endpoints.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    url: str
    signer: SignerType = "anonymous"
    region: str | None = None


class Collection(BaseModel):
    """One curated STAC collection, addressed by a logical key.

    Attributes:
        endpoint: Default endpoint key this collection is served from.
        collection_id: The upstream collection id at the default endpoint.
            Defaults to the logical key when omitted.
        aliases: Per-endpoint overrides of the collection id, keyed by
            endpoint key (e.g. `{"earth-search": "sentinel-2-c1-l2a"}`).
        asset_aliases: Per-endpoint overrides of the *asset* keys, keyed by
            endpoint key then by the catalog's own asset key (e.g. CDSE serves
            Sentinel-2 bands per resolution, so `B04` is `B04_10m` there). Only
            the endpoints that rename need an entry; an asset with no override
            is passed through unchanged. Both keys are checked when the catalog
            loads — an endpoint no `endpoints:` block declares, or an asset this
            collection does not carry, is rejected rather than silently doing
            nothing. Resolve with `Catalog.resolve_assets`.
        default_assets: Asset keys pulled when the request names no assets.
        cadence: Native revisit cadence label, or `None`.
        resolution: Native ground sample distance in metres, or `None`.
        extent: Spatial/temporal coverage.
        assets: Asset key → :class:`Asset` metadata.
        signer: Optional per-collection signer override. When set, the backend
            reads this collection's assets with this signer instead of the
            endpoint's default — e.g. `"aws-requester-pays"` for a collection
            whose assets sit on a requester-pays S3 bucket (Earth Search's
            `landsat-c2-l2` → `s3://usgs-landsat`). `None` uses the endpoint
            signer.
        requires_token: Documentation flag for token-gated collections (e.g. a
            Brazil Data Cube row that needs `$BDC_ACCESS_TOKEN`). The flag is
            informational only; the actual routing comes from `signer`
            (e.g. `signer: bdc-token`). Defaults to `False`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str
    collection_id: str | None = None
    aliases: dict[str, str] = Field(default_factory=dict)
    asset_aliases: dict[str, dict[str, str]] = Field(default_factory=dict)
    default_assets: list[str] = Field(default_factory=list)
    cadence: str | None = None
    resolution: float | None = None
    extent: Extent | None = None
    assets: dict[str, Asset] = Field(default_factory=dict)
    signer: SignerType | None = None
    requires_token: bool = False


def _load_catalog_data(
    path: Path,
) -> tuple[dict[str, Endpoint], dict[str, list[str]], dict[str, Collection]]:
    """Parse, validate, and cache the STAC catalog at `path`.

    Merges every `*.yaml` under a directory: `endpoints:` maps are unioned
    (a duplicate endpoint key across files is an error), `available_collections:`
    maps are merged per endpoint, and `collections:` maps are unioned (a
    duplicate logical collection key across files is an error). Cached on the
    resolved path plus every contributing file's `mtime_ns`.

    Args:
        path: Catalog directory (default) or a single `*.yaml` file.

    Returns:
        `(endpoints, available_collections, collections)`.

    Raises:
        ValueError: On a missing `collections:` block, a duplicate endpoint or
            collection key, an invalid row, an unknown asset field, or a
            collection whose `endpoint` is not a declared endpoint key.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    endpoints_yaml: dict[str, Any] = {}
    available: dict[str, list[str]] = {}
    collections_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        for ep_key, ep_body in (data.get("endpoints") or {}).items():
            if ep_key in endpoints_yaml:
                raise ValueError(
                    f"endpoint {ep_key!r} declared in two catalog files: "
                    f"{origin.get('endpoint:' + ep_key)} and {file_path}"
                )
            endpoints_yaml[ep_key] = {"key": ep_key, **dict(ep_body or {})}
            origin["endpoint:" + ep_key] = file_path
        for ep_key, ids in (data.get("available_collections") or {}).items():
            available.setdefault(ep_key, [])
            available[ep_key].extend(ids or [])
        for col_key, col_body in (data.get("collections") or {}).items():
            if col_key in collections_yaml:
                raise ValueError(
                    f"collection {col_key!r} declared in two catalog files: "
                    f"{origin[col_key]} and {file_path}"
                )
            collections_yaml[col_key] = col_body
            origin[col_key] = file_path

    if not collections_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'collections:' block. "
            "The catalog must contain at least one curated collection."
        )

    endpoints: dict[str, Endpoint] = {}
    for ep_key, ep_body in endpoints_yaml.items():
        try:
            endpoints[ep_key] = Endpoint(**ep_body)
        except ValidationError as exc:
            raise ValueError(
                f"invalid endpoint {ep_key!r} in {origin['endpoint:' + ep_key]}: {exc}"
            ) from exc

    collections: dict[str, Collection] = {}
    for col_key, col_body in collections_yaml.items():
        body = dict(col_body or {})
        assets_yaml = dict(body.pop("assets", {}) or {})
        assets: dict[str, Asset] = {}
        for asset_key, asset_body in assets_yaml.items():
            try:
                assets[asset_key] = Asset(**dict(asset_body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"invalid asset {asset_key!r} under collection {col_key!r} "
                    f"in {origin[col_key]}: {exc}"
                ) from exc
        try:
            collections[col_key] = Collection(assets=assets, **body)
        except ValidationError as exc:
            raise ValueError(
                f"invalid collection {col_key!r} in {origin[col_key]}: {exc}"
            ) from exc
        if endpoints and collections[col_key].endpoint not in endpoints:
            raise ValueError(
                f"collection {col_key!r} names endpoint "
                f"{collections[col_key].endpoint!r} which is not declared in "
                f"any 'endpoints:' block ({origin[col_key]})."
            )
        # An asset_aliases typo is otherwise invisible: an unknown endpoint key
        # never matches, and an unknown asset key passes straight through, so
        # the rename silently does nothing and the request fails much later as
        # a StacAssetError naming a key the catalog never advertised.
        for ep_key, renames in collections[col_key].asset_aliases.items():
            if endpoints and ep_key not in endpoints:
                raise ValueError(
                    f"collection {col_key!r} declares asset_aliases for endpoint "
                    f"{ep_key!r} which is not declared in any 'endpoints:' block "
                    f"({origin[col_key]})."
                )
            unknown = sorted(set(renames) - set(assets))
            if assets and unknown:
                raise ValueError(
                    f"collection {col_key!r} declares asset_aliases for "
                    f"{ep_key!r} renaming {unknown} which are not among its "
                    f"assets ({sorted(assets)}) ({origin[col_key]})."
                )

    _CATALOG_CACHE[key] = (endpoints, available, collections)
    return _CATALOG_CACHE[key]


class Catalog(AbstractCatalog):
    """YAML-backed catalog of STAC endpoints, collections, and assets.

    Reads every `*.yaml` under :data:`CATALOG_PATH` on construction and merges
    them into one catalog of typed :class:`Endpoint` / :class:`Collection` /
    :class:`Asset` models. Collections are stored under the inherited
    :attr:`datasets` field (keyed by logical collection key); endpoints live in
    :attr:`endpoints`; the per-endpoint `available_collections:` index lives in
    :attr:`available_collections`.

    Attributes:
        endpoints: Endpoint key → :class:`Endpoint`.
        available_collections: Endpoint key → list of every collection id the
            endpoint serves (the refreshed index; informational).
        datasets: Logical collection key → :class:`Collection`.

    Examples:
        - Construct the catalog and inspect the endpoints it serves:
            ```python
            >>> from earthlens.stac import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.endpoints)  # doctest: +NORMALIZE_WHITESPACE
            ['bdc', 'cdse', 'dea', 'deafrica', 'earth-search', 'eodc', 'planetary-computer',
             'usgs-landsat', 'veda']
            >>> cat.get_collection("sentinel-2-l2a").resolution
            10.0

            ```
        - Reach a collection's default asset set:
            ```python
            >>> from earthlens.stac import Catalog
            >>> Catalog().get_collection("sentinel-2-l2a").default_assets
            ['B02', 'B03', 'B04', 'B08']

            ```
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _catalog_kind: str = "STAC catalog"

    endpoints: dict[str, Endpoint] = Field(default_factory=dict)
    available_collections: dict[str, list[str]] = Field(default_factory=dict)
    datasets: dict[str, Collection] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no collections were supplied.

        `Catalog()` with no args reads the bundled `catalog/` directory through
        the `(path, mtime)`-keyed cache. If the caller passed `datasets=...`,
        the disk read is skipped (in-memory catalogs for tests). The base
        `available_datasets` field is populated with the flattened, sorted
        union of the per-endpoint `available_collections` index so it is
        discoverable through the `AbstractCatalog` contract, then
        `super().model_post_init` populates `catalog` from `get_catalog()`.

        Raises:
            ValueError: When auto-loading, propagates the same errors as
                :meth:`load`.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.endpoints = loaded.endpoints
            self.available_collections = loaded.available_collections
            self.datasets = loaded.datasets
        if not self.providers:
            self.providers = dict(self.endpoints)
        if not self.available_datasets:
            self.available_datasets = sorted(
                {cid for ids in self.available_collections.values() for cid in ids}
            )
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the STAC catalog from disk (cached).

        Args:
            catalog_path: Catalog directory or single `*.yaml` file. Defaults
                to module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from the loader.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        endpoints, available, collections = _load_catalog_data(catalog_path)
        return cls(
            endpoints=dict(endpoints),
            available_collections=dict(available),
            datasets=dict(collections),
        )

    def get_catalog(self) -> dict[str, Collection]:
        """Return the curated collection map (logical key → :class:`Collection`)."""
        return self.datasets

    def get_collection(self, collection_key: str) -> Collection:
        """Return the :class:`Collection` for `collection_key` (did-you-mean on miss).

        Alias of the inherited :meth:`get_dataset`; provided for naming clarity.

        Args:
            collection_key: Logical collection key.

        Returns:
            The matching :class:`Collection`.

        Raises:
            ValueError: If the key is unknown (message suggests the closest key).

        Examples:
            - Look up a collection and read its native cadence:
                ```python
                >>> from earthlens.stac import Catalog
                >>> Catalog().get_collection("sentinel-2-l2a").cadence
                '5-day'

                ```
        """
        return cast("Collection", self.get_dataset(collection_key))

    def get_endpoint(self, endpoint_key: str) -> Endpoint:
        """Return the :class:`Endpoint` for `endpoint_key`.

        Args:
            endpoint_key: Endpoint key.

        Returns:
            The matching :class:`Endpoint`.

        Raises:
            ValueError: If the endpoint is unknown.

        Examples:
            - Read an endpoint's signer type and URL:
                ```python
                >>> from earthlens.stac import Catalog
                >>> ep = Catalog().get_endpoint("planetary-computer")
                >>> ep.signer
                'mpc-sas'
                >>> ep.url
                'https://planetarycomputer.microsoft.com/api/stac/v1'

                ```
        """
        try:
            return self.endpoints[endpoint_key]
        except KeyError:
            raise ValueError(
                f"{endpoint_key!r} is not a known endpoint. "
                f"Known endpoints: {sorted(self.endpoints)}."
            ) from None

    def resolve(self, endpoint: str, collection_key: str) -> str:
        """Resolve a logical collection key to the id `endpoint` actually serves.

        Applies the collection's per-endpoint `aliases`, falling back to the
        collection's `collection_id` (then the logical key itself).

        Args:
            endpoint: Endpoint key the request targets.
            collection_key: Logical collection key.

        Returns:
            The upstream collection id to pass to `client.search(collections=)`.

        Raises:
            ValueError: If `collection_key` is unknown.

        Examples:
            - Earth Search serves Sentinel-2 L2A under a different id:
                ```python
                >>> from earthlens.stac import Catalog
                >>> Catalog().resolve("earth-search", "sentinel-2-l2a")
                'sentinel-2-c1-l2a'

                ```
            - Without an alias the collection's own id is returned:
                ```python
                >>> from earthlens.stac import Catalog
                >>> Catalog().resolve("planetary-computer", "sentinel-2-l2a")
                'sentinel-2-l2a'

                ```
        """
        collection = self.get_collection(collection_key)
        if endpoint in collection.aliases:
            return collection.aliases[endpoint]
        return collection.collection_id or collection_key

    def resolve_assets(
        self, endpoint: str, collection_key: str, assets: list[str]
    ) -> list[str]:
        """Rename `assets` to the keys `endpoint` actually publishes.

        The catalog names each asset once, but an endpoint may publish the same
        band under a different key — CDSE splits Sentinel-2 per resolution, so
        the catalog's `B04` is `B04_10m` there. An asset the endpoint does not
        rename is returned unchanged, so only the renaming endpoints need an
        `asset_aliases` entry.

        Args:
            endpoint: Endpoint key the request targets.
            collection_key: Logical collection key.
            assets: Asset keys in the catalog's own naming.

        Returns:
            The asset keys to request from `endpoint`, in the given order.

        Raises:
            ValueError: If `collection_key` is unknown.

        Examples:
            - CDSE publishes Sentinel-2 bands per resolution:
                ```python
                >>> from earthlens.stac import Catalog
                >>> Catalog().resolve_assets("cdse", "sentinel-2-l2a", ["B04"])
                ['B04_10m']

                ```
            - An endpoint that does not rename gets the keys unchanged:
                ```python
                >>> from earthlens.stac import Catalog
                >>> Catalog().resolve_assets(
                ...     "planetary-computer", "sentinel-2-l2a", ["B04"]
                ... )
                ['B04']

                ```
        """
        collection = self.get_collection(collection_key)
        renames = collection.asset_aliases.get(endpoint, {})
        return [renames.get(asset, asset) for asset in assets]
