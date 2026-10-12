"""A cohesive adapter over the optional pyramids-eo EEDAI reader.

`_eedai.py` is three free functions — `import_earthengine_reader`,
`eedai_available`, `credentials_for` — and the backend carried the resolved
credential separately in `self._eedai_credential`. Every reader call therefore
re-imported the module and threaded the credential in by hand.

:class:`EedaiReader` binds the module and the credential into one object, so a
caller asks it for a read rather than assembling one. The guarded import stays
guarded: the optional dependency is still resolved lazily and still surfaces as
one clear `ImportError` naming the extra to install.

**The process-global hazard this owns.** `_eedai.py`'s own module docstring
warns that pyramids-eo configures GDAL's `EEDAI:` credentials *process-globally*
for the duration of a read, so two `GEE` instances using different service
accounts concurrently in one process race over which identity is in effect — and
the loser reads as the wrong account, silently. Sequential reads are unaffected.
This adapter serialises reads behind a module-level lock and refuses a read that
would change the in-effect identity while another is in flight, so the race
becomes an error rather than wrong pixels.

**What is wired, as of this commit.** The credential factory is used — the
backend reads `.credential` from here. The read methods and the identity guard
are not: both real read sites call the lazily-imported module directly, so
`estimate_cost` / `from_earthengine` / `collection_from_earthengine` have no
production caller yet, and the guard therefore protects nothing in practice.
Note also that `_IN_EFFECT` is set and cleared inside `_READ_LOCK`, so no other
thread can ever observe a foreign identity through it — the lock already
serialises the whole read. The guard fires only on same-thread re-entrancy. The
save/restore is correct, and moving the read sites onto this adapter is what
makes the protection real; until then this is an adapter with one live method.

"""

from __future__ import annotations

import threading
from types import ModuleType
from typing import Any

from loguru import logger

from earthlens.gee._eedai import import_earthengine_reader
from earthlens.gee.auth import AuthenticationError

__all__ = ["EedaiReader"]

#: Serialises reads, because pyramids-eo configures GDAL's EEDAI credentials
#: process-globally for the duration of one. Held only across a reader call, not
#: across the whole download.
_READ_LOCK = threading.RLock()

#: Which credential is currently configured process-wide, so a concurrent read
#: under a different identity can be refused rather than silently mis-attributed.
_IN_EFFECT: dict[str, Any] = {"credential": None}


