"""Météo-France centre — authenticated WCS API (ARPEGE / AROME).

Météo-France serves its open NWP forecasts through the authenticated
API portal (`portail-api.meteofrance.fr`), **not** the unsigned
`mf-nwp-models` S3 bucket (which holds only static geometry — see
:mod:`earthlens.nwp.centres.meteofrance`). The portal is a WSO2 gateway
fronting an OGC **WCS 2.0.1** service: a `GetCoverage` request returns a
server-side-subset GRIB2 directly.

:class:`MeteoFranceAPICentre` (`meteofrance-api` backend) builds one
`GetCoverage` per `(cycle, step, variable)` from templates in the
catalog row's `request_options` (`api_base`, `coverage_service`) and the
per-band coverage-id base, subsetting to the request bbox + valid time,
and concatenates the returned GRIB messages into one `.grib2`.

**Auth.** An application API key from the MF portal, read from the
`METEO_FRANCE_API_KEY` (or `MF_API_KEY`) environment variable and sent
as the `apikey` header. Without it the centre raises
:class:`AuthenticationError`.

!!! warning "Not live-validated"
    This client is built to the confirmed gateway auth scheme + OGC WCS
    2.0.1 shape, but it has not been exercised against a real MF API key.
    The exact coverage-id strings / subset axis labels may need tuning
    per model — they live in the catalog `request_options` + `bands` so
    a fix is a catalog edit, not a code change. Validate with
    `tools/nwp/probe_nwp_model.py <key>` once a key is available.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from earthlens.base import AuthenticationError, redact_url
from earthlens.nwp._helpers import grib_name, valid_time
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    import requests

    from earthlens.nwp.catalog import NWPModel

#: Environment variables checked (in order) for the MF portal API key.
_API_KEY_ENV = ("METEO_FRANCE_API_KEY", "MF_API_KEY")

#: HTTP timeout (seconds) for one WCS GetCoverage request.
_HTTP_TIMEOUT = 300

#: Streaming block size (bytes) for writing a coverage to disk.
_CHUNK_SIZE = 1 << 20

#: Leading bytes of a GRIB edition-1/2 message. A WSO2 gateway answers an
#: expired key or an unpublished run with an XML/JSON fault at `200`, which
#: would otherwise be appended into the `.grib2` as if it were data.
_GRIB_MAGIC = b"GRIB"


def resolve_api_key() -> str:
    """Return the Météo-France API key from the environment.

    Returns:
        str: The portal application API key.

    Raises:
        AuthenticationError: When neither `METEO_FRANCE_API_KEY` nor
            `MF_API_KEY` is set.
    """
    for name in _API_KEY_ENV:
        value = os.environ.get(name)
        if value:
            return value
    raise AuthenticationError(
        "the Météo-France API centre needs an application API key — set "
        f"{' or '.join(_API_KEY_ENV)} (create one at "
        "https://portail-api.meteofrance.fr)."
    )


class MeteoFranceAPICentre(_NWPCentre):
    """Authenticated WCS-API fetcher for the Météo-France models."""

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
        """Fetch each variable via WCS GetCoverage into one `.grib2`.

        Each coverage is streamed to disk block by block rather than
        buffered whole, so a full-globe ARPEGE band does not have to fit in
        memory alongside the file being written. The per-variable bodies are
        appended to a single `.part` that is renamed only once every
        requested band has arrived.

        `member` and `whole` are accepted for interface parity but
        ignored (the Météo-France rows here are deterministic;
        PEARP/PEAROME are a follow-on — and each WCS `GetCoverage`
        returns one per-variable coverage, so there is no subset for
        `whole` to override).

        Args:
            model: The resolved catalog row. `request_options` must carry
                `api_base` and `coverage_service`; `bands` maps each
                param to its WCS coverage-id base.
            cycle: The forecast cycle (run) datetime, UTC.
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: Ignored — the portal is a single origin.
            member: Ignored (see above).
            whole: Ignored — already whole-per-variable.

        Returns:
            pathlib.Path: One local `.grib2` with every requested band's
                server-side-subset GRIB messages.

        Raises:
            ValueError: When `request_options` lacks `api_base` /
                `coverage_service`.
            AuthenticationError: When no API key is configured.
        """
        opts = model.request_options
        api_base = opts.get("api_base")
        coverage_service = opts.get("coverage_service")
        if not api_base or not coverage_service:
            raise ValueError(
                f"model with backend {model.backend!r} needs request_options "
                "with 'api_base' and 'coverage_service' for the WCS API centre."
            )
        from earthlens.base.http import HttpClient, RequestsGet

        client = HttpClient(session=cast("requests.Session | None", RequestsGet()))
        headers = {"apikey": resolve_api_key()}
        url = f"{api_base}/wcs/{coverage_service}/GetCoverage"
        valid = valid_time(cycle, step)
        out = self.save_dir / grib_name(model.model_family or "mf", cycle, step)
        tmp = out.with_name(out.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for param in params:
                    band_offset = handle.tell()
                    params_qs = self._coverage_query(model.bands[param], cycle, valid)
                    response = client.get(
                        url,
                        params=params_qs,
                        headers=headers,
                        timeout=_HTTP_TIMEOUT,
                        stream=True,
                    )
                    # Copy block by block: a full-globe ARPEGE coverage is
                    # large enough that buffering `response.content` before
                    # writing it doubles the peak footprint per band.
                    try:
                        for block in response.iter_content(chunk_size=_CHUNK_SIZE):
                            if block:
                                handle.write(block)
                    finally:
                        response.close()
                    # Each band appends whole GRIB messages, so each one must
                    # itself start with the magic — checking per band catches a
                    # gateway fault returned for a later band only. The buffered
                    # writes are flushed first so the read sees them.
                    handle.flush()
                    with open(tmp, "rb") as probe:
                        probe.seek(band_offset)
                        head = probe.read(len(_GRIB_MAGIC))
                    if head != _GRIB_MAGIC:
                        raise ValueError(
                            f"{redact_url(url)} did not return GRIB2 for band "
                            f"{param!r}: the body starts {head!r}, not "
                            f"{_GRIB_MAGIC!r}. The API key may be rejected, or "
                            "the run is not published yet."
                        )
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out

    def _coverage_query(
        self, coverage_base: str, cycle: dt.datetime, valid: dt.datetime
    ) -> list[tuple[str, str]]:
        """Build the WCS GetCoverage query for one coverage + valid time.

        Args:
            coverage_base: The band's coverage-id base (e.g.
                `"TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"`).
            cycle: The run datetime (appended to the coverage id).
            valid: The valid time (`cycle + step`) — the `time` subset.

        Returns:
            list[tuple[str, str]]: Query items (repeated `subset` keys are
                a list of pairs, not a dict, so both axes are sent).
        """
        # A pressure-level band uses the "COVERAGE@level" convention (level in
        # hPa); it selects an isobaric coverage + a WCS pressure subset (Pa).
        level_hpa: str | None = None
        if "@" in coverage_base:
            coverage_base, level_hpa = coverage_base.split("@", 1)
        coverage_id = f"{coverage_base}___{cycle:%Y-%m-%dT%H.%M.%SZ}"
        query: list[tuple[str, str]] = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", coverage_id),
            ("format", "application/wmo-grib"),
            ("subset", f"time({valid:%Y-%m-%dT%H:%M:%SZ})"),
        ]
        if level_hpa is not None:
            query.append(("subset", f"pressure({int(level_hpa) * 100})"))
        if self.bbox is not None:
            west, south, east, north = self.bbox
            query.append(("subset", f"lat({south},{north})"))
            query.append(("subset", f"long({west},{east})"))
        return query
