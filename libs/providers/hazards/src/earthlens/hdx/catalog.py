"""Dataset-catalog loader for the Humanitarian Data Exchange backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled HDX
catalog. Mirrors the shape of :mod:`earthlens.earthdata.catalog` and
:mod:`earthlens.cmems.catalog`: the catalog ships as a directory of
per-theme YAML files at `src/earthlens/hdx/catalog/`
(`population.yaml`, `buildings.yaml`, `boundaries.yaml`, …) plus a
single gzipped `_available.json.gz` carrying the long-tail index
(the `C7` auto-generated index). Each per-theme file contributes its
`datasets:` block; the loader unions them into one :class:`Catalog` at
construction time.

A friendly dataset key (e.g. `"kontur-population"`) resolves to an
:class:`HdxDataset` via :meth:`Catalog.get_dataset` / `Catalog()["..."]`
/ :meth:`Catalog.resolve`. The row carries the fields the backend needs
to address one HDX dataset and filter its resources: the CKAN
`hdx_id` (the dataset's `name` on `data.humdata.org`), the owning
`org`, a human `title`, the `themes` it belongs to, the CKAN format
labels its resources carry (`"Geopackage"`, `"CSV"`, `"GeoTIFF"`), an
optional default `resource_filter`, and the informational
`output_kinds` those resources map onto.

`available_datasets:` is the informational index of every HDX dataset
the `C7` refresh tool found for the curated orgs/themes; the curated
`datasets:` map is the small vetted subset the maintainer hand-checks.
The path to the bundled catalog directory lives at
:data:`CATALOG_PATH`; tests can monkey-patch that module attribute to
redirect the loader at a temporary directory or single YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog, yaml_files_for
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"

# Module-level cache of parsed catalog data, keyed on the resolved path
# plus a tuple of `(file, mtime_ns)` for every YAML the load touched, so
# editing any per-theme file invalidates the entry without inspecting
# every row. Mirrors the Earthdata / CMEMS multi-file pattern.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()
# Same `(path, mtime_ns)` cache for the auto-generated `available_datasets`
# index. The index is held as JSON (`_available.json`), not YAML, and parsed
# separately from the curated per-theme YAMLs — so a `Catalog()` pays only the
# fast JSON read (a flat list of ~7k ids) instead of a multi-hundred-millisecond
# YAML parse, mirroring how `earthlens.earthdata` keeps its long tail in
# `_auto.json` out of the curated YAML glob.
_AVAILABLE_CACHE: CatalogParseCache = CatalogParseCache()

#: Filename of the gzipped JSON `available_datasets` index, kept beside the
#: curated per-theme YAMLs (and out of the `*.yaml` glob). Gzipped because the
#: full ~41k-row index compresses ~9x (≈5 MB → ≈0.5 MB) and decompresses in a
#: few ms, so the wheel stays small with no meaningful load-time cost.
AVAILABLE_INDEX_NAME = "_available.json.gz"
#: Uncompressed fallback name (custom catalog dirs / tests may ship plain JSON).
AVAILABLE_INDEX_NAME_PLAIN = "_available.json"

OutputKindLiteral = Literal["raster", "vector", "tabular", "mixed"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse caches.

    Useful in tests that rewrite the catalog on disk and want to force
    a re-parse. Production callers do not need this — the cache keys
    include every contributing file's `st_mtime_ns`, so any real file
    mutation invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()
    _AVAILABLE_CACHE.clear()


def _available_index_path(catalog_path: Path) -> Path:
    """Return the `_available.json` path that sits beside a catalog path.

    Args:
        catalog_path: The catalog directory, or a single catalog YAML
            file (the index is then looked for in its parent directory).

    Returns:
        Path: The sibling gzipped `_available.json.gz` when present,
            else the plain `_available.json` (which may not exist
            either).
    """
    base = catalog_path if catalog_path.is_dir() else catalog_path.parent
    gz = base / AVAILABLE_INDEX_NAME
    return gz if gz.is_file() else base / AVAILABLE_INDEX_NAME_PLAIN


def _load_available(json_path: Path) -> dict[str, dict]:
    """Read and cache the JSON long-tail index `{hdx_id: {org, title}}`.

    Reads the full HDX index from `json_path` without touching the
    curated YAMLs — a flat `{id: {org, title}}` map that parses in
    milliseconds. The bundled index is gzipped (`.json.gz`); a plain
    `.json` is also accepted (custom catalog dirs / tests), detected by
    suffix. Two on-disk shapes are accepted: the enriched
    `{"datasets": {id: {"org": ..., "title": ...}}}` (current) and the
    older `{"available_datasets": [id, ...]}` (back-compat — those ids
    become thin rows with empty `org` / `title`). Returns an empty map
    when the file is absent (e.g. a custom catalog directory in a test
    that ships no index).

    Args:
        json_path: Path to the `_available.json.gz` (or plain
            `_available.json`) file.

    Returns:
        dict[str, dict]: Map from HDX id to its `{org, title}` row
            (empty when the file is absent).
    """
    if not json_path.is_file():
        return {}
    return load_catalog(json_path, _AVAILABLE_CACHE, _parse_available, provider="HDX")


def _parse_available(files: list[Path]) -> dict[str, dict]:
    """Read the long-tail index out of `_available.json[.gz]`.

    Args:
        files: The single index path, from the shared loader.

    Returns:
        dict[str, dict]: Map from HDX id to its `{org, title}` row.
    """
    json_path = files[0]
    import json

    if json_path.suffix == ".gz":
        import gzip

        with gzip.open(json_path, "rt", encoding="utf-8") as handle:
            data = json.load(handle) or {}
    else:
        data = json.loads(json_path.read_text(encoding="utf-8")) or {}
    rows = data.get("datasets")
    if isinstance(rows, dict):
        return {key_: dict(body or {}) for key_, body in rows.items()}
    return {name: {} for name in (data.get("available_datasets") or [])}


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='HDX', shard_noun='per-theme')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, HdxDataset]]:
    """Parse, validate, and cache the HDX catalog at `path`.

    Returns a `(available_datasets, datasets)` tuple. When `path` is a
    directory, every `*.yaml` file is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (a dataset
    key declared in two files is an error). Cached on the resolved path
    plus every contributing file's `mtime_ns`.

    Args:
        path: Catalog directory (default `src/earthlens/hdx/catalog/`)
            or a single `*.yaml` file.

    Returns:
        Tuple of `(list[str], dict[str, HdxDataset])` — the merged
            `available_datasets:` index and the curated `datasets:` map.

    Raises:
        ValueError: If no file has a `datasets:` block, a dataset key
            is declared in two files, or a dataset fails validation.
    """
    return load_catalog(
        path,
        _CATALOG_CACHE,
        _parse_catalog,
        provider="HDX",
        shard_noun="per-theme",
    )


def _parse_catalog(files: list[Path]) -> tuple[list[str], dict[str, HdxDataset]]:
    """Merge and validate the per-theme catalog shards.

    Args:
        files: The contributing YAML files, in sorted order.

    Returns:
        Tuple of `(list[str], dict[str, HdxDataset])` — the merged
            `available_datasets:` index and the curated `datasets:` map.

    Raises:
        ValueError: If no file has a `datasets:` block, a dataset key is
            declared in two files, or a dataset fails validation.
    """
    # Name the directory for a sharded catalog and the file for a single one, so
    # the "empty datasets:" error points at what the caller passed.
    path = files[0].parent if len(files) > 1 else files[0]
    merged_available: list[str] = []
    merged_datasets_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for ds_key, ds_body in (data.get("datasets") or {}).items():
            if ds_key in merged_datasets_yaml:
                raise ValueError(
                    f"dataset {ds_key!r} declared in two catalog files: "
                    f"{origin[ds_key]} and {file_path}"
                )
            merged_datasets_yaml[ds_key] = ds_body
            origin[ds_key] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The catalog must contain at least one curated dataset."
        )

    structural: dict[str, HdxDataset] = {}
    for ds_key, ds_body in merged_datasets_yaml.items():
        try:
            structural[ds_key] = HdxDataset(**dict(ds_body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[ds_key]} dataset {ds_key!r} failed validation:\n{exc}"
            ) from exc

    return merged_available, structural


class HdxDataset(BaseModel):
    """One curated HDX dataset row.

    Mirrors a single `datasets.<key>:` block in one of the per-theme
    `catalog/*.yaml` files. The friendly dataset key itself is the
    parent key in :attr:`Catalog.datasets` and is not stored on the
    row; :attr:`hdx_id` is the CKAN-side identifier the backend reads.

    Attributes:
        hdx_id: The HDX/CKAN dataset `name` (the identifier
            `Dataset.read_from_hdx` takes), e.g.
            `"kontur-population-dataset"`.
        org: The owning HDX organisation slug (`"kontur"`, `"hot"`,
            `"meta-data-for-good"`, …).
        title: Human-readable dataset title.
        themes: Theme tags this dataset belongs to (`["population"]`),
            used for browsing / the docs reference.
        formats: CKAN format *labels* the dataset's resources carry —
            `"Geopackage"`, `"CSV"`, `"GeoTIFF"`, `"GeoJSON"` (a label,
            not a file extension; this is what `r["format"]` returns).
        resource_filter: Optional default filter applied to the
            dataset's resources when the request names no explicit
            filter — a name glob (`"*.gpkg.gz"`) or a CKAN format label
            (`"Geopackage"`). Empty means "every resource".
        output_kinds: Informational — the pyramids output kinds the
            dataset's resources map onto (`["vector"]`, `["raster"]`,
            `["tabular"]`, or several when the dataset is mixed). The
            backend's `OUTPUT_KIND` is the fixed class value `"mixed"`;
            this field documents the per-dataset reality.

    Examples:
        - Inspect a curated vector row:
            ```python
            >>> from earthlens.hdx import Catalog
            >>> ds = Catalog().get_dataset("kontur-population")
            >>> ds.org
            'kontur'
            >>> ds.output_kinds
            ['vector']

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hdx_id: str
    org: str = ""
    title: str = ""
    themes: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    resource_filter: str = ""
    output_kinds: list[OutputKindLiteral] = Field(default_factory=list)


class Catalog(AbstractCatalog[HdxDataset]):
    """Dataset catalog for the Humanitarian Data Exchange backend.

    Reads the bundled `catalog/` directory (shipped as package data)
    and exposes its consumed top-level sections as typed pydantic
    fields. Instantiate with no arguments (`Catalog()`) —
    :func:`model_post_init` parses the YAML and populates every field
    in one pass.

    Attributes:
        available_datasets: The full index of every HDX dataset id (the
            whole `data.humdata.org` catalogue, ~41k), produced by the
            `refresh --all` tool. Any id here resolves to a thin
            :class:`HdxDataset` via :meth:`get_dataset` (the long-tail
            fallback, mirroring `earthlens.earthdata`'s `_auto.json`);
            the curated `datasets` carry vetted metadata.
        datasets: Structural map keyed by the curated dataset key. Each
            value is an :class:`HdxDataset`.

    Examples:
        - A curated key is a member; the long tail resolves but is not:
            ```python
            >>> from earthlens.hdx import Catalog
            >>> "kontur-population" in Catalog()
            True

            ```
    """

    _catalog_kind: str = "HDX catalog"

    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, HdxDataset] = Field(default_factory=dict)
    #: The full long-tail index `{hdx_id: {org, title}}`, used by
    #: :meth:`get_dataset` to synthesise enriched rows. Set by
    #: :meth:`load`; defaults to an empty map.
    _available_rows: dict[str, dict] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args is sugar for `Catalog.load()` — it
        reads the bundled `catalog/` directory through the
        `(path, mtime)`-keyed cache so repeated construction is fast. If
        the caller passed `datasets=...`, the disk read is skipped.

        Raises:
            ValueError: When auto-loading, propagates the same errors as
                :meth:`load`.
        """
        if self.datasets:
            return
        loaded = Catalog.load()
        self.available_datasets = loaded.available_datasets
        self.datasets = loaded.datasets
        self._available_rows = loaded._available_rows

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the HDX catalog from disk (cached).

        Args:
            catalog_path: Path to the `catalog/` directory or a single
                `*.yaml` file. Defaults to module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from :func:`_load_catalog_data`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        yaml_available, datasets = _load_catalog_data(catalog_path)
        # The bundled long-tail index lives in a sibling `_available.json`
        # ({id: {org, title}}, fast, out of the YAML glob); a custom catalog
        # dir may instead carry an `available_datasets:` block inside its
        # YAML (thin ids). Union both so either layout works.
        rows = dict(_load_available(_available_index_path(catalog_path)))
        for name in yaml_available:
            rows.setdefault(name, {})
        catalog = cls(
            available_datasets=sorted(rows),
            datasets=dict(datasets),
        )
        catalog._available_rows = rows
        return catalog

    def get_dataset(self, name: str) -> HdxDataset:
        """Resolve a key against the curated then the full HDX index.

        Curated `datasets` (hand-vetted, with full metadata) win.
        Otherwise, any id in the full long-tail index (the whole
        `data.humdata.org` catalogue) resolves to a synthesised
        :class:`HdxDataset` carrying its `hdx_id` plus the `org` / `title`
        recorded in `_available.json` (empty for the few ids the CKAN
        `package_search` walk does not expose). The `hdx_id` is the only
        load-bearing field — the backend fetches the dataset live via
        `Dataset.read_from_hdx` — so the row is fully usable. This is the
        long-tail fallback, mirroring `earthlens.earthdata`'s `_auto`
        resolution. An unknown id raises with a did-you-mean hint.

        Args:
            name: A curated key (e.g. `"kontur-population"`) or any HDX
                dataset id in the available index.

        Returns:
            HdxDataset: The curated row, or a thin synthesised row for an
                available id.

        Raises:
            ValueError: When `name` is in neither the curated map nor the
                available index.
        """
        if name in self.datasets:
            return self.datasets[name]
        row = self._available_rows.get(name)
        if row is not None:
            return HdxDataset(
                hdx_id=name,
                org=row.get("org", ""),
                title=row.get("title", ""),
            )
        import difflib

        # Fuzzy-match against the curated keys only: a mistyped key is
        # almost always a near-miss of a curated name, and running difflib
        # over the ~41k-id long tail on every miss would be needlessly slow.
        close = difflib.get_close_matches(name, list(self.datasets), n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{name!r} is not in the {self._catalog_kind} "
            f"({len(self.datasets)} curated + {len(self.available_datasets)} "
            f"available).{hint}"
        )

    def resolve(self, key: str) -> HdxDataset:
        """Resolve a curated dataset key to its :class:`HdxDataset` row.

        Thin wrapper over the base :meth:`get_dataset` (which raises a
        `ValueError` with a did-you-mean hint on an unknown key), named
        `resolve` to match the Earthdata backend's catalog surface.

        Args:
            key: Curated dataset key (a member of :attr:`datasets`).

        Returns:
            HdxDataset: The resolved row.

        Raises:
            ValueError: When `key` is unknown (with a did-you-mean
                hint).

        Examples:
            - Resolve a key and read its HDX id:
                ```python
                >>> from earthlens.hdx import Catalog
                >>> Catalog().resolve("kontur-population").hdx_id
                'kontur-population-dataset'

                ```
        """
        return self.get_dataset(key)
