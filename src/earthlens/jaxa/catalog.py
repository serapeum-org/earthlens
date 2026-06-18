"""Dataset catalog for the JAXA backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`jaxa_data_catalog.yaml`. Each catalog row is a `Dataset` carrying a
`protocol` discriminator (`"jaxa-earth"` or `"gportal"`) plus the
protocol-specific identifier — a `jaxa.earth` STAC collection name for the
former or a G-Portal numeric dataset id for the latter. The catalog's
`by_protocol(...)` view + dataset-level validator together let the backend
route a request to the right SDK branch without re-parsing the YAML at every
fetch.

The aliases map is a separate small index resolved by :meth:`Catalog.resolve`,
giving every dataset a friendly key (``"elevation"`` → ``"aw3d30"``) on top
of its canonical key. The model is `extra="forbid"` so typos in the bundled
YAML fail loudly on first load rather than silently being ignored.

`CATALOG_PATH` points at the bundled YAML; :func:`clear_catalog_cache` empties
the `(path, mtime)` parse cache used by :meth:`Catalog.load`.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "jaxa_data_catalog.yaml"

_CATALOG_CACHE: dict[tuple[str, int], Catalog] = {}


def clear_catalog_cache() -> None:
    """Empty the module-level JAXA catalog parse cache."""
    _CATALOG_CACHE.clear()


JaxaProtocol = Literal["jaxa-earth", "gportal"]


class Dataset(BaseModel):
    """One JAXA dataset row.

    The row's `protocol` field is the discriminator the backend dispatches
    on (`_jaxa_earth.py` vs `_gportal.py`). The cross-field validator
    enforces that the right identifier is present for each protocol:

    * `protocol="jaxa-earth"` requires `collection` (the STAC collection
      name the `jaxa.earth` API consumes, e.g.
      `"JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global"`).
    * `protocol="gportal"` requires `short_name` (the G-Portal numeric
      dataset id, e.g. `"10003001"`).

    Attributes:
        key: Canonical catalog key (`"aw3d30"`) — repeated on the row so it
            travels with the object outside the catalog dict.
        protocol: One of `"jaxa-earth"` or `"gportal"`.
        collection: STAC collection name. Required when
            `protocol="jaxa-earth"`; must be `None` for `gportal`.
        short_name: G-Portal numeric dataset id (string). Required when
            `protocol="gportal"`; must be `None` for `jaxa-earth`.
        default_band: For `jaxa-earth`, the band selected when the user
            does not pass `bands=`. Ignored for `gportal` (G-Portal
            products are downloaded whole).
        aliases: Friendly keys that resolve to this row's canonical key
            (e.g. ``["elevation", "dem"]`` for ``"aw3d30"``).
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
                `gportal` row has no `short_name`, or either row sets the
                identifier belonging to the other protocol.
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
                    "`short_name` belongs to the gportal protocol — drop it."
                )
        else:
            if not self.short_name:
                raise ValueError(
                    f"dataset {self.key!r} has protocol='gportal' but no "
                    "`short_name` (the G-Portal numeric dataset id) — set it."
                )
            if self.collection is not None:
                raise ValueError(
                    f"dataset {self.key!r} has protocol='gportal'; "
                    "`collection` belongs to the jaxa-earth protocol — drop it."
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
        in tests). Either way the `available_datasets` index and the
        `aliases` map are derived from the loaded rows.
        """
        if not self.datasets:
            loaded = Catalog.load()
            self.datasets = loaded.datasets
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
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the JAXA catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `datasets:` block, or any row
                fails validation (missing identifier, alias conflict).
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = 0
        cache_key = (str(path.resolve()), mtime)
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None:
            return cached

        data = load_yaml_strict(path) or {}
        rows = data.get("datasets") or {}
        if not rows:
            raise ValueError(
                f"{path} is missing or has an empty 'datasets:' block. "
                "The JAXA catalog must list at least one dataset."
            )
        datasets: dict[str, Dataset] = {}
        for key, body in rows.items():
            try:
                datasets[key] = Dataset(key=key, **dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} dataset {key!r} failed validation:\n{exc}"
                ) from exc
        catalog = cls(datasets=datasets)
        _CATALOG_CACHE[cache_key] = catalog
        return catalog

    def get_catalog(self) -> dict[str, Dataset]:
        """Return the dataset map (satisfies the abstract contract).

        Returns:
            dict[str, Dataset]: Same object as :attr:`datasets`.
        """
        return self.datasets

    def get(self, key: str) -> Dataset:
        """Return the :class:`Dataset` for `key` (canonical or alias).

        Resolves `key` through :meth:`resolve` first, then returns the
        matching row via :meth:`get_dataset` (did-you-mean hint on miss).

        Args:
            key: A canonical key or a friendly alias.

        Returns:
            Dataset: The matching dataset row.

        Raises:
            ValueError: If `key` is neither a canonical key nor a
                registered alias.
        """
        canonical = self.resolve(key)
        return self.get_dataset(canonical)

    def resolve(self, key: str) -> str:
        """Map a friendly alias to its canonical key.

        Args:
            key: A canonical key (returns it unchanged) or a friendly
                alias.

        Returns:
            str: The canonical catalog key.

        Raises:
            ValueError: When `key` is neither, with a did-you-mean hint
                drawn from the union of canonical keys and aliases.
        """
        try:
            return self.aliases[key]
        except KeyError:
            close = difflib.get_close_matches(key, self.aliases, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not in the JAXA catalog. "
                f"Known keys: {sorted(self.datasets)}.{hint}"
            ) from None

    def by_protocol(self, protocol: JaxaProtocol) -> list[str]:
        """Return canonical keys whose `protocol` matches `protocol`.

        Args:
            protocol: Either `"jaxa-earth"` or `"gportal"`.

        Returns:
            list[str]: Sorted canonical keys filtered by protocol.
        """
        return sorted(k for k, ds in self.datasets.items() if ds.protocol == protocol)
