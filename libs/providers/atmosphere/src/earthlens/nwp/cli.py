"""Catalog-tooling handlers for the NWP backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The light prober reads a
model's live GRIB `.idx` (no eccodes); the deep prober dispatches a per-backend
availability check; the validator lints the curated models offline.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, cast

import requests

from earthlens.cli.toolkit import HTTP_TIMEOUT, http_head

_NWP_NO_IDX_FAMILIES = {"gdps", "rdps", "hrdps"}
_NWP_NEEDS_EXTRA_ATTRS = {"hiresw", "href", "gefs"}


def _herbie_models_dir() -> Any:
    """Locate the installed `herbie/models` template directory.

    Raises:
        FileNotFoundError: When `herbie` is not importable (install
            `earthlens[nwp]`; the templates are read as data, no eccodes).
    """
    import pathlib
    import sys

    for entry in sys.path:
        candidate = pathlib.Path(entry) / "herbie" / "models"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "herbie is not installed; install `pip install earthlens[nwp]`"
    )


class _TemplateStub:
    """Minimal Herbie stand-in so a model template's f-strings resolve."""

    def __init__(self, date: Any, fxx: int, product: str) -> None:
        """Capture the cycle date, forecast step, and product the template reads."""
        self.date = date
        self.fxx = fxx
        self.product = product

    def __getattr__(self, name: str) -> str:
        """Resolve any attribute the template touches to an empty string."""
        return ""


def _nwp_idx_url(models_dir: Any, model: Any, cycle: Any, step: int) -> str:
    """Format a model's `.idx` URL from its installed Herbie template.

    Reads Herbie's own template file as data (via `runpy`) rather than
    importing `herbie` (whose package init pulls the `cfgrib`/`eccodes`
    stack), then evaluates it against a stub to recover the AWS/NOMADS URL.
    Because `runpy.run_path` executes the named file, the catalog-supplied
    `model_family` is validated to a bare identifier first so it can only
    name a file inside herbie's installed `models/` dir.

    Args:
        models_dir: The installed `herbie/models` template directory.
        model: The curated NWP model record (uses `model_family` / `product`).
        cycle: The model run datetime to format into the URL.
        step: The forecast step (hours) to format into the URL.

    Returns:
        The `.idx` URL for the requested cycle / step.

    Raises:
        ValueError: If `model_family` is not a bare `[A-Za-z0-9_]+` identifier.
    """
    import runpy

    # `runpy.run_path` executes the named file, so guard the catalog-supplied
    # `model_family` to a bare identifier: it must name a file inside herbie's
    # installed models/ dir, never traverse out of it or smuggle in a path.
    family = model.model_family or ""
    if not re.fullmatch(r"[A-Za-z0-9_]+", family):
        raise ValueError(f"unsafe model_family for .idx template: {family!r}")
    namespace = runpy.run_path(str(models_dir / f"{family}.py"))
    template_cls = namespace.get(model.model_family) or next(
        value
        for value in namespace.values()
        if isinstance(value, type) and hasattr(value, "template")
    )
    stub = _TemplateStub(cycle, step, getattr(model, "product", "") or "")
    template_cls.template(stub)
    if not getattr(model, "product", "") and getattr(stub, "PRODUCTS", None):
        stub = _TemplateStub(cycle, step, list(stub.PRODUCTS)[0])
        template_cls.template(stub)
    # The executed Herbie template assigns `stub.SOURCES` a dict at runtime;
    # the stub's `__getattr__` only advertises a `str` return to the type checker.
    sources = cast("dict[str, str]", stub.SOURCES)
    base = sources.get("aws") or sources.get("nomads") or next(iter(sources.values()))
    return base + ".idx"


