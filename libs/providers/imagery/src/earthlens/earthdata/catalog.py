"""Dataset-catalog loader for the NASA Earthdata backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
Earthdata catalog. Mirrors the shape of :mod:`earthlens.cmems.catalog`
and :mod:`earthlens.gee.catalog`: the catalog ships as a directory of
per-DAAC YAML files at `src/earthlens/earthdata/catalog/`
(`ges_disc.yaml`, `po_daac.yaml`, `lp_daac.yaml`, …) plus a single
`_index.yaml` carrying the merged `available_datasets:` list. Each
per-DAAC file contributes its `datasets:` block; the loader unions
them into one :class:`Catalog` at construction time.

A dataset key (e.g. `"GPM_3IMERGHHL_07"`) resolves to an
:class:`EarthdataDataset` via :meth:`Catalog.get_dataset` /
`Catalog()["..."]` / :meth:`Catalog.resolve`. The row carries the
fields the backend needs to shape a CMR search and a fetch:
`short_name` / `version` / `provider` (the CMR provider code), the
per-instance `output_kind` (`G1`), the on-disk `format`, the
informational `bands`, and the cloud-hosting flags that gate the
in-region S3 streaming path (`G4`).

`available_datasets:` is the informational index of every collection
the CMR walk found per provider (the `C7` auto-generated index); the
curated `datasets:` map is the small vetted subset the maintainer
hand-checks. The path to the bundled catalog directory lives at
:data:`CATALOG_PATH`; tests can monkey-patch that module attribute to
redirect the loader at a temporary directory or single YAML file.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
)

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import (
    catalog_cache_key,
    load_catalog,
    yaml_files_for,
)
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"
PROVIDERS_PATH: Path = Path(__file__).parent / "providers.yaml"
#: Auto-generated long-tail rows (machine-derived from a CMR walk: real
#: short_name / version / provider + a heuristic output_kind, no bands). Kept
#: separate from the hand-vetted `datasets:` so the two never mix — and stored
#: as JSON (not `*.yaml`) so the ~8k rows parse in milliseconds and stay out of
#: the curated YAML glob.
AUTO_PATH: Path = Path(__file__).parent / "catalog" / "_auto.json"

# Module-level cache of parsed catalog data, keyed on the resolved
# path plus a tuple of `(file, mtime_ns)` for every YAML the load
# touched, so editing any per-DAAC file invalidates the entry without
# inspecting every row. Mirrors the CMEMS / GEE multi-file pattern.
_CATALOG_CACHE: dict[Any, tuple[list[str], dict[str, EarthdataDataset]]] = (
    CatalogParseCache()
)
# Same `(path, mtime_ns)` cache for the DAAC provider registry.
_PROVIDERS_CACHE: dict[Any, dict[str, EarthdataDAAC]] = CatalogParseCache()
# …and for the auto-generated long-tail rows (kept as raw dicts — a model is
# built only for the one key a caller resolves, so membership/resolution never
# instantiates all ~8k pydantic rows).
_AUTO_CACHE: CatalogParseCache = CatalogParseCache()

OutputKindLiteral = Literal["raster", "vector", "tabular"]

CadenceLiteral = Literal[
    "subhourly",
    "hourly",
    "3hourly",
    "6hourly",
    "daily",
    "8day",
    "16day",
    "weekly",
    "monthly",
    "annual",
    "static",
    "irregular",
]


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force
    a re-parse. Production callers do not need this — the cache keys
    include every contributing file's `st_mtime_ns`, so any real file
    mutation invalidates the entry on its own.
    """
    _CATALOG_CACHE.clear()
    _PROVIDERS_CACHE.clear()
    _AUTO_CACHE.clear()


def _yaml_files_for(path: Path) -> list[Path]:
    """Return the sorted YAML files contributing to a load.

    Binds the shared `yaml_files_for` to this catalog's provider label. Kept
    as a module-level name because the tests import and monkey-patch it.
    """
    return yaml_files_for(path, provider='Earthdata', shard_noun='per-DAAC')


