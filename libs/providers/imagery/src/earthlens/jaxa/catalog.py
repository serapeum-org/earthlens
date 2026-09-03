"""Dataset catalog for the JAXA backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled JAXA
catalog. The catalog ships as a directory of per-mission YAML files at
`src/earthlens/jaxa/catalog/` (`jaxa-earth.yaml`, `sgli.yaml`,
`amsr.yaml`, `alos-palsar.yaml`, `earthcare.yaml`,
`precipitation.yaml`, …), plus a single `_index.yaml` carrying the merged
`available_datasets:` list — the same sharded layout used by the
`gee` and `ecmwf` siblings.

Each catalog row is a `Dataset` carrying a `protocol` discriminator
(`"jaxa-earth"`, `"gportal"`, or `"ptree"`) plus the protocol-specific
identifier — a `jaxa.earth` STAC collection name for `"jaxa-earth"`, a
G-Portal numeric dataset id for `"gportal"`, or a P-Tree product token
(e.g. `"himawari-ahi-fldk"`) for `"ptree"`. The catalog's
`by_protocol(...)` view + dataset-level validator together let the
backend route a request to the right branch without re-parsing the
YAML at every fetch.

The aliases map is a separate small index resolved by :meth:`Catalog.resolve`,
giving every dataset a friendly key (``"elevation"`` → ``"aw3d30"``) on top
of its canonical key. The model is `extra="forbid"` so typos in the bundled
YAML fail loudly on first load rather than silently being ignored.

`CATALOG_PATH` points at the bundled catalog directory (or, for tests
that monkey-patch it, a single `*.yaml` file); :func:`clear_catalog_cache`
empties the `(path, mtime)` parse cache used by :meth:`Catalog.load`.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "catalog"

#: Keyed on the resolved path + every contributing file's stamp so editing any
#: per-mission YAML invalidates the cache without re-walking the whole tree.
#: Holds the construction kwargs rather than a built :class:`Catalog`, so each
#: `load()` returns a fresh instance that a caller can mutate in isolation.
_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


#: G-Portal dataset ids are 7- to 9-digit numeric strings (e.g. `11001002`).
_GPORTAL_ID_RE = re.compile(r"^\d{7,9}$")

#: jaxa.earth STAC collection names start with the data publisher prefix.
_JAXA_EARTH_PREFIX_RE = re.compile(r"^(JAXA|NASA|Copernicus)\.")


def clear_catalog_cache() -> None:
    """Empty the module-level JAXA catalog parse cache.

    Examples:
        - Calling it is a no-op when the cache is already empty, and
          subsequent `Catalog()` calls re-read the YAML from disk:
            ```python
            >>> from earthlens.jaxa.catalog import clear_catalog_cache, Catalog
            >>> clear_catalog_cache()
            >>> "aw3d30" in Catalog()
            True

            ```
    """
    _CATALOG_CACHE.clear()


#: Re-exported so a reader of `catalog.py`'s `Dataset` model can import
#: the discriminator type from the same module. The single source of
#: truth lives in :mod:`earthlens.jaxa.auth` so a new credentialed
#: protocol only needs to grow the Literal in one place.
from earthlens.jaxa.auth import JaxaProtocol  # noqa: E402  (re-export)


class Dataset(BaseModel):
    """One JAXA dataset row.

    The row's `protocol` field is the discriminator the backend dispatches
    on (`_jaxa_earth.py` vs `_gportal.py` vs `_ptree.py`). The cross-field
    validator enforces that the right identifier is present for each
    protocol:

    * `protocol="jaxa-earth"` requires `collection` (the STAC collection
      name the `jaxa.earth` API consumes, e.g.
      `"JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global"`).
    * `protocol="gportal"` requires `short_name` (the G-Portal numeric
      dataset id, e.g. `"10003001"`).
    * `protocol="ptree"` requires `short_name` (the P-Tree product
      token, e.g. `"himawari-ahi-fldk"`).

    Attributes:
        key: Canonical catalog key (`"aw3d30"`) — repeated on the row so it
            travels with the object outside the catalog dict.
        protocol: One of `"jaxa-earth"`, `"gportal"`, or `"ptree"`.
        collection: STAC collection name. Required when
            `protocol="jaxa-earth"`; must be `None` for `gportal` and
            `ptree`.
        short_name: Protocol-specific product identifier — a G-Portal
            numeric dataset id (e.g. `"10003001"`) for `gportal`, or a
            P-Tree product token (e.g. `"himawari-ahi-fldk"`) for
            `ptree`. Required for both credentialed protocols; must be
            `None` for `jaxa-earth`.
        default_band: The band selected when the user does not pass
            `bands=`. Used by `jaxa-earth` (picks the GeoTIFF band to
            write) and `ptree` (picks which Himawari AHI band to
            download from `B01`..`B16`). Ignored for `gportal`
            (G-Portal products are downloaded whole).
        aliases: Friendly keys that resolve to this row's canonical key
            (e.g. `["elevation", "dem"]` for `"aw3d30"`).
        description: Human-readable summary used in docs.

    Examples:
        - A `jaxa-earth` row needs `collection`:
            ```python
            >>> from earthlens.jaxa.catalog import Dataset
            >>> ds = Dataset(
            ...     key="aw3d30",
            ...     protocol="jaxa-earth",
            ...     collection="JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global",
            ...     default_band="DSM",
            ... )
            >>> ds.protocol
            'jaxa-earth'

            ```
        - A `gportal` row needs `short_name`:
            ```python
            >>> from earthlens.jaxa.catalog import Dataset
            >>> ds = Dataset(key="sgli-l380", protocol="gportal", short_name="10003001")
            >>> ds.short_name
            '10003001'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    protocol: JaxaProtocol
    collection: str | None = None
    short_name: str | None = None
    default_band: str | None = None
    aliases: list[str] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _check_protocol_identifier(self) -> Dataset:
        """Enforce one identifier per protocol.

        Raises:
            ValueError: If a `jaxa-earth` row has no `collection`, or a
                credentialed (`gportal` / `ptree`) row has no
                `short_name`, or the row sets the identifier belonging
                to the other side (a `jaxa-earth` row with a
                `short_name`, or a credentialed row with a
                `collection`).
        """
        if self.protocol == "jaxa-earth":
            if not self.collection:
                raise ValueError(
                    f"dataset {self.key!r} has protocol='jaxa-earth' but no "
                    "`collection` (the STAC collection name) — set it."
                )
            if self.short_name is not None:
                raise ValueError(
                    f"dataset {self.key!r} has protocol='jaxa-earth'; "
                    "`short_name` belongs to a credentialed protocol "
                    "(gportal/ptree) — drop it."
                )
        else:
            if not self.short_name:
                raise ValueError(
                    f"dataset {self.key!r} has protocol={self.protocol!r} "
                    "but no `short_name` (the protocol's product identifier) "
                    "— set it."
                )
            if self.collection is not None:
                raise ValueError(
                    f"dataset {self.key!r} has protocol={self.protocol!r}; "
                    "`collection` belongs to the jaxa-earth protocol "
                    "— drop it."
                )
        return self


class Catalog(AbstractCatalog):
    """Reader for the bundled JAXA dataset catalog.

    Subclasses :class:`AbstractCatalog` so the dict-like surface
    (`cat["aw3d30"]`, `"aw3d30" in cat`, `len(cat)`) comes for free. The
    `resolve` method maps a friendly alias (`"elevation"`) to its canonical
    key (`"aw3d30"`); :meth:`by_protocol` lists the keys filtered by
    protocol; :meth:`get` is a thin alias for `get_dataset`.

    Attributes:
        datasets: Map from canonical key to its :class:`Dataset` row.
        available_datasets: Sorted canonical keys.

    Examples:
        - List the canonical keys, resolve a friendly alias:
            ```python
            >>> from earthlens.jaxa import Catalog
            >>> cat = Catalog()
            >>> "aw3d30" in cat
            True
            >>> cat.resolve("elevation")
            'aw3d30'

            ```
    """

    _catalog_kind: str = "JAXA catalog"

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no datasets were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read (used
        in tests). Either way the `aliases` map is derived from the loaded
        rows. `available_datasets` is populated from the YAML's optional
        `available_datasets:` block (the refresh CLI's `--write` target —
        an informational index of every live SDK id across all three
        protocols) or, when that block is absent, defaults to the sorted
        curated keys so the dict-like surface (`len(cat)` etc.) still
        works.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
            if loaded.available_datasets and not self.available_datasets:
                self.available_datasets = loaded.available_datasets
        # Build the alias index from the rows' `aliases` lists. Conflicts
        # (same alias on two rows) raise; canonical keys also resolve to
        # themselves so `resolve()` is total over both.
        aliases: dict[str, str] = {}
        for key, ds in self.datasets.items():
            aliases[key] = key
            for alias in ds.aliases:
                if alias in aliases and aliases[alias] != key:
                    raise ValueError(
                        f"alias {alias!r} is claimed by both {aliases[alias]!r} "
                        f"and {key!r} — pick one."
                    )
                aliases[alias] = key
        self.aliases = aliases
        if not self.available_datasets:
            self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the JAXA catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog directory (the sharded
                default) or a single YAML file (tests / legacy). Defaults
                to the module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog` merged across every
            contributing file.

        Raises:
            ValueError: If `catalog_path` does not exist, the path has no
                `datasets:` rows across any file, or any row fails
                validation (missing identifier, alias conflict), or a key
                is declared in two shards. A missing path only fires when a
                caller passes an explicit one — the bundled catalog ships
                with the wheel.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="jaxa")
        return cls(**payload)

    def get(self, key: str) -> Dataset:
        """Return the :class:`Dataset` for `key` (canonical, alias, or raw id).

        Resolves `key` through :meth:`resolve` first, then returns the
        matching curated row when one exists. **Raw G-Portal numeric ids**
        (e.g. ``"11001002"``) and **raw jaxa.earth STAC collection names**
        (e.g. ``"JAXA.G-Portal_GCOM-W.AMSR2_standard.L3-SSW.daytime.v4_global_yearly"``)
        pass through unmapped: a passthrough :class:`Dataset` row is
        synthesized on the fly with the right protocol so the backend
        dispatches correctly — the full 799-entry G-Portal universe + the
        ~119-entry JAXA Earth STAC catalogue are usable without bloating
        the bundled YAML with hand-curated rows.

        Args:
            key: A canonical key, friendly alias, raw G-Portal id, or raw
                jaxa.earth STAC collection name.

        Returns:
            Dataset: The matching curated row, or a synthesized passthrough
            row when `key` is a raw id recognised by the live SDK.

        Raises:
            ValueError: If `key` is neither curated nor a syntactically
                valid raw id.

        Examples:
            - A friendly alias resolves to the same row as the canonical key:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> cat = Catalog()
                >>> cat.get("elevation").collection
                'JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global'
                >>> cat.get("aw3d30").default_band
                'DSM'

                ```
            - A raw G-Portal id passes through as a synthesized gportal row:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> row = Catalog().get("11001002")
                >>> row.protocol, row.short_name
                ('gportal', '11001002')

                ```
            - An unknown key raises ValueError:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> try:
                ...     Catalog().get("definitely-not-a-key")
                ... except ValueError as exc:
                ...     "JAXA catalog" in str(exc)
                True

                ```
        """
        canonical = self.resolve(key)
        if canonical in self.datasets:
            return self.datasets[canonical]
        # Passthrough: synthesize a Dataset row for raw ids that match
        # either protocol's id shape (resolve() has already accepted them).
        if _GPORTAL_ID_RE.match(canonical):
            return Dataset(key=canonical, protocol="gportal", short_name=canonical)
        return Dataset(key=canonical, protocol="jaxa-earth", collection=canonical)

    def resolve(self, key: str) -> str:
        """Map a friendly alias / raw id to its canonical key.

        Args:
            key: A canonical key, friendly alias, raw G-Portal numeric
                id (e.g. ``"11001002"``), or raw jaxa.earth STAC
                collection name (e.g. ``"JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global"``).

        Returns:
            str: The canonical catalog key. Raw ids pass through unchanged.

        Raises:
            ValueError: When `key` is neither curated nor a syntactically
                valid raw id, with a did-you-mean hint drawn from the
                union of canonical keys and aliases.

        Examples:
            - A canonical key resolves to itself; an alias resolves to its row:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> cat = Catalog()
                >>> cat.resolve("aw3d30")
                'aw3d30'
                >>> cat.resolve("elevation")
                'aw3d30'

                ```
            - Raw G-Portal numeric ids pass through unmapped:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> Catalog().resolve("11001002")
                '11001002'

                ```
        """
        if key in self.aliases:
            return self.aliases[key]
        # Raw G-Portal id (7-9 digits) or raw jaxa.earth STAC collection name
        # passes through unmapped, mirroring `usgs_water.Catalog.resolve` for
        # raw 5-digit NWIS codes. The available_datasets: index lets a
        # curator verify the id is real upstream.
        if _GPORTAL_ID_RE.match(key) or _JAXA_EARTH_PREFIX_RE.match(key):
            return key
        close = difflib.get_close_matches(key, self.aliases, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"{key!r} is not in the JAXA catalog. "
            f"Known keys: {sorted(self.datasets)}.{hint}"
        )

    def by_protocol(self, protocol: JaxaProtocol) -> list[str]:
        """Return canonical keys whose `protocol` matches `protocol`.

        Args:
            protocol: One of `"jaxa-earth"`, `"gportal"`, or `"ptree"`.

        Returns:
            list[str]: Sorted canonical keys filtered by protocol.

        Examples:
            - The bundled catalog ships all three protocols; `aw3d30` is
              jaxa-earth, `sgli-l3-nwlr` is gportal, and
              `himawari-ahi-fldk` is ptree:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> cat = Catalog()
                >>> "aw3d30" in cat.by_protocol("jaxa-earth")
                True
                >>> "sgli-l3-nwlr" in cat.by_protocol("gportal")
                True
                >>> "himawari-ahi-fldk" in cat.by_protocol("ptree")
                True

                ```
        """
        return sorted(k for k, ds in self.datasets.items() if ds.protocol == protocol)

    def __contains__(self, name: object) -> bool:
        """`name in cat` — accept both canonical keys and friendly aliases.

        Overrides :meth:`AbstractCatalog.__contains__`, which only checks
        `self.datasets`, so the alias surface (`"elevation" in cat`)
        matches what `cat.get("elevation")` already accepts.

        Examples:
            - Aliases and canonical keys both resolve:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> cat = Catalog()
                >>> "aw3d30" in cat
                True
                >>> "elevation" in cat
                True
                >>> "not-a-real-key" in cat
                False

                ```
        """
        return name in self.aliases

    def __getitem__(self, name: str) -> Dataset:
        """`cat[name]` — accept both canonical keys and friendly aliases.

        Overrides :meth:`AbstractCatalog.__getitem__` so dict-style
        lookup matches the same alias surface as :meth:`get`. Raises
        :class:`KeyError` on miss (the dict-style contract; callers that
        want a did-you-mean hint use :meth:`get` instead).

        Examples:
            - Lookup by alias returns the canonical row:
                ```python
                >>> from earthlens.jaxa import Catalog
                >>> cat = Catalog()
                >>> cat["elevation"].key
                'aw3d30'

                ```
        """
        try:
            return self.get(name)
        except ValueError as exc:
            raise KeyError(name) from exc


