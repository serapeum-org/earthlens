"""Variable / transport catalog for the Solar & Wind Atlas backend.

The backend serves a fixed, slow-changing set of Global Solar Atlas and Global
Wind Atlas climatology layers, so the catalog is curated as config-as-code in
the bundled `catalog/` directory — per-atlas `*.yaml` files (`solar.yaml`,
`wind.yaml`) plus an `_index.yaml` carrying the informational
`available_datasets:` list — and validated here against typed pydantic rows.
The loader merges every file at construction time (the `ghsl` / `cmems` /
`bathymetry` sharded pattern) through a `(path, mtime_ns)` parse cache.

Each row pins the layer's `atlas` (`"gsa"` / `"gwa"`), its `transport`
(`"vsicurl"` windowed COG read for the wind layers, `"download_zip"`
download-then-localise for the solar layers — see the A1 gate), and the
download `url`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

#: Path to the bundled catalog directory of per-atlas `*.yaml` files plus the
#: `_index.yaml` informational index. Tests can monkey-patch this attribute to
#: redirect the loader at a temporary directory or a single YAML file.
CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Module-level cache of parsed catalog data, keyed on the resolved path plus a
#: tuple of `(file, mtime_ns)` for every YAML the load touched, so editing any
#: per-atlas file invalidates the entry without re-parsing an unchanged tree.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, Layer]]] = CatalogParseCache()

#: The two source atlases a row may belong to.
Atlas = Literal["gsa", "gwa"]

#: The two transports a row may declare. `"vsicurl"` is the windowed remote-COG
#: read (Global Wind Atlas figshare layers); `"download_zip"` downloads the
#: deflate ZIP once and reads the bbox from the local member (Global Solar
#: Atlas layers).
Transport = Literal["vsicurl", "download_zip"]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force a
    re-parse. Production callers do not need this — the cache keys include
    every contributing file's `st_mtime_ns`, so any real file mutation
    invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()


class Layer(BaseModel):
    """One curated Solar / Wind Atlas layer.

    Attributes:
        id: Catalog key for the row (`"ghi"`, `"wind_100m"`). Set from the
            catalog key by the loader.
        atlas: Which atlas the layer comes from — `"gsa"` (Global Solar Atlas)
            or `"gwa"` (Global Wind Atlas).
        transport: How the bbox window is read — `"vsicurl"` (windowed remote
            COG read, the wind layers) or `"download_zip"` (download the deflate
            ZIP once, then read the local member, the solar layers).
        url: The download URL — a figshare COG URL for `"vsicurl"` rows, a
            Global Solar Atlas `*.zip` URL for `"download_zip"` rows.
        units: Human-readable unit of the values (`"kWh/m2/day"`, `"m/s"`).
        long_name: One-line human-readable description.
        license_note: Attribution / licence text surfaced in docs and logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    atlas: Atlas
    transport: Transport
    url: str
    units: str = ""
    long_name: str = ""
    license_note: str = ""


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='solar_wind_atlas', shard_noun='per-atlas')


def _load_catalog_data(path: Path) -> tuple[list[str], dict[str, Layer]]:
    """Parse, validate, and cache the Solar & Wind Atlas catalog at `path`.

    When `path` is a directory, every `*.yaml` is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (an id declared in
    two files is an error). Cached on the resolved path plus every contributing
    file's `mtime_ns`, so a second `Catalog()` on an unchanged tree skips both
    YAML parsing and pydantic validation.

    Args:
        path: Catalog directory (default `CATALOG_PATH`) or a single `*.yaml`.

    Returns:
        tuple[list[str], dict[str, Layer]]: The merged `available_datasets:`
            index and the curated layer map (keyed by id).

    Raises:
        ValueError: If no file has a `datasets:` block, an id is declared in
            two files, a row fails validation, or a curated id is absent from
            `available_datasets:`.
    """
    files = _yaml_files_for(path)
    key = catalog_cache_key(path, files)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    merged_available: list[str] = []
    merged_datasets_yaml: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        merged_available.extend(data.get("available_datasets") or [])
        for code, body in (data.get("datasets") or {}).items():
            if code in merged_datasets_yaml:
                raise ValueError(
                    f"layer {code!r} declared in two catalog files: "
                    f"{origin[code]} and {file_path}"
                )
            merged_datasets_yaml[code] = body
            origin[code] = file_path

    if not merged_datasets_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The solar_wind_atlas catalog must list at least one layer."
        )

    available = set(merged_available)
    datasets: dict[str, Layer] = {}
    for code, body in merged_datasets_yaml.items():
        try:
            datasets[code] = Layer(id=code, **dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{origin[code]} layer {code!r} failed validation:\n{exc}"
            ) from exc
        if available and code not in available:
            raise ValueError(
                f"layer {code!r} is in 'datasets:' but missing from "
                f"'available_datasets:' ({origin[code]}); add it to "
                "_index.yaml too."
            )

    _CATALOG_CACHE[key] = (merged_available, datasets)
    return _CATALOG_CACHE[key]


class Catalog(AbstractCatalog):
    """Variable / transport catalog for the Solar & Wind Atlas backend.

    Merges the bundled `catalog/` directory's per-atlas `*.yaml` files and
    exposes their `datasets:` blocks as a map of `Layer` rows keyed by id under
    the inherited `datasets` field (giving `cat["ghi"]`, `"ghi" in cat`,
    `len(cat)`, and the did-you-mean error for free). Instantiate with no
    arguments (`Catalog()`); `model_post_init` loads and validates the catalog
    through the parse cache.

    Attributes:
        datasets: Map from layer id to its `Layer` row.
        available_datasets: Every layer id from `_index.yaml`. The curated set
            is the full shipped surface, so this equals the curated keys.
    """

    _catalog_kind: str = "Solar & Wind Atlas catalog"
    _entry_noun: str = "layers"

    datasets: dict[str, Layer] = Field(default_factory=dict)
    _alias_index: dict[str, str] = PrivateAttr(default_factory=dict)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: The `datasets`, `available_datasets` read from
                the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "available_datasets": loaded.available_datasets,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the layer catalog from disk (directory or single file).

        Args:
            catalog_path: Catalog directory or single YAML file. Defaults to
                the module-level `CATALOG_PATH`.

        Returns:
            A fully-populated `Catalog`.

        Raises:
            ValueError: If no file has a `datasets:` block, an id is declared
                in two files, a row fails `Layer` validation, or a curated id
                is absent from `available_datasets:`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        available, datasets = _load_catalog_data(catalog_path)
        return cls(
            datasets=dict(datasets),
            available_datasets=list(available),
        )

    def get_catalog(self) -> dict[str, Layer]:
        """Return the layer map (satisfies the abstract contract)."""
        return self.datasets

    def get(self, layer_id: str) -> Layer:
        """Return the `Layer` for a curated id, did-you-mean on miss.

        Args:
            layer_id: A curated layer id (`"ghi"`, `"wind_100m"`).

        Returns:
            Layer: The matching row.

        Raises:
            ValueError: If `layer_id` is not a curated layer; the message
                lists the known ids with a did-you-mean hint.

        Examples:
            - A solar and a wind layer resolve to their atlas / transport:
                ```python
                >>> from earthlens.solar_wind_atlas import Catalog
                >>> Catalog().get("ghi").atlas
                'gsa'
                >>> Catalog().get("wind_100m").transport
                'vsicurl'

                ```
        """
        return cast("Layer", self.get_dataset(layer_id))

    def available(self) -> list[str]:
        """Return the curated layer ids, sorted.

        Returns:
            list[str]: The curated layer ids (`["air_density_100m", ...]`).
        """
        return sorted(self.datasets)
