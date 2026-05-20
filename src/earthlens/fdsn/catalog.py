"""Provider dispatch table for the FDSN seismic-event backend.

FDSN is a fixed query protocol, not a curated dataset catalogue, so
this "catalog" is deliberately tiny: a four-row map from a user-facing
network name (`"USGS"`, `"EMSC"`, `"INGV"`, `"EARTHSCOPE"`) to the
obspy `URL_MAPPINGS` key plus a little metadata. There is no
`refresh` / `probe` / `audit` tooling and no `tools/fdsn/` directory —
adding a network later (ISC, ORFEUS, GEONET, …) is a hand-edit of one
YAML row.

:class:`Catalog` is a thin :class:`earthlens.base.AbstractCatalog`
subclass that loads the bundled `fdsn_data_catalog.yaml` and exposes
each row as a :class:`Provider`. Look a provider up with
:meth:`AbstractCatalog.get_provider`, which raises with a
did-you-mean hint on an unknown name. :data:`CATALOG_PATH` is the
path to the bundled YAML and is monkey-patchable in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "fdsn_data_catalog.yaml"


class Provider(BaseModel):
    """One FDSN-event network's dispatch row.

    The user-facing name is the parent key in
    :attr:`Catalog.providers` and is not stored on the row.

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
    (`Catalog()`); :func:`model_post_init` loads and validates the
    YAML in one pass. Resolve a provider with
    :meth:`AbstractCatalog.get_provider`.

    Attributes:
        providers: Map from the user-facing network name to its
            :class:`Provider` dispatch row.

    Examples:
        - List networks and resolve one:
            ```python
            >>> from earthlens.fdsn import Catalog
            >>> cat = Catalog()
            >>> sorted(cat.providers)
            ['EARTHSCOPE', 'EMSC', 'GEONET', 'INGV', 'ISC', 'USGS']
            >>> cat.get_provider("USGS").fdsn_id
            'USGS'

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

    providers: dict[str, Provider] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no providers were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH`; passing
        `providers=...` skips the disk read (used in tests).

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is
                missing, empty, or has a malformed provider row.
        """
        if self.providers:
            return
        loaded = Catalog.load()
        self.providers = loaded.providers

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read the FDSN provider catalog from disk.

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the
                module-level :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If the file has no `providers:` block, or a
                row fails :class:`Provider` validation.
        """
        catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH
        data = load_yaml_strict(catalog_path) or {}
        providers_yaml = data.get("providers") or {}
        if not providers_yaml:
            raise ValueError(
                f"{catalog_path} is missing or has an empty 'providers:' "
                "block. The FDSN catalog must list at least one provider."
            )
        providers: dict[str, Provider] = {}
        for name, body in providers_yaml.items():
            try:
                providers[name] = Provider(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{catalog_path} provider {name!r} failed validation:\n{exc}"
                ) from exc
        return cls(providers=providers)

    def get_catalog(self) -> dict[str, Provider]:
        """Return the provider map (satisfies the abstract contract).

        Returns:
            dict[str, Provider]: Same object as :attr:`providers`.
        """
        return self.providers