class EedaiReader:
    """The pyramids-eo reader plus the credential it reads with.

    Attributes:
        credential: The `EarthEngineCredentials` the reader authenticates with,
            or `None` when it falls back to Application Default Credentials.

    Examples:
        - Constructing one resolves nothing, so it is cheap and needs no extra
          installed; it reports which identity it will read as:
            ```python
            >>> repr(EedaiReader())
            'EedaiReader(adc)'

            ```
        - A bound credential never reaches the text, so an adapter is safe to
          log:
            ```python
            >>> repr(EedaiReader(credential="a-secret-looking-object"))
            'EedaiReader(pinned)'

            ```
    """

    def __init__(self, credential: Any = None) -> None:
        """Bind a resolved credential to the reader module.

        Args:
            credential: The pyramids-eo credential, or `None` for ADC.
        """
        self.credential = credential
        self._module: ModuleType | None = None

    @classmethod
    def from_service_key(cls, service_key: str | None) -> EedaiReader:
        """Build a reader for a service-account key path.

        `EarthEngineCredentials` writes inline key material to a private temp
        file whose removal is left to the garbage collector, so rebuilding it per
        bucket would scatter transient key files across a multi-band, multi-date
        download. Callers should build one adapter per request and reuse it.

        The Earth Engine `project` is deliberately not forwarded: the reader
        authenticates GDAL's `EEDAI:` driver with the key alone.

        Args:
            service_key: Path to the service-account key, or `None` to fall back
                to Application Default Credentials.

        Returns:
            The adapter.

        Raises:
            AuthenticationError: If the credential cannot be built, so the
                failure matches this backend's error contract rather than
                surfacing pyramids-eo's own exception type.
            ImportError: If `pyramids-eo` (the `[eedai]` extra) is missing.
        """
        if service_key is None:
            logger.warning(
                "No Earth Engine service key resolved for the EEDAI read; falling "
                "back to Application Default Credentials, which may authenticate "
                "as a different identity than the Earth Engine half of this "
                "request. Pass service_key= (or set GEE_SERVICE_KEY) to pin it."
            )
        # Read `credentials_for` through the backend module rather than
        # importing it here: that attribute is the single seam through which the
        # optional reader's credential helper is resolved, and a direct import
        # would bypass it. Same reason `eedai_available` is read below.
        from earthlens.gee import backend as backend_module

        try:
            credential = backend_module.credentials_for(service_key)
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"could not build Earth Engine credentials for the EEDAI read: {exc}"
            ) from exc
        return cls(credential)

    @staticmethod
    def available() -> bool:
        """Return whether the optional reader is importable.

        Returns:
            `True` when the `[eedai]` extra is installed.
        """
        from earthlens.gee import backend as backend_module

        return backend_module.eedai_available()

    @property
    def module(self) -> ModuleType:
        """Return the reader module, imported once per adapter.

        Returns:
            The `pyramids_eo.earthengine` module.

        Raises:
            ImportError: If `pyramids-eo` is not installed; the message names
                the extra to install.
        """
        if self._module is None:
            self._module = import_earthengine_reader()
        return self._module

    @property
    def window_type(self) -> Any:  # noqa: N802 - mirrors the upstream class name
        """Return the reader's `Window` class, for building a read spec.

        Returns:
            The `pyramids_eo.earthengine.Window` class itself, not an
            instance — call it to build a read window.

        Raises:
            ImportError: If `pyramids-eo` is not installed; the message names
                the extra to install.
        """
        return self.module.Window

    def _guard_identity(self) -> None:
        """Refuse a read that would change the process-wide identity mid-flight.

        pyramids-eo configures GDAL's EEDAI credentials process-globally, so a
        second read under a different credential while one is in flight would
        silently read as whichever identity won. Sequential reads re-configure
        freely; only a genuine overlap under a *different* credential is an
        error.

        Raises:
            AuthenticationError: If another credential is currently in effect.
        """
        in_effect = _IN_EFFECT["credential"]
        if in_effect is not None and in_effect is not self.credential:
            raise AuthenticationError(
                "another EEDAI read is in flight under a different service "
                "account. pyramids-eo configures GDAL's EEDAI credentials "
                "process-globally, so overlapping reads under two identities "
                "would read as whichever one won. Run them sequentially, or in "
                "separate processes."
            )

    def _read(self, entry_point: str, asset_id: str, **kwargs: Any) -> Any:
        """Call one reader entry point with the bound credential, serialised.

        Args:
            entry_point: The reader function name to call.
            asset_id: The Earth Engine asset id to read.
            **kwargs: The entry point's own arguments.

        Returns:
            Whatever the reader returned.

        Raises:
            AuthenticationError: If a concurrent read holds a different
                identity.
        """
        with _READ_LOCK:
            self._guard_identity()
            previous = _IN_EFFECT["credential"]
            _IN_EFFECT["credential"] = self.credential
            try:
                return getattr(self.module, entry_point)(
                    asset_id, credentials=self.credential, **kwargs
                )
            finally:
                _IN_EFFECT["credential"] = previous

    def estimate_cost(self, asset_id: str, **kwargs: Any) -> Any:
        """Ask the catalog what a collection read would cost.

        Args:
            asset_id: The Earth Engine collection id.
            **kwargs: The discovery window, AOI, CRS and any property filter.

        Returns:
            The reader's `ReadCost`, whose `scene_count` is what the sizing
            verdict is built from.

        Raises:
            ImportError: If `pyramids-eo` is not installed.
            AuthenticationError: If another read is in flight under a different
                service account.
        """
        return self._read("estimate_earthengine_cost", asset_id, **kwargs)

    def from_earthengine(self, asset_id: str, **kwargs: Any) -> Any:
        """Materialise pixels from one asset, or composite a collection.

        Args:
            asset_id: The Earth Engine asset id.
            **kwargs: The read specification — `window`, `bands`, `path`,
                `tile_size`, `geometry`, and the compositing arguments for a
                collection.

        Returns:
            The pyramids `Dataset` the reader built.

        Raises:
            ImportError: If `pyramids-eo` is not installed.
            AuthenticationError: If another read is in flight under a different
                service account.
        """
        return self._read("from_earthengine", asset_id, **kwargs)

    def collection_from_earthengine(self, asset_id: str, **kwargs: Any) -> Any:
        """Materialise a collection read as a `DatasetCollection`.

        Args:
            asset_id: The Earth Engine collection id.
            **kwargs: The read specification.

        Returns:
            The pyramids `DatasetCollection` the reader built.

        Raises:
            ImportError: If `pyramids-eo` is not installed.
            AuthenticationError: If another read is in flight under a different
                service account.
        """
        return self._read("collection_from_earthengine", asset_id, **kwargs)

    def __repr__(self) -> str:
        """Say whether a credential is bound, without printing any of it."""
        bound = "pinned" if self.credential is not None else "adc"
        return f"EedaiReader({bound})"
