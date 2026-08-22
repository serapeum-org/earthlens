"""Provider dispatch table for the FDSN seismic-event backend.

FDSN is a fixed query protocol, not a curated dataset catalogue, so
this "catalog" is deliberately tiny: a six-row map from a user-facing
network name (`"USGS"`, `"EMSC"`, `"INGV"`, `"EARTHSCOPE"`, `"ISC"`,
`"GEONET"`) to the obspy `URL_MAPPINGS` key plus a little metadata.
Adding another network later (ORFEUS, GFZ, …) is a hand-edit of one
YAML row. There is no `probe` handler and no `tools/fdsn/` directory,
but the backend does ship `refresh` and `validate` handlers in
`earthlens.fdsn.cli`: `refresh` lists the data centres obspy can reach
(its `URL_MAPPINGS` registry, no network call) and diffs them against
the `fdsn_id` values curated here, so a centre obspy gains or drops
surfaces instead of going unnoticed. `audit` works too, derived from
the refresher's drift axis rather than from a handler of its own.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass. To stay consistent with the other backends (GEE / ECMWF /
CMEMS), the network rows are stored in the framework's
:attr:`~earthlens.base.AbstractCatalog.datasets` field — so the
inherited dict-like surface works (`len(cat)`, `name in cat`,
`cat[name]`, `iter(cat)`, `str(cat)`, :meth:`get_dataset`). Because
the FDSN domain term for a row is a *network* / *provider*, the same
map is mirrored onto :attr:`~earthlens.base.AbstractCatalog.providers`
and :meth:`get_provider` works too — they are aliases of the same
rows. Parsing is cached on `(path, mtime)` exactly like the other
backends (see :data:`_CATALOG_CACHE` / :func:`clear_catalog_cache`).
:data:`CATALOG_PATH` is the path to the bundled YAML and is
monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import catalog_cache_key
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "fdsn_data_catalog.yaml"

# Module-level cache of parsed catalog rows, keyed on the resolved path
# plus the YAML's `st_mtime_ns`, so editing the file invalidates the
# entry without re-parsing on every `Catalog()`. Mirrors the
# `_CATALOG_CACHE` pattern in the GEE / ECMWF / CMEMS catalog loaders.
_CATALOG_CACHE: dict[tuple[str, int], dict[str, Provider]] = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level catalog parse cache.

    Useful in tests that rewrite the catalog on disk and want to force
    a re-parse. Production callers do not need this — the cache key
    includes the file's `st_mtime_ns`, so any real edit invalidates the
    entry on its own.
    """
    _CATALOG_CACHE.clear()


def _load_catalog_data(path: Path) -> dict[str, Provider]:
    """Parse, validate, and cache the FDSN provider catalog at `path`.

    Args:
        path: Path to the catalog YAML (default :data:`CATALOG_PATH`).

    Returns:
        Mapping from network name to its :class:`Provider` row.

    Raises:
        ValueError: If the file has no `providers:` block, or a row
            fails :class:`Provider` validation.
    """
    key = catalog_cache_key(path, [path])
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    providers_yaml = data.get("providers") or {}
    if not providers_yaml:
        raise ValueError(
            f"{path} is missing or has an empty 'providers:' block. "
            "The FDSN catalog must list at least one provider."
        )
    rows: dict[str, Provider] = {}
    for name, body in providers_yaml.items():
        try:
            rows[name] = Provider(**dict(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} provider {name!r} failed validation:\n{exc}"
            ) from exc

    _CATALOG_CACHE[key] = rows
    return rows