def _nwp_idx_body(model: Any) -> str:
    """Fetch the live `.idx` text for a model's most recent reachable cycle.

    Raises:
        ValueError: If no recent cycle's `.idx` is reachable.
    """
    import datetime as dt

    models_dir = _herbie_models_dir()
    step = 1 if (getattr(model, "horizon_h", 0) or 0) >= 1 else 0
    for days_back in (1, 2):
        cycle = (
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days_back)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        url = _nwp_idx_url(models_dir, model, cycle, step)
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT)
        except Exception:  # noqa: BLE001 — try the previous day  # nosec B112
            continue
        if response.status_code == 200:
            return response.text
    raise ValueError("no recent .idx is reachable for this model")


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an NWP model's bands against its live GRIB `.idx` (no eccodes).

    Reads Herbie's installed template to build the `.idx` URL for a recent
    cycle, fetches it, and reports which of the model's catalog band tokens
    are present — the live drift check `tools/nwp/probe_idx.py` does.

    Args:
        catalog: The loaded NWP `Catalog`.
        dataset: A curated model key.

    Returns:
        Mapping of band name to `{token, present}`.

    Raises:
        ValueError: For models with no `.idx` (ECCC) or whose template needs
            domain / member / resolution, or an unknown model key.
    """
    model = catalog.datasets.get(dataset)
    if model is None:
        raise ValueError(f"unknown NWP model {dataset!r}")
    family = getattr(model, "model_family", None)
    if family in _NWP_NO_IDX_FAMILIES:
        raise ValueError(f"{dataset}: ECCC per-variable files have no .idx to probe")
    if family in _NWP_NEEDS_EXTRA_ATTRS:
        raise ValueError(
            f"{dataset}: template needs domain/member/resolution; use the SDK"
        )
    body = _nwp_idx_body(model)
    bands = getattr(model, "bands", None) or {}
    return {
        str(band): {"token": token, "present": bool(re.search(re.escape(token), body))}
        for band, token in bands.items()
    }


def _nwp_recent_cycle(model: Any) -> Any:
    """Return the model's most recent run datetime (~8 h in the past)."""
    import datetime as dt

    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=8)
    hours = sorted(getattr(model, "cycles_utc", None) or []) or [0]
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in reversed(hours):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return moment.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def _nwp_probe_direct_https(model: Any, cycle: Any, step: int) -> str:
    """Probe a `direct-https` model with an HTTP HEAD on its url_template."""
    bands = getattr(model, "bands", None) or {}
    if not getattr(model, "url_template", None) or not bands:
        return "no url_template/bands"
    var = next(iter(bands.values()))
    url = model.url_template.format(
        cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
    )
    try:
        code = requests.head(
            url, timeout=HTTP_TIMEOUT, allow_redirects=True
        ).status_code
        return f"HTTP {code} ({url})"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"unreachable: {type(exc).__name__} ({url})"


def _nwp_probe_direct_boto3(model: Any, cycle: Any, step: int) -> str:
    """Probe a `direct-boto3` model with an unsigned-S3 head_object."""
    bands = getattr(model, "bands", None) or {}
    options = getattr(model, "request_options", None) or {}
    bucket = options.get("bucket")
    key_template = options.get("key_template") or getattr(model, "url_template", "")
    if not (bucket and key_template and bands):
        return "no bucket/key_template/bands"
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    var = next(iter(bands.values()))
    key = key_template.format(
        cycle=cycle, date=cycle, step=step, var=var, var_lc=var.lower()
    )
    client = boto3.client(
        "s3",
        region_name=options.get("region", "eu-west-1"),
        config=Config(signature_version=UNSIGNED),
    )
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        return f"OK {head['ContentLength']} bytes (s3://{bucket}/{key})"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"unreachable: {type(exc).__name__} (s3://{bucket}/{key})"


