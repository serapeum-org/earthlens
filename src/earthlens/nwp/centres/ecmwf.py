"""ECMWF Open Data centre — IFS GRIB2 fetch via `ecmwf-opendata`.

ECMWF publishes IFS HRES / ENS / AIFS forecasts as open data (CC-BY-4.0,
no auth) and ships the `ecmwf-opendata` client, which does its own
index-based parameter subsetting. :class:`ECMWFCentre` maps the
requested earthlens params to the client's `param` tokens (`"2t"`,
`"tp"`, …), selects the mirror `source`, and returns the local GRIB2
the client wrote.

`ecmwf-opendata` is imported lazily inside
:meth:`ECMWFCentre.fetch_one` so the package imports without the
`[nwp]` extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from earthlens.nwp._helpers import grib_name
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel

#: Maps the earthlens `mirror=` kwarg to an `ecmwf-opendata` `source`.
#: `ecmwf-opendata` serves from `ecmwf` / `aws` / `azure` only (no GCP),
#: so `"gcp"` and `"origin"` fall back to the ECMWF dissemination host.
_MIRROR_TO_SOURCE: dict[str, str] = {
    "aws": "aws",
    "azure": "azure",
    "ecmwf": "ecmwf",
    "origin": "ecmwf",
    "gcp": "ecmwf",
}


def _import_client() -> Any:
    """Import and return the `ecmwf.opendata.Client` class.

    Returns:
        The `ecmwf.opendata.Client` class.

    Raises:
        ImportError: When `ecmwf-opendata` is not installed; the message
            names the `earthlens[nwp]` extra.
    """
    try:
        from ecmwf.opendata import Client
    except ImportError as exc:
        raise ImportError(
            "the NWP ECMWF centre needs `ecmwf-opendata`; install "
            "`pip install earthlens[nwp]`."
        ) from exc
    return Client


def _source_for(mirror: str, model: NWPModel) -> str:
    """Resolve the `ecmwf-opendata` `source` from the `mirror=` kwarg (`G5`).

    Args:
        mirror: The `mirror=` kwarg.
        model: The resolved catalog row (its `mirrors:` order drives
            `"auto"`).

    Returns:
        str: An `ecmwf-opendata` `source` (`"aws"`, `"azure"`, or
            `"ecmwf"`).
    """
    if mirror == "auto":
        for candidate in model.mirrors:
            if candidate in _MIRROR_TO_SOURCE:
                return _MIRROR_TO_SOURCE[candidate]
        return "ecmwf"
    return _MIRROR_TO_SOURCE.get(mirror, "ecmwf")


class ECMWFCentre(_NWPCentre):
    """`ecmwf-opendata`-backed fetcher for the IFS models."""

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
    ) -> Path:
        """Retrieve the param-subset GRIB2 for one `(cycle, step)`.

        Args:
            model: The resolved catalog row.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: The selected cloud-mirror key.

        Returns:
            pathlib.Path: The local param-subset GRIB2 file.
        """
        client_cls = _import_client()
        client = client_cls(source=_source_for(mirror, model))
        target = self.save_dir / grib_name("ifs", cycle, step)
        client.retrieve(
            date=cycle.strftime("%Y-%m-%d"),
            time=cycle.hour,
            step=step,
            type="fc",
            param=[model.bands[p] for p in params],
            target=str(target),
        )
        return target
