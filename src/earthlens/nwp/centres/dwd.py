"""DWD Open Data centre — ICON GRIB2 fetch over plain HTTPS.

DWD publishes ICON forecasts as per-variable, bz2-compressed GRIB2
files over plain HTTPS (no `.idx`, no SDK): one file per
`(cycle, step, variable)`. :class:`DWDCentre` builds each variable's
URL from the catalog `url_template`, downloads and decompresses it
in-flight, and concatenates the decompressed GRIB messages into a
single `.grib2` — valid because GRIB is a stream of self-describing
messages, so `pyramids.grib.open_grib` sees every requested band.

**Grid caveat.** DWD's *native* ICON-global files are on an
**icosahedral** grid (`icon_global_icosahedral_…`), which is not a
regular lat/lon raster and will not crop meaningfully through the
shared `_fetch` pipeline. For a croppable COG the catalog should point
at a regular-lat/lon ICON product (e.g. ICON-EU, or a regridded
global feed). The download path here is correct regardless of grid;
only the downstream crop assumes a regular raster.
"""

from __future__ import annotations

import bz2
from pathlib import Path
from typing import TYPE_CHECKING

from earthlens.nwp._helpers import grib_name
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel

#: HTTP timeout (seconds) for one per-variable `.bz2` download.
_HTTP_TIMEOUT = 120


class DWDCentre(_NWPCentre):
    """Direct-HTTPS fetcher for the DWD ICON models."""

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
    ) -> Path:
        """Download + decompress one `.bz2` per variable into one GRIB2.

        Args:
            model: The resolved catalog row (carries `url_template` and
                the param -> DWD variable-token band map).
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: Ignored — DWD serves from a single origin host
                (kept for interface parity with the other centres).

        Returns:
            pathlib.Path: One local `.grib2` holding every requested
                band's decompressed messages.

        Raises:
            ValueError: When the model has no `url_template` (not a
                direct-HTTPS model).
            requests.HTTPError: When any variable's download fails — the
                partial file is removed first, so no truncated `.grib2`
                is left for a later `open_grib` to misread.
        """
        if not model.url_template:
            raise ValueError(
                f"model with backend {model.backend!r} has no url_template; "
                "a direct-HTTPS centre needs one."
            )
        import requests

        out = self.save_dir / grib_name(model.model_family, cycle, step)
        # Stream into a sibling .part and atomically rename on full success, so
        # a failure partway through (variable 2 of N) never leaves a truncated
        # .grib2 at `out` (L1).
        tmp = out.with_name(out.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for param in params:
                    var = model.bands[param]
                    url = model.url_template.format(
                        cycle=cycle,
                        date=cycle,
                        step=step,
                        var=var,
                        var_lc=var.lower(),
                    )
                    response = requests.get(url, timeout=_HTTP_TIMEOUT)
                    response.raise_for_status()
                    handle.write(bz2.decompress(response.content))
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out