def _load_providers(path: Path) -> dict[str, EarthdataDAAC]:
    """Parse, validate, and cache the DAAC provider registry.

    Args:
        path: Path to `providers.yaml`.

    Returns:
        dict[str, EarthdataDAAC]: Map keyed by CMR provider code.

    Raises:
        ValueError: If the file has no `daacs:` block or a DAAC entry
            fails validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _PROVIDERS_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    daacs_yaml = data.get("daacs") or {}
    if not daacs_yaml:
        raise ValueError(f"{path} is missing or has an empty 'daacs:' block.")
    daacs: dict[str, EarthdataDAAC] = {}
    for code, body in daacs_yaml.items():
        try:
            daacs[code] = EarthdataDAAC(cmr_provider=code, **(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} provider {code!r} failed validation:\n{exc}"
            ) from exc
    _PROVIDERS_CACHE[key] = daacs
    return daacs


def _load_auto_raw(path: Path) -> dict[str, dict]:
    """Read and cache the auto-generated long-tail rows as raw dicts.

    Reads the `auto_datasets` block of `_auto.json` (machine-derived from
    a CMR walk) without instantiating pydantic models — a model is built
    only for the single key a caller resolves, so membership checks and
    resolution never pay for all ~8k rows. Returns an empty map when the
    file is absent.

    Args:
        path: Path to `_auto.yaml`.

    Returns:
        dict[str, dict]: Raw row bodies keyed by `short_name`.
    """
    if not path.is_file():
        return {}
    return load_catalog(path, _AUTO_CACHE, _parse_auto, provider="Earthdata")


def _parse_auto(files: list[Path]) -> dict[str, dict]:
    """Read the `auto_datasets` block out of `_auto.json`.

    Args:
        files: The single `_auto.json` path, from the shared loader.

    Returns:
        dict[str, dict]: Raw row bodies keyed by `short_name`.
    """
    import json

    data = json.loads(files[0].read_text(encoding="utf-8")) or {}
    return {k: dict(v or {}) for k, v in (data.get("auto_datasets") or {}).items()}


def _load_catalog_data(
    path: Path,
) -> tuple[list[str], dict[str, EarthdataDataset]]:
    """Parse, validate, and cache the Earthdata catalog at `path`.

    Returns a `(available_datasets, datasets)` tuple. When `path` is a
    directory, every `*.yaml` file is merged: `available_datasets:`
    lists are concatenated and `datasets:` maps are unioned (a dataset
    key declared in two files is an error). Cached on the resolved
    path plus every contributing file's `mtime_ns`.

    Args:
        path: Catalog directory (default
            `src/earthlens/earthdata/catalog/`) or a single `*.yaml`
            file.

    Returns:
        Tuple of `(list[str], dict[str, EarthdataDataset])` — the
            merged `available_datasets:` index and the curated
            `datasets:` map.

    Raises:
        ValueError: If no file has a `datasets:` block, a dataset key
            is declared in two files, or a dataset fails validation.
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

    structural: dict[str, EarthdataDataset] = {}
    for ds_key, ds_body in merged_datasets_yaml.items():
        body = dict(ds_body or {})
        temporal_body = body.pop("temporal", None) or {}
        bands_body = body.pop("bands", None) or {}
        try:
            structural[ds_key] = EarthdataDataset(
                temporal=TemporalCoverage(
                    start=temporal_body.get("start"),
                    end=temporal_body.get("end"),
                ),
                bands={name: Band(**(meta or {})) for name, meta in bands_body.items()},
                **body,
            )
        except ValidationError as exc:
            raise ValueError(
                f"{origin[ds_key]} dataset {ds_key!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = (merged_available, structural)
    return _CATALOG_CACHE[key]


class EarthdataDAAC(BaseModel):
    """One DAAC's entry in the CMR provider registry (`providers.yaml`).

    Maps a CMR provider code (the `provider:` field on a dataset row,
    passed straight to `earthaccess.search_data`) to its DAAC name,
    landing page, and — for cloud-hosted collections — the us-west-2
    region and the S3 credentials endpoint `earthaccess` uses to mint
    rotating keys (`G4`).

    Attributes:
        cmr_provider: The CMR provider code (also the registry key),
            e.g. `"GES_DISC"`, `"POCLOUD"`, `"LPCLOUD"`.
        daac: Short DAAC id (e.g. `"GES DISC"`, `"PO.DAAC"`).
        display_name: Full human-readable DAAC name.
        landing_page: DAAC home page URL.
        cloud_region: AWS region the DAAC's cloud holdings live in
            (`"us-west-2"` for every current DAAC).
        s3_credentials_endpoint: URL that vends rotating S3 credentials
            for in-region streaming.

    Examples:
        - Look up a DAAC by provider code:
            ```python
            >>> from earthlens.earthdata import Catalog
            >>> Catalog().get_daac("GES_DISC").daac
            'GES DISC'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cmr_provider: str
    daac: str = ""
    display_name: str = ""
    landing_page: str = ""
    cloud_region: str = "us-west-2"
    s3_credentials_endpoint: str = ""


class Band(BaseModel):
    """One band / variable's metadata row inside an Earthdata dataset.

    Bands are **informational** for the whole-granule fetch the MVP
    performs — you receive the entire granule, not a per-band subset
    (that needs Harmony, deferred). They seed catalog metadata and the
    future server-side subset request.

    Attributes:
        long_name: Human-readable description of the band.
        units: CF-style unit string, when known.

    Examples:
        - Build a band row:
            ```python
            >>> from earthlens.earthdata.catalog import Band
            >>> b = Band(long_name="Precipitation", units="mm/hr")
            >>> b.units
            'mm/hr'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    long_name: str = ""
    units: str = ""


class TemporalCoverage(BaseModel):
    """Temporal coverage of an Earthdata dataset (start + optional end).

    Mirrors the `temporal:` block in the YAML. `end: null` (or a
    missing `end`) means the dataset is near-real-time / rolling.

    Attributes:
        start: First date with data, as a `YYYY-MM-DD` string. May be
            `None` when the start date is not pinned in the YAML.
        end: Last date with data, or `None` for a rolling product.

    Examples:
        - Bounded coverage:
            ```python
            >>> from earthlens.earthdata.catalog import TemporalCoverage
            >>> tc = TemporalCoverage(start="2000-06-01", end="2023-12-31")
            >>> tc.start, tc.end
            ('2000-06-01', '2023-12-31')

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str | None = None
    end: str | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        """Accept a `datetime.date` (PyYAML's native parse) as ISO string.

        Args:
            value: Raw YAML value — a string, a `datetime.date`, or
                `None`.

        Returns:
            An ISO-format string (`"YYYY-MM-DD"`) or `None`.
        """
        if isinstance(value, _dt.date):
            return value.isoformat()
        return value


class EarthdataDataset(BaseModel):
    """One curated Earthdata dataset row.

    Mirrors a single `datasets.<key>:` block in one of the per-DAAC
    `catalog/*.yaml` files. The dataset key itself is the parent key
    in :attr:`Catalog.datasets` and is not stored on the row.

    Attributes:
        short_name: CMR `short_name` (the collection identifier the
            search uses), e.g. `"GPM_3IMERGHHL"`.
        version: Collection version string, e.g. `"07"`. Empty means
            "latest / unversioned".
        doi: Optional dataset DOI.
        daac: Short DAAC id this collection belongs to (`"GES_DISC"`,
            `"PO.DAAC"`, `"LP DAAC"`, …) — for browsing / disambiguation.
        provider: CMR provider code used in the search (`"GES_DISC"`,
            `"POCLOUD"`, `"LPCLOUD"`, …).
        cadence: Native temporal cadence (advisory).
        format: On-disk granule format (`"netcdf4"`, `"hdf-eos2"`,
            `"hdf5"`, `"cog"`, `"geopackage"`, …). Drives the reader
            on the `aggregate=` path.
        output_kind: The per-dataset output shape — `"raster"`,
            `"vector"`, or `"tabular"`. Copied onto the backend
            instance's `OUTPUT_KIND` (`G1`).
        cloud_hosted: Whether the collection is hosted in AWS
            `us-west-2`; gates the in-region S3 streaming path (`G4`).
        cloud_bucket: The `s3://…` bucket prefix, when known.
        requires_harmony_for_subset: Informational — whether a
            band/bbox subset needs Harmony (deferred, `G5`).
        supports_harmony: Informational — whether the collection is
            Harmony-enabled at all.
        bands: Per-band metadata map (informational for whole-granule
            fetch).
        temporal: :class:`TemporalCoverage` — start / end dates.

    Examples:
        - Inspect a curated raster row:
            ```python
            >>> from earthlens.earthdata import Catalog
            >>> ds = Catalog().get_dataset("GPM_3IMERGHHL_07")
            >>> ds.provider
            'GES_DISC'
            >>> ds.output_kind
            'raster'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    short_name: str
    version: str = ""
    doi: str | None = None
    daac: str = ""
    provider: str = ""
    cadence: CadenceLiteral = "irregular"
    format: str = ""
    output_kind: OutputKindLiteral = "raster"
    cloud_hosted: bool = False
    cloud_bucket: str | None = None
    requires_harmony_for_subset: bool = False
    supports_harmony: bool = False
    bands: dict[str, Band] = Field(default_factory=dict)
    temporal: TemporalCoverage = Field(default_factory=TemporalCoverage)


class Catalog(AbstractCatalog[EarthdataDataset]):
    """Dataset catalog for the NASA Earthdata backend.

    Reads the bundled `catalog/` directory (shipped as package data)
    and exposes its consumed top-level sections as typed pydantic
    fields. Instantiate with no arguments (`Catalog()`) —
    :func:`model_post_init` parses the YAML and populates every field
    in one pass.

    Attributes:
        available_datasets: Informational list of every collection the
            CMR walk found per provider (the `C7` index). Runtime code
            does not consume it.
        datasets: Structural map keyed by the curated dataset key. Each
            value is an :class:`EarthdataDataset`.

    Examples:
        - Resolve a curated dataset:
            ```python
            >>> from earthlens.earthdata import Catalog
            >>> "GPM_3IMERGHHL_07" in Catalog()
            True

            ```
    """

    _catalog_kind: str = "Earthdata catalog"

    available_datasets: list[str] = Field(default_factory=list)
    datasets: dict[str, EarthdataDataset] = Field(default_factory=dict)
    daacs: dict[str, EarthdataDAAC] = Field(default_factory=dict)
    # Machine-derived long tail (~8k rows) held as raw dicts, read lazily on
    # first access. A pydantic row is built only for the key a caller resolves,
    # so `Catalog()` and curated-only ops never pay for the 8k parse.
    _auto_raw: dict[str, dict] | None = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args is sugar for `Catalog.load()` — it
        reads the bundled `catalog/` directory and `providers.yaml`
        through the `(path, mtime)`-keyed caches so repeated
        construction is fast. If the caller passed `datasets=...`, the
        disk read is skipped.

        Raises:
            ValueError: When auto-loading, propagates the same errors
                as :meth:`load`.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.available_datasets = loaded.available_datasets
            self.datasets = loaded.datasets
            self.daacs = loaded.daacs
        if not self.providers:
            self.providers = dict(self.daacs)
        super().model_post_init(__context)

    @classmethod
    def load(
        cls,
        catalog_path: Path | None = None,
        providers_path: Path | None = None,
    ) -> Catalog:
        """Read the Earthdata catalog + provider registry from disk (cached).

        Args:
            catalog_path: Path to the `catalog/` directory or a single
                `*.yaml` file. Defaults to module-level
                :data:`CATALOG_PATH`.
            providers_path: Path to `providers.yaml`. Defaults to
                module-level :data:`PROVIDERS_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: Propagated from :func:`_load_providers` /
                :func:`_load_catalog_data`, including when a dataset
                row names a `provider:` absent from `providers.yaml`.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        providers_path = (
            providers_path if providers_path is not None else PROVIDERS_PATH
        )
        daacs = _load_providers(providers_path)
        available_datasets, datasets = _load_catalog_data(catalog_path)
        unknown = {
            ds.provider
            for ds in datasets.values()
            if ds.provider and ds.provider not in daacs
        }
        if unknown:
            raise ValueError(
                f"dataset rows reference provider code(s) not in "
                f"{providers_path.name}: {sorted(unknown)}. Known codes: "
                f"{sorted(daacs)}."
            )
        return cls(
            available_datasets=list(available_datasets),
            datasets=dict(datasets),
            daacs=dict(daacs),
        )

    def _auto_rows(self) -> dict[str, dict]:
        """Return the raw auto-row bodies, reading `_auto.yaml` on first call."""
        if self._auto_raw is None:
            self._auto_raw = _load_auto_raw(AUTO_PATH)
        return self._auto_raw

    @property
    def auto_datasets(self) -> dict[str, EarthdataDataset]:
        """The machine-derived long-tail rows as a built map.

        Builds an :class:`EarthdataDataset` for every auto row — heavy
        (~8k rows). For membership or single-key resolution prefer
        :meth:`get_dataset`, which builds only the one row requested.

        Returns:
            dict[str, EarthdataDataset]: Map keyed by `short_name`
                (empty when `_auto.yaml` is absent).
        """
        return {k: self._build_auto(k) for k in self._auto_rows()}

    def _build_auto(self, name: str) -> EarthdataDataset:
        """Build the :class:`EarthdataDataset` for one auto row."""
        body = {k: v for k, v in self._auto_rows()[name].items() if k != "bands"}
        return EarthdataDataset(**body)

    def get_dataset(self, name: str) -> EarthdataDataset:
        """Resolve a dataset key against the curated then the auto map.

        Curated `datasets` (hand-vetted, with bands) win; the
        machine-derived auto long tail (read lazily from `_auto.yaml`) is
        the fallback — only the resolved row is instantiated. An unknown
        key raises with a did-you-mean hint drawn from both.

        Args:
            name: A curated key (e.g. `"GPM_3IMERGHHL_07"`) or an auto
                key (a bare `short_name`, e.g. `"AA_L2A"`).

        Returns:
            EarthdataDataset: The resolved row.

        Raises:
            ValueError: When `name` is in neither map.
        """
        if name in self.datasets:
            return self.datasets[name]
        auto = self._auto_rows()
        if name in auto:
            return self._build_auto(name)
        import difflib

        pool = list(self.datasets) + list(auto)
        close = difflib.get_close_matches(name, pool, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{name!r} is not in the {self._catalog_kind} "
            f"({len(self.datasets)} curated + {len(auto)} auto).{hint}"
        )

    def get_daac(self, provider_code: str) -> EarthdataDAAC:
        """Return the :class:`EarthdataDAAC` for a CMR provider code.

        Args:
            provider_code: A CMR provider code registered in
                `providers.yaml` (e.g. `"GES_DISC"`, `"POCLOUD"`).

        Returns:
            EarthdataDAAC: The matching DAAC entry.

        Raises:
            KeyError: If `provider_code` is not registered (with a
                did-you-mean hint).

        Examples:
            - Look up a DAAC's region by provider code:
                ```python
                >>> from earthlens.earthdata import Catalog
                >>> Catalog().get_daac("POCLOUD").cloud_region
                'us-west-2'

                ```
            - An unknown code surfaces a did-you-mean hint:
                ```python
                >>> from earthlens.earthdata import Catalog
                >>> Catalog().get_daac("POCLOD")
                Traceback (most recent call last):
                    ...
                KeyError: "'POCLOD' is not a registered CMR provider. Known: ['ASF', 'GES_DISC', 'LAADS', 'LARC_CLOUD', 'LPCLOUD', 'NSIDC_CPRD', 'OB_CLOUD', 'ORNL_CLOUD', 'POCLOUD']. Did you mean 'POCLOUD'?"

                ```
        """
        try:
            return self.daacs[provider_code]
        except KeyError:
            import difflib

            close = difflib.get_close_matches(provider_code, self.daacs, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise KeyError(
                f"{provider_code!r} is not a registered CMR provider. "
                f"Known: {sorted(self.daacs)}.{hint}"
            ) from None

    def resolve(self, key: str, daac: str | None = None) -> EarthdataDataset:
        """Resolve a dataset key (optionally disambiguated by DAAC).

        Most keys map one-to-one to a curated row; `get_dataset` (with
        its did-you-mean) handles those. The `daac=` filter exists for
        the rare case where two curated rows share a `short_name`
        served by different providers and the caller distinguishes
        them by DAAC.

        Args:
            key: Curated dataset key (a member of :attr:`datasets`).
            daac: Optional DAAC id to disambiguate; when given, the
                resolved row's `daac` must match.

        Returns:
            EarthdataDataset: The resolved row.

        Raises:
            ValueError: When `key` is unknown (with a did-you-mean hint)
                or when `daac=` is given but does not match the row.
                Both not-found conditions raise `ValueError`, matching
                :meth:`get_dataset`.

        Examples:
            - Resolve a key and read its provider:
                ```python
                >>> from earthlens.earthdata import Catalog
                >>> Catalog().resolve("GPM_3IMERGHHL_07").provider
                'GES_DISC'

                ```
            - A wrong `daac=` filter is rejected:
                ```python
                >>> from earthlens.earthdata import Catalog
                >>> Catalog().resolve("ATL08_006", daac="GES DISC")
                Traceback (most recent call last):
                    ...
                ValueError: dataset 'ATL08_006' resolves to DAAC 'NSIDC', not the requested daac='GES DISC'.

                ```
        """
        dataset = self.get_dataset(key)
        if daac is not None and dataset.daac != daac:
            raise ValueError(
                f"dataset {key!r} resolves to DAAC {dataset.daac!r}, "
                f"not the requested daac={daac!r}."
            )
        return dataset
