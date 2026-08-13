"""Curation probes — extract a dataset's band/asset schema from a live sample.

The companion to :mod:`earthlens.cli.refresh`. Where `refresh` regenerates the
informational `available_*` index, `probe` produces the *seed* for the curated,
load-bearing rows: it fetches one sample record from a provider and records the
per-band / per-asset metadata (media type, common name, dtype, nodata) a
maintainer reviews before pasting into the catalog. This is the CLI port of the
`tools/*/probe_*.py` scripts.

Like `refresh`, only providers with a usable sample source have a prober
wired up; others report `unsupported`. Adding one is a single entry in
:data:`_PROBERS`. The heavier **credentialed** samplers (real NetCDF /
granule / CDS retrieval / full NWP availability) live in :data:`_DEEP_PROBERS`
and are reached with `probe --deep` (cmems, earthdata, ecmwf, nwp); `--deep`
falls back to the light prober for providers without a deep sampler.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import requests

from earthlens._cli_tooling import dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _TIMEOUT


@dataclass
class ProbeResult:
    """The asset/band schema probed for one dataset.

    Attributes:
        provider: Canonical provider id.
        dataset: The dataset / collection probed.
        status: `"ok"`, `"unsupported"` (no prober), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        assets: Mapping of asset key -> `{media_type, common_name, dtype,
            nodata}` (empty unless `status == "ok"`).
    """

    provider: str
    dataset: str
    status: str
    detail: str = ""
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The probed asset schema is nested under `assets`:

                ```python
                >>> from earthlens.cli.curate import ProbeResult
                >>> result = ProbeResult(
                ...     "stac", "sentinel-2-l2a", "ok",
                ...     assets={"B04": {"common_name": "red"}},
                ... )
                >>> result.to_dict()["assets"]["B04"]["common_name"]
                'red'

                ```
        """
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "status": self.status,
            "detail": self.detail,
            "assets": self.assets,
        }


