"""Catalog for the CatRaRE heavy-rainfall event-catalogue backend.

The CatRaRE backend fetches the DWD Catalogue of Radar-based Rainfall Events
(v2026.01). Two threshold selections are published — `T5` (return period
>= 5 yr) and `W3` (severity-weighted) — so the catalog exposes them as a map of
:class:`CatRaReDataset` rows under the inherited :attr:`datasets` field, keyed
`"t5"` / `"w3"`, alongside the shared `base_url`, `version` / `version_tag` /
`years`, the RADOLAN `source_crs`, the `geometry_layers` stems, the
`event_columns` to keep, and the `license` / `attribution`. The concrete
download URL and FileGDB layer name are composed by :meth:`Catalog.download_url`
/ :meth:`Catalog.layer_name`.

:data:`CATALOG_PATH` is the path to the bundled YAML; it is monkey-patchable in
tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catrare_data_catalog.yaml"

#: Module-level parse cache keyed on the resolved path plus the YAML's
#: `(mtime_ns, size)`, so a repeated `Catalog()` skips the parse + validation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache (for tests that rewrite YAML)."""
    _CATALOG_CACHE.clear()


class CatRaReDataset(BaseModel):
    """One CatRaRE threshold selection's spec.

    The threshold key (`"t5"` / `"w3"`) is the parent key in
    :attr:`Catalog.datasets`, not stored on the row.

    Attributes:
        threshold: The threshold code embedded in the file / layer names
            (`"T5"` or `"W3"`).
        description: Human-readable description of the selection.

    Examples:
        - Read a threshold code:
            ```python
            >>> from earthlens.catrare import Catalog
            >>> Catalog().get("t5").threshold
            'T5'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: str
    description: str = ""


def _parse_thresholds(
    rows_yaml: dict[str, Any], path: Path
) -> dict[str, CatRaReDataset]:
    """Validate the `datasets:` rows into :class:`CatRaReDataset` instances.

    Args:
        rows_yaml: The raw `name -> body` mapping from the catalog YAML.
        path: The catalog path (for the error message).

    Returns:
        dict[str, CatRaReDataset]: One validated row per threshold key.

    Raises:
        ValueError: If any row fails validation.
    """
    datasets: dict[str, CatRaReDataset] = {}
    for name, body in rows_yaml.items():
        try:
            datasets[name] = CatRaReDataset(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} dataset {name!r} failed validation:\n{exc}"
            ) from exc
    return datasets


def _str_map(data: dict[str, Any], key: str) -> dict[str, str]:
    """Return `data[key]` coerced to a `str -> str` mapping (empty when absent)."""
    return {str(k): str(v) for k, v in (data.get(key) or {}).items()}


def _load_catalog_data(path: Path) -> dict[str, Any]:
    """Parse, validate, and cache the catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        dict[str, Any]: Every field a :class:`Catalog` is built from.

    Raises:
        ValueError: If the file has no `datasets:` block or a row fails
            validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cast("dict[str, Any]", cached)

    data = load_yaml_strict(path) or {}
    rows_yaml = data.get("datasets") or {}
    if not rows_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. The CatRaRE "
            "catalog must list at least one threshold (t5 / w3)."
        )

    value: dict[str, Any] = {
        "datasets": _parse_thresholds(rows_yaml, path),
        "base_url": str(data.get("base_url") or "").rstrip("/"),
        "version": str(data.get("version") or ""),
        "version_tag": str(data.get("version_tag") or ""),
        "years": str(data.get("years") or ""),
        "source_crs": str(data.get("source_crs") or ""),
        "license": str(data.get("license") or ""),
        "attribution": str(data.get("attribution") or ""),
        "geometry_layers": _str_map(data, "geometry_layers"),
        "date_columns": _str_map(data, "date_columns"),
        "event_columns": [str(c) for c in (data.get("event_columns") or [])],
    }
    _CATALOG_CACHE[key] = value
    return value


