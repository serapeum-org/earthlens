"""Authentication placeholder for the WorldPop backend.

The WorldPop open population data hub (`hub.worldpop.org`) is **open
data, attribution only** — every dataset is licensed `CC-BY-4.0` and
served over plain anonymous HTTPS from `data.worldpop.org`, so
`WorldPop` performs no login:
`earthlens.worldpop.backend.WorldPop._initialize` reads no credentials
and the backend works with no key, env var, or config file.

`WorldPopAuth` exists so the package mirrors the layout of the
authenticated backends (each has an `auth.py` with an `AbstractAuth`
subclass). It is a no-op: `configure()` does nothing and
`is_authenticated()` is always `True`. It carries the required
**attribution string** so the backend can stamp it into output
metadata / a sidecar.
"""

from __future__ import annotations

from earthlens.base.auth import AbstractAuth
from pydantic import BaseModel

#: The attribution every WorldPop product (CC-BY-4.0) must carry downstream.
WORLDPOP_ATTRIBUTION: str = (
    "Source: WorldPop (www.worldpop.org), School of Geography and "
    "Environmental Science, University of Southampton. Licensed CC-BY-4.0."
)

#: The canonical WorldPop licence URL (CC-BY-4.0).
WORLDPOP_LICENCE_URL: str = "https://hub.worldpop.org/data/licence.txt"


class WorldPopCredentials(BaseModel, frozen=True):
    """Empty credentials value object for the open WorldPop backend.

    WorldPop needs no secrets; this exists only to satisfy the
    `earthlens.base.auth.AbstractAuth` generic contract that every
    backend's auth class binds a `pydantic.BaseModel` credentials
    type. It carries no fields.

    Examples:
        - Construct the empty credentials:
            ```python
            >>> from earthlens.worldpop import WorldPopCredentials
            >>> WorldPopCredentials()
            WorldPopCredentials()

            ```
    """


class WorldPopAuth(AbstractAuth[WorldPopCredentials]):
    """No-op auth for the open, attribution-only WorldPop data.

    Kept for conformance with the `AbstractAuth` shape the other
    backends follow; WorldPop reads no credentials. `configure()`
    flips an internal flag and `is_authenticated()` is always
    `True`, so the context-manager form (`with WorldPopAuth() as
    auth: ...`) works like any other backend's.

    Examples:
        - It is always authenticated, with nothing to configure:
            ```python
            >>> from earthlens.worldpop import WorldPopAuth
            >>> auth = WorldPopAuth()
            >>> auth.is_authenticated()
            True
            >>> auth.configure()  # idempotent no-op
            >>> auth.is_authenticated()
            True

            ```
    """

    def __init__(self, credentials: WorldPopCredentials | None = None) -> None:
        """Store the (empty) credentials; default to a fresh `WorldPopCredentials`.

        Args:
            credentials: Optional empty credentials object. Defaults to a
                fresh `WorldPopCredentials()` since WorldPop needs no
                secrets.
        """
        super().__init__(
            credentials if credentials is not None else WorldPopCredentials()
        )
        self._configured = False

    def configure(self) -> None:
        """No-op setup — WorldPop is open + attribution-only (nothing to do)."""
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` — open data needs no credentials."""
        return True