def _nwp_probe_ecmwf_opendata(model: Any, cycle: Any, step: int) -> str:
    """Probe an `ecmwf-opendata` model by asking the SDK for its latest cycle."""
    options = getattr(model, "request_options", None) or {}
    try:
        from ecmwf.opendata import Client
    except ImportError:
        return "ecmwf-opendata not installed (pip install earthlens[nwp])"
    client = Client(source="aws", model=options.get("ecmwf_model", "ifs"))
    request = {"type": options.get("type", "fc"), "step": 0}
    if options.get("stream"):
        request["stream"] = options["stream"]
    try:
        return f"latest cycle {client.latest(**request):%Y-%m-%d %HZ}"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"unreachable: {type(exc).__name__}: {exc}"


def _nwp_probe_meteofrance(model: Any, cycle: Any, step: int) -> str:
    """Probe a `meteofrance-api` model with a keyed WCS GetCapabilities."""
    options = getattr(model, "request_options", None) or {}
    api_base, service = options.get("api_base"), options.get("coverage_service")
    if not (api_base and service):
        return "no api_base/coverage_service in request_options"
    key = os.environ.get("METEO_FRANCE_API_KEY") or os.environ.get("MF_API_KEY")
    if not key:
        return "needs METEO_FRANCE_API_KEY"
    url = f"{api_base}/wcs/{service}/GetCapabilities"
    try:
        code = requests.get(
            url,
            params={"service": "WCS", "version": "2.0.1"},
            headers={"apikey": key},
            timeout=HTTP_TIMEOUT,
        ).status_code
        return f"HTTP {code} ({url})"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"unreachable: {type(exc).__name__} ({url})"


def _nwp_probe_herbie(model: Any, cycle: Any, step: int) -> str:
    """Probe a `herbie` model by resolving its GRIB source for the cycle."""
    import contextlib
    import io

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            from herbie import Herbie
    except Exception:  # noqa: BLE001 — optional SDK / eccodes binary
        return "herbie unavailable (needs the [nwp] extra + eccodes binary)"
    kwargs: dict[str, Any] = {"model": model.model_family, "fxx": step}
    if getattr(model, "product", None) is not None:
        kwargs["product"] = model.product
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            grib = Herbie(cycle, **kwargs).grib
        return f"resolved {grib}" if grib else "no GRIB at this cycle/step"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"unreachable: {type(exc).__name__}: {exc}"


_NWP_PROBES: dict[str, Callable[[Any, Any, int], str]] = {
    "direct-https": _nwp_probe_direct_https,
    "direct-boto3": _nwp_probe_direct_boto3,
    "ecmwf-opendata": _nwp_probe_ecmwf_opendata,
    "meteofrance-api": _nwp_probe_meteofrance,
    "herbie": _nwp_probe_herbie,
    # ECCC Datamart uses the same per-variable HTTPS HEAD pattern as DWD.
    "eccc-msc": _nwp_probe_direct_https,
}


def _nwp_availability(model: Any, cycle: Any, step: int) -> str:
    """Return a live 'is this fetchable now?' status, dispatching on backend.

    Ports `tools/nwp/probe_nwp_model.py`: a cheap availability check per
    centre (HTTP HEAD / unsigned-S3 head_object / ecmwf-opendata latest /
    Météo-France GetCapabilities / Herbie GRIB resolve). No bulk download.
    Each centre's check lives in a `_nwp_probe_*` helper keyed by backend
    in `_NWP_PROBES`.
    """
    backend = getattr(model, "backend", None)
    probe = _NWP_PROBES.get(backend) if backend is not None else None
    return (
        probe(model, cycle, step)
        if probe is not None
        else f"no live availability probe for backend {backend!r}"
    )


def _nwp_deep_sample(model: Any, step: int) -> dict[str, dict[str, Any]]:
    """Return a model's live availability for its most recent cycle."""
    cycle = _nwp_recent_cycle(model)
    backend = getattr(model, "backend", "?")
    return {
        f"{backend} @ {cycle:%Y-%m-%d %HZ}": {
            "status": _nwp_availability(model, cycle, step),
            "step": step,
        }
    }


