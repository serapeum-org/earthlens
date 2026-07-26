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
from collections.abc import Iterable
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from earthlens.base import redact_url
from earthlens.nwp._helpers import grib_name
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    import requests

    from earthlens.nwp.catalog import NWPModel

#: HTTP timeout (seconds) for one per-variable `.bz2` download.
_HTTP_TIMEOUT = 120

#: Streaming block size (bytes) fed to the incremental bz2 decompressor.
_CHUNK_SIZE = 1 << 20


def _decompress_stream(blocks: Iterable[bytes], handle: IO[bytes], url: str) -> None:
    """Decompress a `.bz2` byte stream into `handle`, rejecting a short body.

    `bz2.decompress()` on a whole body raises when the data ends before the
    end-of-stream marker, and transparently decodes a file made of several
    concatenated streams. A bare `BZ2Decompressor` fed chunk by chunk does
    neither: it returns what it has with `eof` still `False` for a truncated
    body, and stops at the first stream's end, silently discarding the rest.
    Either would publish a short `.grib2` that only fails later, inside
    `open_grib`. This restores both behaviours while keeping the memory
    profile of a streamed read.

    Args:
        blocks: The compressed body, in arbitrary-sized chunks.
        handle: Binary file object the decompressed bytes are appended to.
        url: Source URL, redacted into any error message.

    Raises:
        ValueError: If the stream ends before its end-of-stream marker — i.e.
            the download was truncated.
    """
    decompressor = bz2.BZ2Decompressor()
    saw_data = False
    for block in blocks:
        if not block:
            continue
        saw_data = True
        handle.write(decompressor.decompress(block))
        # A multi-stream `.bz2` (several concatenated streams) leaves the
        # remainder in `unused_data`; keep going with a fresh decompressor so
        # every stream is decoded, as `bz2.decompress()` does.
        while decompressor.eof and decompressor.unused_data:
            leftover = decompressor.unused_data
            decompressor = bz2.BZ2Decompressor()
            handle.write(decompressor.decompress(leftover))
    if saw_data and not decompressor.eof:
        raise ValueError(
            f"{redact_url(url)} returned a truncated bz2 body: the stream "
            "ended before its end-of-stream marker, so the decompressed GRIB2 "
            "would be short. Retry the download."
        )


class DWDCentre(_NWPCentre):
    """Direct-HTTPS fetcher for the DWD ICON models."""

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
        member: str | None = None,
        *,
        whole: bool = False,
    ) -> Path:
        """Download + decompress one `.bz2` per variable into one GRIB2.

        Each variable is streamed and fed through an incremental
        `bz2.BZ2Decompressor`, so neither the compressed body nor the
        decompressed result is ever held whole in memory — a global ICON
        band runs to hundreds of megabytes on each side. The decompressed
        messages are appended to a single `.part` that is renamed only once
        every variable has succeeded.

        `member` and `whole` are accepted for interface parity but ignored
        — the ICON rows here are deterministic (ICON-EPS is a separate
        model), and DWD already serves one whole `.bz2` per variable, so
        there is no byte-range subset for `whole` to override.

        Args:
            model: The resolved catalog row (carries `url_template` and
                the param -> DWD variable-token band map).
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: Ignored — DWD serves from a single origin host
                (kept for interface parity with the other centres).
            member: Ignored (see above).
            whole: Ignored — already whole-per-variable.

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
        from earthlens.base.http import HttpClient, RequestsGet

        client = HttpClient(session=cast("requests.Session | None", RequestsGet()))
        out = self.save_dir / grib_name(model.model_family, cycle, step)
        # Stream into a sibling .part and atomically rename on full success, so
        # a failure partway through (variable 2 of N) never leaves a truncated
        # .grib2 at `out` (L1).
        tmp = out.with_name(out.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for param in params:
                    url = self._band_url(model, param, cycle, step)
                    response = client.get(url, timeout=_HTTP_TIMEOUT, stream=True)
                    # Decompress incrementally: a global ICON band is a
                    # multi-hundred-MB .bz2, and `bz2.decompress(resp.content)`
                    # would hold both the whole compressed body and the whole
                    # decompressed result in memory at once.
                    try:
                        _decompress_stream(
                            response.iter_content(chunk_size=_CHUNK_SIZE), handle, url
                        )
                    finally:
                        response.close()
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out

    @staticmethod
    def _band_url(model: NWPModel, param: str, cycle: dt.datetime, step: int) -> str:
        """Build the DWD URL for one band — single-level or pressure-level.

        A surface band token is a bare DWD variable (`"T_2M"`) and uses the
        model's `url_template`. A pressure-level token uses the
        `VAR@level` convention (`"T@850"`) and the `pl_url_template` in
        `request_options`, which additionally takes a `{level}` field.

        Args:
            model: The resolved catalog row.
            param: The requested earthlens parameter name.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.

        Returns:
            str: The fully-formatted `.grib2.bz2` URL.

        Raises:
            ValueError: When a pressure-level band is requested but the
                row has no `pl_url_template`, or the row has no
                single-level `url_template` for a surface band.
        """
        token = model.bands[param]
        if "@" in token:
            var, level = token.split("@", 1)
            template = model.request_options.get("pl_url_template")
            if not template:
                raise ValueError(
                    f"model {model.model_family!r} has no 'pl_url_template' in "
                    f"request_options for pressure-level band {param!r}."
                )
            return cast(
                "str",
                template.format(
                    cycle=cycle,
                    date=cycle,
                    step=step,
                    level=level,
                    var=var,
                    var_lc=var.lower(),
                ),
            )
        if not model.url_template:
            raise ValueError(
                f"model with backend {model.backend!r} has no url_template; "
                "a direct-HTTPS centre needs one."
            )
        return model.url_template.format(
            cycle=cycle, date=cycle, step=step, var=token, var_lc=token.lower()
        )
