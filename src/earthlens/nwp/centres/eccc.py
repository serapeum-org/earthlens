"""ECCC MSC Datamart centre — GDPS/RDPS/HRDPS/GEPS GRIB2 fetch over plain HTTPS.

Environment & Climate Change Canada publishes its operational NWP
forecasts (GDPS, RDPS, HRDPS, GEPS) on the MSC Datamart under the WMO
WIS2 date-partitioned layout: one uncompressed GRIB2 file per
`(cycle, step, variable, level)`, no `.idx`, no SDK. :class:`ECCCCentre`
builds each variable's URL from the catalog `url_template`, downloads
it directly from `dd.weather.gc.ca`, and concatenates the requested
bands' GRIB messages into a single per-`(cycle, step)` file — valid
because GRIB is a stream of self-describing messages, so
`pyramids.grib.open_grib` sees every requested band downstream.

The path schema is

    https://dd.weather.gc.ca/{date:%Y%m%d}/WXO-DD/{model_dir}/{cycle:%H}/{step:03d}/
    {date:%Y%m%d}T{cycle:%H}Z_MSC_{MODEL}_{var}_{grid}_PT{step:03d}H.grib2

where `{var}` is the joined `VARIABLE_LEVEL` token (e.g.
`AirTemp_AGL-2m`, `GeopotentialHeight_IsbL-0500`) curated into each
model row's `bands:` map. ECCC files are uncompressed (unlike DWD's
`.bz2`), so there is no decompression step.

GEPS is the one ensemble model. The bundled `geps` row uses Datamart's
**raw `_allmbrs`** layout — one file per `(cycle, step, variable)` that
already concatenates every member (control + 20 perturbations) — so the
shipped centre has no per-member fetch axis. The static `_band_url`
nonetheless honours an optional `{member:03d}` substitution: a future
catalog row could point at a per-member URL pattern (e.g. the
`grib2/products/...mem{NNN}.grib2` per-statistic feed) without any
centre-code change. Today no row uses it.

License note (`C6` / `G4`). Real-time AMQP push exists via
`sarracenia`, but `sarracenia` is GPL and is therefore **not vendored**;
it is documented as an external path users may run themselves. The
direct HTTPS fetch here is sufficient for batch use.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from earthlens.nwp._helpers import grib_name
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel

#: HTTP timeout (seconds) for one per-variable GRIB2 download.
_HTTP_TIMEOUT = 120

#: Streaming chunk size (1 MiB) for per-band downloads, picked to keep
#: peak resident memory bounded even for the multi-GB GEPS `_allmbrs`
#: ensemble files.
_STREAM_CHUNK = 1 << 20


class ECCCCentre(_NWPCentre):
    """Direct-HTTPS fetcher for ECCC MSC Datamart NWP models."""

    def __init__(self, save_dir: Path | str) -> None:
        """Bind the centre to an output directory and lazily own a `requests.Session`.

        Datamart serves every band from one host (`dd.weather.gc.ca`), so
        a single connection-pooled `requests.Session` is reused across
        all per-band GETs to amortise the TCP / TLS handshake (one
        handshake instead of N for an N-band request).
        """
        super().__init__(save_dir)
        self._session: Any | None = None

    def _get_session(self) -> Any:
        """Return the cached `requests.Session`, importing `requests` on first use."""
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
        member: str | None = None,
    ) -> Path:
        """Download the per-`(cycle, step[, member])` GRIB2 for the requested bands.

        Streams each band into a sibling `.part` and atomically renames
        on full success, so a failure partway through (band N of M) does
        not leave a truncated `.grib2` for a later `open_grib` to misread.
        The HTTP body is streamed in 1 MiB chunks straight to disk, so
        a multi-GB GEPS `_allmbrs` file never has to fit in RAM.

        Args:
            model: The resolved catalog row.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: Reserved for interface parity with the other centres.
                Datamart serves from a single origin host; only the
                values `"auto"` and `"origin"` (the catalog default) are
                accepted, anything else raises `ValueError` rather than
                silently using the same origin.
            member: Ensemble member id (a numeric string like `"0"` for
                the control or `"1"` … `"20"` for the perturbations);
                `None` for the deterministic models. Forwarded into the
                `url_template` as `{member:03d}` when present.

        Returns:
            pathlib.Path: One local `.grib2` holding every requested
                band's messages, in request order.

        Raises:
            ValueError: When the model has no `url_template`, when
                `mirror` is neither `"auto"` nor `"origin"`, or when
                `member` is a non-numeric string the URL template
                cannot format.
            requests.HTTPError: When any band's download fails — the
                partial file is removed first so no truncated `.grib2`
                is left for a later `open_grib` to misread.
        """
        if mirror not in ("auto", "origin"):
            raise ValueError(
                f"ECCC Datamart serves from a single origin host; "
                f"mirror={mirror!r} is not supported (use 'auto' or 'origin')."
            )
        session = self._get_session()
        out = self.save_dir / grib_name(model.model_family, cycle, step, member)
        tmp = out.with_name(out.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for param in params:
                    url = self._band_url(model, param, cycle, step, member)
                    with session.get(url, stream=True, timeout=_HTTP_TIMEOUT) as response:
                        response.raise_for_status()
                        for chunk in response.iter_content(chunk_size=_STREAM_CHUNK):
                            if chunk:
                                handle.write(chunk)
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out

    @staticmethod
    def _band_url(
        model: NWPModel,
        param: str,
        cycle: dt.datetime,
        step: int,
        member: str | None = None,
    ) -> str:
        """Build the Datamart URL for one band.

        Args:
            model: The resolved catalog row (must carry `url_template`).
            param: The requested earthlens parameter name.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            member: Ensemble member id, or `None`. A non-`None` value must
                be a numeric string — it is cast to `int` and substituted
                as `{member:03d}` in the template. Non-numeric ids raise
                `ValueError` with a clear message rather than the deep
                `int()` traceback the bare cast would produce.

        Returns:
            str: The fully-formatted `.grib2` URL.

        Raises:
            ValueError: When the row has no `url_template` (not an
                ECCC Datamart row), or when `member` is a non-numeric
                string.
            KeyError: When `param` names a band the catalog does not
                curate on this row.
        """
        if not model.url_template:
            raise ValueError(
                f"model with backend {model.backend!r} has no url_template; "
                "an ECCC Datamart centre needs one."
            )
        if param not in model.bands:
            raise KeyError(param)
        token = model.bands[param]
        kwargs: dict[str, Any] = {
            "cycle": cycle,
            "date": cycle,
            "step": step,
            "var": token,
        }
        if member is not None:
            if not member.isdigit():
                raise ValueError(
                    f"ECCC ensemble member id must be a numeric string "
                    f"(e.g. '7'); got {member!r}."
                )
            kwargs["member"] = int(member)
        return model.url_template.format(**kwargs)