def _merge_shards(
    files: list[Path],
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Path]]:
    """Union the `available_datasets:` and `datasets:` blocks across shards.

    Args:
        files: The contributing YAML files, in sorted order.

    Returns:
        Tuple of the merged available-id list, the raw row bodies keyed by
            dataset key, and the file each row came from (for error messages).

    Raises:
        ValueError: If the same dataset key is declared in two files.
    """
    merged_rows: dict[str, dict[str, Any]] = {}
    row_origin: dict[str, Path] = {}
    available: list[str] = []
    for file_path in files:
        data = load_yaml_strict(file_path) or {}
        available.extend(str(ident) for ident in data.get("available_datasets") or [])
        for key, body in (data.get("datasets") or {}).items():
            if key in merged_rows:
                raise ValueError(
                    f"dataset {key!r} declared in two catalog files: "
                    f"{row_origin[key]} and {file_path}"
                )
            merged_rows[key] = dict(body or {})
            row_origin[key] = file_path
    return available, merged_rows, row_origin


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Merge and validate the JAXA catalog shards into construction kwargs.

    Args:
        files: The contributing YAML files, in sorted order.

    Returns:
        dict[str, Any]: The validated `datasets` / `available_datasets`
            kwargs. The payload is cached rather than a built Catalog, so
            `load()` hands back a fresh instance each call.

    Raises:
        ValueError: If no file carries `datasets:` rows, a key is declared in
            two shards, or a row fails validation.
    """
    # Name the directory when the catalog is sharded, the file when it is not,
    # so the "empty datasets:" error points at what the caller passed.
    path = files[0].parent if len(files) > 1 else files[0]
    available, merged_rows, row_origin = _merge_shards(files)
    if not merged_rows:
        raise ValueError(
            f"{path} is missing or has an empty 'datasets:' block. "
            "The JAXA catalog must list at least one dataset."
        )
    datasets: dict[str, Dataset] = {}
    for key, body in merged_rows.items():
        try:
            datasets[key] = Dataset(key=key, **body)
        except ValidationError as exc:
            raise ValueError(
                f"{row_origin[key]} dataset {key!r} failed validation:\n{exc}"
            ) from exc
    return {"datasets": datasets, "available_datasets": available}