class Catalog(AbstractCatalog[CatRaReDataset]):
    """Catalog for the CatRaRE heavy-rainfall event-catalogue backend.

    Reads the bundled `catrare_data_catalog.yaml` (shipped as package data) and
    exposes its `t5` / `w3` threshold rows under the inherited :attr:`datasets`
    map, plus the shared metadata used to build a download URL and FileGDB layer
    name. Instantiate with no arguments (`Catalog()`); resolve a threshold with
    :meth:`get`.

    Attributes:
        datasets: Map from threshold key (`"t5"` / `"w3"`) to its
            :class:`CatRaReDataset` row.
        base_url: DWD open-data host prefix.
        version: Dotted directory version (`"v2026.01"`).
        version_tag: The tag embedded in file / layer names (`"v2026_01"`).
        years: The year span in file names (`"2001_2025"`).
        source_crs: The DWD RADOLAN proj4 string the FileGDB geometry is in
            (the file itself carries no CRS).
        license: SPDX-style redistribution licence (`"CC-BY-4.0"`).
        attribution: Required attribution string.
        geometry_layers: Layer name stem per geometry kind (`zones` ->
            `"EventZones"`, `points` -> `"RRmaxPoints"`).
        date_columns: The `start` / `end` event-interval column names the
            date-window filter compares against (`Date_START` / `Date_END`).
        event_columns: Event attribute columns kept on every returned feature.

    Examples:
        - Build a download URL and layer name for a threshold:
            ```python
            >>> from earthlens.catrare import Catalog
            >>> cat = Catalog()
            >>> cat.download_url("t5").endswith("CatRaRE_2001_2025_T5_Eta_v2026_01.gdb.zip")
            True
            >>> cat.layer_name("t5", "zones")
            'CatRaRE_2001_2025_T5_Eta_EventZones_v2026_01'

            ```
    """

    _catalog_kind: str = "CatRaRE catalog"
    _entry_noun: str = "thresholds"

    datasets: dict[str, CatRaReDataset] = Field(default_factory=dict)
    base_url: str = ""
    version: str = ""
    version_tag: str = ""
    years: str = ""
    source_crs: str = ""
    license: str = ""
    attribution: str = ""
    geometry_layers: dict[str, str] = Field(default_factory=dict)
    date_columns: dict[str, str] = Field(default_factory=dict)
    event_columns: list[str] = Field(default_factory=list)

    @classmethod
    def _autoload(cls) -> dict[str, Any]:
        """Read the bundled catalog from disk.

        Returns:
            dict[str, Any]: Every field read from the bundled catalog.
        """
        loaded = Catalog.load()
        return {
            "datasets": loaded.datasets,
            "base_url": loaded.base_url,
            "version": loaded.version,
            "version_tag": loaded.version_tag,
            "years": loaded.years,
            "source_crs": loaded.source_crs,
            "license": loaded.license,
            "attribution": loaded.attribution,
            "geometry_layers": loaded.geometry_layers,
            "date_columns": loaded.date_columns,
            "event_columns": loaded.event_columns,
        }

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the CatRaRE catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block or a row fails
                validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        return cls(**_load_catalog_data(catalog_path))

    def get(self, threshold: str) -> CatRaReDataset:
        """Resolve a threshold key to its :class:`CatRaReDataset` row.

        Thin wrapper over the inherited :meth:`get_dataset`, which raises a
        `ValueError` with a did-you-mean hint on an unknown key.

        Args:
            threshold: A shipped threshold key (`"t5"` / `"w3"`).

        Returns:
            CatRaReDataset: The matching catalog row.

        Raises:
            ValueError: If `threshold` is not a known threshold.
        """
        return cast("CatRaReDataset", self.get_dataset(threshold))

    def _file_stem(self, threshold: str) -> str:
        """Return the shared file/layer stem `CatRaRE_<years>_<T>_Eta`.

        Args:
            threshold: A shipped threshold key.

        Returns:
            str: The stem, e.g. `"CatRaRE_2001_2025_T5_Eta"`.
        """
        code = self.get(threshold).threshold
        return f"CatRaRE_{self.years}_{code}_Eta"

    def download_url(self, threshold: str) -> str:
        """Return the FileGDB download URL for a threshold.

        Args:
            threshold: A shipped threshold key (`"t5"` / `"w3"`).

        Returns:
            str: The absolute `.gdb.zip` URL under the versioned data directory.

        Raises:
            ValueError: If `threshold` is not a known threshold.
        """
        stem = self._file_stem(threshold)
        return (
            f"{self.base_url}/CatRaRE_{self.version}/data/"
            f"{stem}_{self.version_tag}.gdb.zip"
        )

    def layer_name(self, threshold: str, geometry: str) -> str:
        """Return the FileGDB layer name for a threshold + geometry kind.

        Args:
            threshold: A shipped threshold key (`"t5"` / `"w3"`).
            geometry: A geometry kind key (`"zones"` / `"points"`).

        Returns:
            str: The layer name, e.g.
                `"CatRaRE_2001_2025_T5_Eta_EventZones_v2026_01"`.

        Raises:
            ValueError: If `threshold` or `geometry` is unknown.
        """
        if geometry not in self.geometry_layers:
            raise ValueError(
                f"geometry {geometry!r} is not a CatRaRE geometry kind. Known: "
                f"{sorted(self.geometry_layers)}."
            )
        stem = self._file_stem(threshold)
        return f"{stem}_{self.geometry_layers[geometry]}_{self.version_tag}"