def deep_prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an NWP model's live availability (full dispatch, not .idx).

    Ports `tools/nwp/probe_nwp_model.py`: checks whether the model's most
    recent cycle is fetchable right now via its real backend (Herbie needs
    the eccodes binary; ecmwf-opendata / boto3 / meteofrance need their
    SDKs / keys). The light `probe nwp` only checks `.idx` band tokens.

    Raises:
        ValueError: If `dataset` is not a curated NWP model.
    """
    model = catalog.datasets.get(dataset)
    if model is None:
        raise ValueError(f"unknown NWP model {dataset!r}")
    step = 1 if (getattr(model, "horizon_h", 0) or 0) >= 1 else 0
    return _nwp_deep_sample(model, step)


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Offline structural lint of the curated NWP models.

    Mirrors `tools/nwp/audit_nwp_catalog.py`: flags `direct-https` models
    with no `url_template`, `herbie` models with no `model_family`, empty
    band maps, and cycle hours outside 0-23.

    Args:
        catalog: The loaded NWP `Catalog`.

    Returns:
        `(checked, issues)` — the model count and one message per problem.
    """
    issues: list[str] = []
    # Backends whose fetcher reads `model.url_template` directly. Adding
    # a new direct-fetch backend means adding it here so a missing
    # `url_template` is caught at lint time, not at fetch time.
    _DIRECT_URL_BACKENDS = ("direct-https", "eccc-msc")
    models = catalog.datasets
    for key, record in models.items():
        backend = getattr(record, "backend", None)
        if backend in _DIRECT_URL_BACKENDS and not getattr(
            record, "url_template", None
        ):
            issues.append(f"{key}: {backend} model has no url_template")
        if backend == "herbie" and not getattr(record, "model_family", None):
            issues.append(f"{key}: herbie model has no model_family")
        if not (getattr(record, "bands", None) or {}):
            issues.append(f"{key}: empty band map")
        for hour in getattr(record, "cycles_utc", None) or []:
            if isinstance(hour, int) and not 0 <= hour <= 23:
                issues.append(f"{key}: cycle hour {hour} out of range")
    return len(models), issues


def _nwp_latest_cycle(model: Any, hours_ago: int = 6) -> Any:
    """Return a model's most recent expected run datetime (or None).

    Ported from the retired `tools/nwp/refresh_nwp_catalog.py`.

    Args:
        model: A curated NWP model record (duck-typed: `cycles_utc`).
        hours_ago: How far back to look for the latest published cycle.

    Returns:
        The most recent cycle datetime at or before `now - hours_ago`,
        or None when the model declares no cycle hours.
    """
    import datetime as dt

    if not getattr(model, "cycles_utc", None):
        return None
    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(
        hours=hours_ago
    )
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in sorted(model.cycles_utc, reverse=True):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return None


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """HEAD each direct-https NWP model's latest expected cycle URL.

    Folds the retired `refresh_nwp_catalog.py --live`: only `direct-https`
    models (e.g. DWD ICON) can be checked with a cheap HEAD — Herbie / ECMWF
    models would need their SDKs to resolve a cycle, so they are skipped.
    """
    issues: list[str] = []
    checked = 0
    for key, model in catalog.datasets.items():
        if getattr(model, "backend", None) != "direct-https":
            continue
        cycle = _nwp_latest_cycle(model)
        url_template = getattr(model, "url_template", None)
        bands = getattr(model, "bands", None)
        if cycle is None or not url_template or not bands:
            continue
        checked += 1
        var = next(iter(bands.values()))
        url = url_template.format(
            cycle=cycle, date=cycle, step=0, var=var, var_lc=str(var).lower()
        )
        try:
            status = http_head(url)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: latest cycle unreachable ({type(exc).__name__})")
            continue
        if status != 200:
            issues.append(f"{key}: HTTP {status} for latest cycle {url}")
    return checked, issues