class Provider(BaseModel):
    """One FDSN-event network's dispatch row.

    The user-facing name is the parent key in
    :attr:`Catalog.providers` (and :attr:`Catalog.datasets`) and is not
    stored on the row.

    Attributes:
        fdsn_id: obspy `URL_MAPPINGS` key (e.g. `"USGS"`) or an
            explicit base URL the `obspy.clients.fdsn.Client`
            constructor accepts. This is what actually selects the
            web service.
        title: Human-readable description used in logs and docs.
        needs_token: Whether this network's event service requires an
            access token. `False` for the public networks; the FDSN
            event endpoints are public, so this is informational and
            only consulted when a token has been supplied.
        default_min_magnitude: Network-appropriate default lower
            magnitude bound (regional networks like INGV report much
            smaller events than the global catalogs). Advisory — the
            backend's own `min_magnitude` kwarg takes precedence.
        docs_url: Link to the network's FDSN event-service
            documentation.

    Examples:
        - Build a row directly:
            ```python
            >>> from earthlens.fdsn import Provider
            >>> p = Provider(fdsn_id="USGS", title="USGS ComCat")
            >>> p.fdsn_id, p.needs_token, p.default_min_magnitude
            ('USGS', False, 4.5)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fdsn_id: str
    title: str = ""
    needs_token: bool = False
    default_min_magnitude: float = 4.5
    docs_url: str = ""


class Catalog(AbstractCatalog):
    """Provider catalog for the FDSN seismic-event backend.

    Reads the bundled `fdsn_data_catalog.yaml` (shipped as package
    data) and exposes its `providers:` block as a map of
    :class:`Provider` rows. Instantiate with no arguments
    (`Catalog()`); :func:`model_post_init` loads and validates the YAML
    (cached) in one pass.

    The rows live in the framework's :attr:`datasets` field so the
    inherited dict-like surface behaves like every other backend's
    catalog — `len(cat)`, `name in cat`, `cat[name]`, `iter(cat)` and
    :meth:`get_dataset` all work. The same rows are mirrored onto
    :attr:`providers`, so the domain-friendly :meth:`get_provider` and
    `cat.providers` work too; the two are aliases of one another.

    Attributes:
        datasets: Map from the user-facing network name to its
            :class:`Provider` dispatch row (the framework field).
        providers: Alias of :attr:`datasets` — same rows, kept for the
            FDSN "network / provider" vocabulary.

    Examples:
        - The dict-like surface works like the other backends:
            ```python
            >>> from earthlens.fdsn import Catalog
            >>> cat = Catalog()
            >>> len(cat)
            6
            >>> "USGS" in cat
            True
            >>> cat["USGS"].fdsn_id
            'USGS'

            ```
        - `datasets` and `providers` are the same rows; resolve via
          either accessor:
            ```python
            >>> from earthlens.fdsn import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.providers) == sorted(cat.datasets)
            True
            >>> cat.get_provider("USGS").fdsn_id
            'USGS'
            >>> cat.get_dataset("EMSC").fdsn_id
            'EMSC'

            ```
        - An unknown network raises with a did-you-mean hint:
            ```python
            >>> from earthlens.fdsn import Catalog
            >>> Catalog().get_provider("USG")
            Traceback (most recent call last):
                ...
            ValueError: 'USG' is not a registered provider. Known providers: ['EARTHSCOPE', 'EMSC', 'GEONET', 'INGV', 'ISC', 'USGS']. Did you mean 'USGS'?

            ```
    """

    _catalog_kind: str = "FDSN catalog"
    _entry_noun: str = "networks"

    datasets: dict[str, Provider] = Field(default_factory=dict)
    providers: dict[str, Provider] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog and keep `datasets`/`providers` in sync.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached).
        Passing either `datasets=...` or `providers=...` skips the disk
        read (used in tests); whichever was supplied is mirrored onto
        the other so both accessors stay consistent.

        Raises:
            ValueError: Propagated from :func:`_load_catalog_data` when
                the YAML is missing, empty, or has a malformed row.
        """
        if self.datasets or self.providers:
            if not self.datasets:
                self.datasets = self.providers
            if not self.providers:
                self.providers = self.datasets
        else:
            rows = _load_catalog_data(CATALOG_PATH)
            self.datasets = dict(rows)
            self.providers = self.datasets
        super().model_post_init(__context)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the FDSN provider catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `providers:` block, or a row
                fails :class:`Provider` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        rows = _load_catalog_data(catalog_path)
        return cls(datasets=dict(rows))

    def get_catalog(self) -> dict[str, Provider]:
        """Return the network map (satisfies the abstract contract).

        Returns:
            dict[str, Provider]: Same object as :attr:`datasets`.
        """
        return self.datasets
