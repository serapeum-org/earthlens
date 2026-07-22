"""Authentication placeholder for the GHSL backend.

The JRC Global Human Settlement Layer is **open data, attribution
only** — "reuse is authorised provided the source is acknowledged"
(CC-BY-style). Every artefact is served over plain anonymous HTTPS
from the JRC JEODPP file tree, so `GHSL` performs no login:
:meth:`earthlens.ghsl.backend.GHSL._initialize` reads no credentials
and the backend works with no key, env var, or config file.

`GhslAuth` exists so the package mirrors the layout of the
authenticated backends (each has an `auth.py` with an
`AbstractAuth` subclass) and so a future credentialled JRC endpoint
has an obvious home. It is a no-op: `configure()` does nothing and
`is_authenticated()` is always `True`. It carries the required
**attribution string** so the backend can stamp it into output
metadata / a sidecar.
"""

from __future__ import annotations

from earthlens.base.auth import AbstractAuth
from pydantic import BaseModel

#: The attribution the GHSL licence requires downstream products to carry.
GHSL_ATTRIBUTION: str = (
    "Source: European Commission, Joint Research Centre (JRC) — "
    "Global Human Settlement Layer (GHSL). Data available at "
    "https://ghsl.jrc.ec.europa.eu/."
)


class GhslCredentials(BaseModel, frozen=True):
    """Empty credentials value object for the open GHSL backend.

    GHSL needs no secrets; this exists only to satisfy the
    :class:`~earthlens.base.auth.AbstractAuth` generic contract that
    every backend's auth class binds a `pydantic.BaseModel`
    credentials type. It carries no fields.

    Examples:
        - Construct the empty credentials:
            ```python
            >>> from earthlens.ghsl import GhslCredentials
            >>> GhslCredentials()
            GhslCredentials()

            ```
    """


class GhslAuth(AbstractAuth[GhslCredentials]):
    """No-op auth for the open, attribution-only GHSL data.

    Kept for conformance with the `AbstractAuth` shape the other
    backends follow; GHSL reads no credentials. `configure()` flips
    an internal flag and `is_authenticated()` is always `True`, so
    the context-manager form (`with GhslAuth() as auth: ...`) works
    like any other backend's.

    Examples:
        - It is always authenticated, with nothing to configure:
            ```python
            >>> from earthlens.ghsl import GhslAuth
            >>> auth = GhslAuth()
            >>> auth.is_authenticated()
            True
            >>> auth.configure()  # idempotent no-op
            >>> auth.is_authenticated()
            True

            ```
    """

    def __init__(self, credentials: GhslCredentials | None = None) -> None:
        """Store the (empty) credentials; default to a fresh `GhslCredentials`.

        Args:
            credentials: Optional empty credentials object. Defaults to
                a fresh `GhslCredentials()` since GHSL needs no secrets.
        """
        super().__init__(credentials if credentials is not None else GhslCredentials())
        self._configured = False

    def configure(self) -> None:
        """No-op setup — GHSL is open + attribution-only (nothing to do)."""
        self._configured = True

    def is_authenticated(self) -> bool:
        """Return `True` — open data needs no credentials."""
        return True
