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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from earthlens._cli_tooling import dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog


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


#: ECCC models ship one whole GRIB per variable (no `.idx` byte-index), so the
#: idx-token check can't apply; template families whose URL also needs
#: domain / member / resolution aren't synthesised here either.
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


#: Provider id -> a credentialed deep sampler (the `--deep` half of probe).
_DEEP_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("deep_prober"),
    "ecmwf": _ecmwf_deep_probe,
}


#: Provider id -> a callable taking the loaded catalog and a dataset id and
#: returning its per-entry schema.
_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("prober"),
    "ecmwf": _ecmwf_probe,
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