def _bands_from_summaries(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a STAC doc's `summaries.eo:bands` (or `gee:bands`) list."""
    summaries = body.get("summaries", {}) or {}
    return summaries.get("eo:bands") or summaries.get("gee:bands") or []


def _infer_dtype(value: str | None) -> str:
    """Infer a coarse dtype (`int` / `float` / `str`) from a sample value."""
    if value is None or value == "":
        return "str"
    try:
        int(value)
        return "int"
    except ValueError:
        pass
    try:
        float(value)
        return "float"
    except ValueError:
        return "str"


#: EUMETSAT public browse collections endpoint (no credentials).


def _ecmwf_constraints(dataset: str) -> list[dict[str, Any]]:
    """Return a dataset's public `constraints.json` rows (no creds).

    Resolves the dataset's CADS endpoint from the catalog so EWDS/ADS datasets
    are fetched from their own catalogue host rather than the CDS host (which
    would 404 and silently return no rows).
    """
    from earthlens.ecmwf.catalog import Catalog
    from earthlens.ecmwf.constraints import fetch_constraints
    from earthlens.ecmwf.endpoints import constraints_base_url

    record = Catalog().datasets.get(dataset)
    endpoint = record.endpoint if record is not None else "cds"
    return fetch_constraints(dataset, constraints_base_url(endpoint))


def _ecmwf_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an ECMWF/CDS dataset's variables from its public constraints.

    Reads `constraints.json` (public, no credentials — only data retrieval
    needs `~/.cdsapirc`) and unions the `variable` values across rows.

    Args:
        catalog: The loaded ECMWF `Catalog` (unused; the CDS dataset is the key).
        dataset: A CDS dataset id (e.g. `reanalysis-era5-single-levels`).

    Returns:
        Mapping of variable name to `{}` (the seed for the catalog `variables`).
    """
    variables = sorted(
        {
            variable
            for row in _ecmwf_constraints(dataset)
            for variable in (row.get("variable") or [])
        }
    )
    return {str(variable): {} for variable in variables}


def _chc_sample_files(ftp_base: str, limit: int = 10) -> list[str]:
    """Return a sample of filenames under a CHC FTP directory (anonymous)."""
    from ftplib import FTP  # nosec B402

    with FTP("data.chc.ucsb.edu", timeout=_TIMEOUT) as ftp:  # nosec B321
        ftp.login()
        ftp.cwd(ftp_base)
        return sorted(ftp.nlst())[:limit]


def _suggest_pattern(filenames: list[str]) -> str:
    """Infer a `{year}.{month}.{day}`-style template from a sample filename.

    Ported from the retired `tools/chc/probe_chirps_gefs.py`: tags 4-digit
    years, 3-digit day-of-year runs, then the first two dotted 2-digit
    segments as month / day. A seed for the catalog `file_patterns` — the
    maintainer eyeballs it against the listing and refines.

    Args:
        filenames: The sampled directory listing.

    Returns:
        The first filename transformed into a template, or `""` when empty.
    """
    if not filenames:
        return ""
    pattern = re.sub(r"\b(19|20)\d{2}\b", "{year}", filenames[0])
    pattern = re.sub(r"(?<!\d)(\d{3})(?!\d)", "{doy}", pattern)
    seen_month = False
    out: list[str] = []
    for piece in re.split(r"(\{year\})", pattern):
        if piece == "{year}":
            out.append(piece)
            continue
        new_piece = piece
        if not seen_month:
            new_piece, hits = re.subn(
                r"(?<=\.)(\d{2})(?=\.|$)", "{month}", new_piece, count=1
            )
            seen_month = bool(hits)
        if seen_month and "{day}" not in new_piece:
            new_piece = re.sub(r"(?<=\.)(\d{2})(?=\.|$)", "{day}", new_piece, count=1)
        out.append(new_piece)
    return "".join(out)


def _chc_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a CHC dataset's FTP directory for a sample of filenames.

    Args:
        catalog: The loaded CHC `Catalog` (resolves the dataset's `ftp_bases`).
        dataset: A curated CHC dataset key.

    Returns:
        Mapping of sample filename to `{}`, plus a `(suggested pattern)` row
        carrying a `{pattern}` template inferred from the listing (the seed
        for the catalog `file_patterns`).

    Raises:
        ValueError: If the dataset has no `ftp_bases`.
    """
    record = catalog.datasets.get(dataset)
    bases = list(getattr(record, "ftp_bases", {}).values()) if record else []
    if not bases:
        raise ValueError(f"no ftp_bases for {dataset!r}")
    files = _chc_sample_files(bases[0])
    schema: dict[str, dict[str, Any]] = {name: {} for name in files}
    pattern = _suggest_pattern(files)
    if pattern:
        schema["(suggested pattern)"] = {"pattern": pattern}
    return schema


def _tropycal_fields(basin: str, source: str) -> dict[str, dict[str, Any]]:
    """Return a basin's `Storm.to_dataframe()` field schema (samples a season)."""
    import datetime as dt

    import tropycal.tracks as tracks

    track_dataset = tracks.TrackDataset(basin=basin, source=source)
    year = dt.datetime.now(dt.UTC).year - 1
    storm_ids = list(track_dataset.get_season(year).summary().get("id") or [])[:3]
    fields: dict[str, dict[str, Any]] = {}
    for storm_id in storm_ids:
        frame = track_dataset.get_storm(storm_id).to_dataframe(attrs_as_columns=True)
        for column in frame.columns:
            fields.setdefault(str(column), {"dtype": str(frame[column].dtype)})
    return fields


def _tropycal_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a Tropycal basin's live `to_dataframe()` field schema (SDK).

    Args:
        catalog: The loaded Tropycal `Catalog` (resolves the basin's sources).
        dataset: A basin code (e.g. `north_atlantic`).

    Returns:
        Mapping of field name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    sources = getattr(record, "sources", None) or ["hurdat"]
    return _tropycal_fields(dataset, sources[0])


#: ECCC models ship one whole GRIB per variable (no `.idx` byte-index), so the
#: idx-token check can't apply; template families whose URL also needs
#: domain / member / resolution aren't synthesised here either.
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
            response = requests.get(url, timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001 — try the previous day  # nosec B112
            continue
        if response.status_code == 200:
            return response.text
    raise ValueError("no recent .idx is reachable for this model")


def _nwp_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
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


# --------------------------------------------------------------------------- #
# Deep probers (the `--deep` half) — credentialed, data-downloading samplers
# that read the REAL on-disk schema, vs the light public metadata probers
# above. Each live call sits behind a mockable helper; the credentials come
# from the environment (copernicusmarine / earthaccess) or ~/.cdsapirc.
# --------------------------------------------------------------------------- #


def _read_netcdf_var_meta(path: str) -> dict[str, dict[str, Any]]:
    """Read each NetCDF variable's `long_name` / `units` via GDAL.

    Uses the GDAL vendored by `pyramids` (no hard `netCDF4` dependency): GDAL
    surfaces the CF attributes as band metadata, exposing a multi-variable file
    as one subdataset per variable.

    Args:
        path: Path to a NetCDF file written by a `cdsapi` retrieve.

    Returns:
        A `{variable_name: {"long_name": ..., "units": ...}}` mapping for every
        variable that carries a `long_name` or `units` attribute.
    """
    from osgeo import gdal

    gdal.UseExceptions()

    def _from_info(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Extract per-band `NETCDF_VARNAME` / long_name / units from gdal.Info."""
        out: dict[str, dict[str, Any]] = {}
        for band in info.get("bands", []) or []:
            meta = band.get("metadata", {}).get("", {})
            name = meta.get("NETCDF_VARNAME")
            long_name, units = meta.get("long_name", ""), meta.get("units", "")
            if name and (long_name or units):
                out[str(name)] = {"long_name": long_name, "units": units}
        return out

    top = gdal.Info(path, format="json")
    subs = top.get("metadata", {}).get("SUBDATASETS", {})
    if not subs:
        return _from_info(top)
    schema: dict[str, dict[str, Any]] = {}
    for key, sub_path in subs.items():
        if key.endswith("_NAME"):
            schema.update(_from_info(gdal.Info(sub_path, format="json")))
    return schema


def _ecmwf_deep_sample(dataset: str) -> dict[str, dict[str, Any]]:
    """Retrieve a tiny CDS NetCDF and read each variable's long_name/units.

    Builds a **complete** minimal request from the dataset's first usable
    `constraints.json` entry — one value per selector, so the family selectors
    a dataset requires beyond year/month/day (a satellite CDR's
    sensor / version / record-type / aggregation, CMIP's experiment/model, ...)
    are carried and the retrieve is a valid combination rather than a 400.
    Only keys the entry actually enumerates are sent, so a product that does
    not partition by day/time (obs4mips CO2/CH4) is not handed a spurious one.
    Retrieves via `cdsapi` (`~/.cdsapirc`); a zip-of-NetCDF response (satellite
    CDRs deliver one) is unwrapped to its first member before the variable
    metadata is read via GDAL.
    """
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    import cdsapi

    rows = _ecmwf_constraints(dataset)
    if not rows:
        return {}
    # Prefer the first entry that enumerates a variable (a usable retrieve);
    # fall back to the first entry for datasets with no variable dimension.
    row = next((entry for entry in rows if entry.get("variable")), rows[0])
    request: dict[str, Any] = {"data_format": "netcdf"}
    for key, value in row.items():
        request[key] = value[:1] if isinstance(value, list) and value else value
    # A dataset with no variable dimension still needs the widget's "all".
    request.setdefault("variable", ["all"])
    target = Path(tempfile.mkdtemp()) / "probe.nc"
    cdsapi.Client().retrieve(dataset, request, str(target))
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as archive:
            members = [name for name in archive.namelist() if name.endswith(".nc")]
            if members:
                inner = target.parent / Path(members[0]).name
                with archive.open(members[0]) as src, inner.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                target = inner
    return _read_netcdf_var_meta(str(target))


def _ecmwf_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an ECMWF/CDS dataset by retrieving a tiny NetCDF (creds).

    Unlike the light constraints prober (variable *names* only), this
    actually retrieves a minimal slice via cdsapi to read each variable's
    real `long_name` / `units`. Needs `~/.cdsapirc`; the CDS queue can take
    minutes.
    """
    return _ecmwf_deep_sample(dataset)


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
        code = requests.head(url, timeout=_TIMEOUT, allow_redirects=True).status_code
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
            timeout=_TIMEOUT,
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


def _nwp_deep_probe(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
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


#: Provider id -> a credentialed deep sampler (the `--deep` half of probe).
_DEEP_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("deep_prober"),
    "ecmwf": _ecmwf_deep_probe,
    "nwp": _nwp_deep_probe,
}


#: Provider id -> a callable taking the loaded catalog and a dataset id and
#: returning its per-entry schema.
_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("prober"),
    "ecmwf": _ecmwf_probe,
    "chc": _chc_probe,
    "tropycal": _tropycal_probe,
    "nwp": _nwp_probe,
}


def supported_providers(deep: bool = False) -> list[str]:
    """Return the provider ids that have a curation prober wired up.

    Args:
        deep: When `True`, include providers that only have a credentialed
            `--deep` sampler (none currently — every deep provider also has
            a light prober).

    Returns:
        The sorted provider ids `probe` can sample.

    Examples:
        - STAC is wired up:

            ```python
            >>> from earthlens.cli.curate import supported_providers
            >>> "stac" in supported_providers()
            True

            ```
    """
    providers = set(_PROBERS)
    if deep:
        providers |= set(_DEEP_PROBERS)
    return sorted(providers)


def probe_dataset(info: BackendInfo, dataset: str, deep: bool = False) -> ProbeResult:
    """Probe one dataset's asset/band schema from a live sample record.

    With `deep=True` it uses the credentialed heavy sampler (real NetCDF /
    granule / CDS retrieval) when the provider has one, falling back to the
    light public prober otherwise. A provider with neither returns
    `"unsupported"`; any fetch / parse failure returns `"error"` — neither
    raises.

    Args:
        info: The backend the dataset belongs to.
        dataset: The dataset / collection id to probe.
        deep: Use the credentialed deep sampler when available.

    Returns:
        The :class:`ProbeResult`.
    """
    prober = (_DEEP_PROBERS.get(info.provider) if deep else None) or _PROBERS.get(
        info.provider
    )
    if prober is None:
        return ProbeResult(
            provider=info.provider,
            dataset=dataset,
            status="unsupported",
            detail="no sample endpoint wired up for probing",
        )
    try:
        catalog = load_catalog(info)
        assets = prober(catalog, dataset)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return ProbeResult(
            provider=info.provider, dataset=dataset, status="error", detail=str(exc)
        )
    return ProbeResult(
        provider=info.provider, dataset=dataset, status="ok", assets=assets
    )
