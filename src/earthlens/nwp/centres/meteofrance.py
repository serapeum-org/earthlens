"""Météo-France centre — GRIB2 fetch from an unsigned public S3 bucket.

Météo-France publishes ARPEGE / AROME forecasts to the open
`s3://mf-nwp-models` bucket (no credentials). :class:`MeteoFranceCentre`
is the `direct-boto3` adapter: it reads each requested variable's object
with an unsigned `boto3` client (the same pattern as `earthlens.s3`) and
concatenates the GRIB messages into one `.grib2`, written atomically.

The bucket / key layout is taken from the catalog row's
`request_options` (`bucket`, `key_template`, optional `region`), so the
centre carries no provider-specific paths itself. `boto3` is imported
lazily, so the package imports without the `[s3]`/`[nwp]` extras.

!!! note
    No Météo-France catalog row ships, because the open
    `s3://mf-nwp-models` bucket exposes **only `static/`** (landmask /
    terrain) under every model prefix (`arpege-world`, `arpege-europe`,
    `arome-france`, `arome-france-hd`) — verified by an unsigned listing
    of the bucket root + each prefix. The rolling forecasts are served
    through Météo-France's **authenticated API portal**
    (`portail-api.meteofrance.fr`, API-key + REST), which is a different
    access pattern than this unsigned-S3 adapter. So MF needs a separate
    auth+REST client (a future enhancement); this `direct-boto3` centre is
    correct infrastructure for any unsigned-S3 NWP source and the moment a
    public MF mirror with a real `key_template` exists, a row plugs in
    here. Use `tools/nwp/probe_nwp_model.py` to confirm a candidate key.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from earthlens.nwp._helpers import grib_name
from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel


def _unsigned_s3_client(region: str) -> Any:
    """Build an unsigned `boto3` S3 client for a public bucket.

    Args:
        region: AWS region the bucket lives in.

    Returns:
        A `boto3` S3 client configured for anonymous access.

    Raises:
        ImportError: When `boto3` is not installed.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config
    except ImportError as exc:
        raise ImportError(
            "the NWP Météo-France centre needs `boto3`; install "
            "`pip install earthlens[nwp]` (or `earthlens[s3]`)."
        ) from exc
    return boto3.client(
        "s3", region_name=region, config=Config(signature_version=UNSIGNED)
    )


class MeteoFranceCentre(_NWPCentre):
    """Unsigned-S3 fetcher for the Météo-France ARPEGE / AROME models."""

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
        member: str | None = None,
    ) -> Path:
        """Download each variable's S3 object into one `.grib2`.

        `member` is accepted for interface parity but ignored (the
        Météo-France rows here are deterministic).

        Args:
            model: The resolved catalog row (carries `request_options`
                with `bucket` / `key_template` and the param -> variable
                band map).
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: Ignored — the bucket is a single origin (kept for
                interface parity).

        Returns:
            pathlib.Path: One local `.grib2` holding every requested
                variable's GRIB messages.

        Raises:
            ValueError: When the row has no `bucket` / `key_template` in
                `request_options`.
        """
        opts = model.request_options
        bucket = opts.get("bucket")
        key_template = opts.get("key_template")
        if not bucket or not key_template:
            raise ValueError(
                f"model with backend {model.backend!r} needs request_options "
                "with 'bucket' and 'key_template' for the boto3 centre."
            )
        client = _unsigned_s3_client(opts.get("region", "eu-west-1"))
        out = self.save_dir / grib_name(model.model_family or "mf", cycle, step)
        tmp = out.with_name(out.name + ".part")
        try:
            with open(tmp, "wb") as handle:
                for param in params:
                    var = model.bands[param]
                    key = key_template.format(
                        cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
                    )
                    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                    handle.write(body)
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out
