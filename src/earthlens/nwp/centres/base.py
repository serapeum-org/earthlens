"""Centre-dispatch base for the NWP backend.

Each numerical-weather-prediction *centre* (NOAA NODD, ECMWF Open
Data, DWD Open Data, …) has its own download protocol — Herbie's
`.idx` byte-range subsetting, `ecmwf-opendata`'s `Client.retrieve`,
or a plain per-variable HTTPS `.bz2` fetch. The `NWP` backend owns the
provider-agnostic half (the cycle-grid walk, the GRIB2→cropped-COG
pipeline); the per-centre half — "given a model, cycle, step, the
requested params, and a mirror, put a GRIB2 file on disk" — lives
behind the :class:`_NWPCentre` interface implemented by the sibling
`centres/*.py` modules.

:func:`resolve_centre` maps a model's catalog `backend:` value to the
concrete centre class, importing it lazily so the optional SDK for a
centre you do not use never has to be installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel

#: Maps a catalog `backend:` value to the `(module, class)` that
#: implements it. Imported lazily by :func:`resolve_centre` so the
#: per-centre SDKs (`herbie`, `ecmwf-opendata`) load only on use.
CENTRE_REGISTRY: dict[str, tuple[str, str]] = {
    "herbie": ("earthlens.nwp.centres.noaa", "NOAACentre"),
    "ecmwf-opendata": ("earthlens.nwp.centres.ecmwf", "ECMWFCentre"),
    "direct-https": ("earthlens.nwp.centres.dwd", "DWDCentre"),
    "direct-boto3": ("earthlens.nwp.centres.meteofrance", "MeteoFranceCentre"),
    "meteofrance-api": (
        "earthlens.nwp.centres.meteofrance_api",
        "MeteoFranceAPICentre",
    ),
    "eccc-msc": ("earthlens.nwp.centres.eccc", "ECCCCentre"),
}


class _NWPCentre(ABC):
    """Per-centre GRIB2 fetcher.

    A centre instance is constructed with the output directory and
    knows how to put the variable-subset GRIB2 for one
    `(model, cycle, step)` on disk. The provider-agnostic crop /
    COG-write pipeline runs on the returned path back in the backend.

    Attributes:
        save_dir: Directory raw GRIB2 files are written to.
        show_progress: Whether the centre should show per-download
            progress. The backend sets it from `download(progress_bar=)`;
            only centres whose SDK exposes a progress/verbose switch
            (Herbie) honour it.
        bbox: The request bounding box `(west, south, east, north)` in
            degrees, set by the backend before fetch. Only centres that
            can subset server-side (the Météo-France WCS API) use it;
            the others download the full field and let the backend crop.
    """

    def __init__(self, save_dir: Path | str):
        """Bind the centre to an output directory.

        Args:
            save_dir: Directory raw GRIB2 downloads are written to.
        """
        self.save_dir = Path(save_dir)
        self.show_progress = True
        self.bbox: tuple[float, float, float, float] | None = None

    @abstractmethod
    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
        member: str | None = None,
    ) -> Path:
        """Download the variable-subset GRIB2 for one `(cycle, step[, member])`.

        Args:
            model: The resolved catalog row.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names (keys of
                `model.bands`).
            mirror: The selected cloud-mirror key (`"auto"` lets the
                centre choose).
            member: Ensemble member id for an ensemble model, or `None`
                for a deterministic one. Only the ensemble-capable centres
                (NOAA GEFS, ECMWF ENS) use it; the others ignore it.

        Returns:
            pathlib.Path: The local GRIB2 file holding every requested
                band for this `(cycle, step[, member])`.
        """
        raise NotImplementedError


def resolve_centre(backend: str, save_dir: Path | str) -> _NWPCentre:
    """Construct the :class:`_NWPCentre` for a catalog `backend:` value.

    Args:
        backend: The model's `backend:` value (e.g. `"herbie"`,
            `"direct-https"`).
        save_dir: Directory raw GRIB2 downloads are written to.

    Returns:
        _NWPCentre: A centre instance bound to `save_dir`.

    Raises:
        ValueError: When `backend` has no registered centre.
        ImportError: When the centre module is registered but its
            optional SDK is not installed (re-raised with a hint).
    """
    try:
        module_name, class_name = CENTRE_REGISTRY[backend]
    except KeyError:
        raise ValueError(
            f"no NWP centre registered for backend {backend!r}; "
            f"known backends: {sorted(CENTRE_REGISTRY)}."
        ) from None
    import importlib

    module = importlib.import_module(module_name)
    centre_cls = getattr(module, class_name)
    return centre_cls(save_dir)
