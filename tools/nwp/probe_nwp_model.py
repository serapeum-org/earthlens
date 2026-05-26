"""Probe live availability of a single NWP model for a recent cycle.

The per-centre analogue of `tools/earthdata/probe_earthdata_granule.py`:
given a catalog model key, resolve it and check — **live** — whether its
forecast is currently fetchable, dispatching on the catalog `backend`:

* `direct-https` (DWD ICON) — HTTP `HEAD` the first band's `.bz2` URL.
* `direct-boto3` (Météo-France) — `head_object` the unsigned S3 key.
* `ecmwf-opendata` (IFS / ENS / AIFS) — `Client.latest()` for the
  newest published cycle (no download).
* `herbie` (NOAA / ECCC) — ask Herbie to resolve the GRIB URL
  (`Herbie(...).grib`); reports "herbie unavailable" if the optional
  SDK / eccodes stack is not installed rather than failing.

This does no bulk download — it is a cheap "is this fetchable right
now?" check, used to vet catalog rows and debug a failing request.

Run with:

    pixi run -e dev python tools/nwp/probe_nwp_model.py gfs
    pixi run -e dev python tools/nwp/probe_nwp_model.py icon-eu --step 3
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io

from earthlens.nwp import Catalog
from earthlens.nwp.catalog import NWPModel


def _recent_cycle(model: NWPModel, hours_ago: int = 8) -> dt.datetime:
    """Return the most recent run datetime for `model` ~`hours_ago` in the past."""
    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=hours_ago)
    hours = sorted(model.cycles_utc) or [0]
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in reversed(hours):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return moment.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def _probe_direct_https(model: NWPModel, cycle: dt.datetime, step: int) -> str:
    """HEAD the first band's URL for a direct-HTTPS model."""
    if not model.url_template or not model.bands:
        return "no url_template/bands"
    import requests

    var = next(iter(model.bands.values()))
    url = model.url_template.format(
        cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
    )
    try:
        resp = requests.head(url, timeout=30, allow_redirects=True)
        return f"HTTP {resp.status_code} ({url})"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__} ({url})"


def _probe_direct_boto3(model: NWPModel, cycle: dt.datetime, step: int) -> str:
    """head_object the first band's key for an unsigned-S3 model."""
    opts = model.request_options
    bucket = opts.get("bucket")
    key_template = opts.get("key_template") or model.url_template
    if not bucket or not key_template or not model.bands:
        return "no bucket/key_template/bands"
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    var = next(iter(model.bands.values()))
    key = key_template.format(
        cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
    )
    s3 = boto3.client(
        "s3",
        region_name=opts.get("region", "eu-west-1"),
        config=Config(signature_version=UNSIGNED),
    )
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        return f"OK {head['ContentLength']} bytes (s3://{bucket}/{key})"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__} (s3://{bucket}/{key})"


def _probe_ecmwf(model: NWPModel) -> str:
    """Ask ecmwf-opendata for the latest published cycle (no download)."""
    try:
        from ecmwf.opendata import Client
    except ImportError:
        return "ecmwf-opendata not installed (pip install earthlens[nwp])"
    opts = model.request_options
    client = Client(source="aws", model=opts.get("ecmwf_model", "ifs"))
    request = {"type": opts.get("type", "fc"), "step": 0}
    if opts.get("stream"):
        request["stream"] = opts["stream"]
    try:
        latest = client.latest(**request)
        return f"latest cycle {latest:%Y-%m-%d %HZ}"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__}: {exc}"


def _probe_herbie(model: NWPModel, cycle: dt.datetime, step: int) -> str:
    """Ask Herbie to resolve the GRIB URL for `(cycle, step)` (no download)."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            from herbie import Herbie
    except Exception:
        return "herbie unavailable (needs the [nwp] extra + eccodes binary)"
    kwargs = {"model": model.model_family, "fxx": step}
    if model.product is not None:
        kwargs["product"] = model.product
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            handle = Herbie(cycle, **kwargs)
            grib = handle.grib
        return f"resolved {grib}" if grib else "no GRIB found at this cycle/step"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__}: {exc}"


def probe(model_key: str, step: int) -> int:
    """Resolve `model_key`, probe its most recent cycle, print the result.

    Args:
        model_key: A curated catalog model key.
        step: The forecast lead time (hours) to probe.

    Returns:
        int: Process exit code (0).
    """
    model = Catalog().get_model(model_key)
    cycle = _recent_cycle(model)
    dispatch = {
        "direct-https": lambda: _probe_direct_https(model, cycle, step),
        "direct-boto3": lambda: _probe_direct_boto3(model, cycle, step),
        "ecmwf-opendata": lambda: _probe_ecmwf(model),
        "herbie": lambda: _probe_herbie(model, cycle, step),
    }
    print(f"{model_key} [{model.backend}] cycle={cycle:%Y-%m-%d %HZ} f{step:03d}")
    print(f"  {dispatch[model.backend]()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to `sys.argv`).

    Returns:
        int: Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_key", help="Catalog model key (e.g. gfs, icon-eu).")
    parser.add_argument(
        "--step", type=int, default=0, help="Forecast lead time in hours (default 0)."
    )
    args = parser.parse_args(argv)
    return probe(args.model_key, args.step)


if __name__ == "__main__":
    raise SystemExit(main())
