"""Catalog-tooling handlers for the ECMWF / Copernicus Data Store backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The refresher / writer /
coverage / prober / emitter read every public CADS store (CDS / ADS / EWDS /
ECDS / XDS); the deep prober, live-validator, hydrator and seeder are
credentialed (`~/.cdsapirc`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from earthlens.cli.toolkit import (
    COVERAGE_BUCKETS,
    get_json,
    index_writer,
)
from earthlens.ecmwf import _hydrate, _seed
from earthlens.ecmwf._categories import categorise_dataset  # noqa: F401 — role target
from earthlens.ecmwf._helpers import endpoint_for as _endpoint_for
from earthlens.ecmwf.endpoints import ENDPOINTS, endpoint_url

#: Cap on `/collections` pages followed via `rel="next"`.
_MAX_PAGES = 50


def _store_urls() -> dict[str, str]:
    """Every store's API root, by `endpoint` slug.

    Derived from the single `ENDPOINTS` registry rather than restated, so
    adding a store is one edit there. Resolved through `endpoint_url` on each
    call so a `<ENDPOINT>_URL` override reaches the catalog tooling exactly as
    it reaches the retrieve client — a frozen module-level dict would silently
    ignore it.

    Returns:
        dict[str, str]: Slug to API root for all five stores.
    """
    return {slug: endpoint_url(slug) for slug in ENDPOINTS}


def _store_collections_urls() -> dict[str, str]:
    """Every store's public STAC catalogue endpoint, by `endpoint` slug.

    Listing `/collections` needs no credentials (only data *retrieval* does).

    Returns:
        dict[str, str]: Slug to `/catalogue/v1/collections` URL.
    """
    return {
        slug: f"{root}/catalogue/v1/collections" for slug, root in _store_urls().items()
    }


#: Persist a live fetch back into the bundled `available_datasets` index.
writer = index_writer("available_datasets", grouped=True)


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List dataset ids per store (CDS / ADS / EWDS / ECDS / XDS), live (public).

    Enumerates each store's `/catalogue/v1/collections`, following `rel="next"`
    pagination (bounded by `_MAX_PAGES`). Each collection's `id` is a
    dataset name for that store. Listing needs no credentials.

    Args:
        _catalog: The loaded ECMWF `Catalog` (unused; the endpoints are fixed).

    Returns:
        A per-store mapping — one key per `ENDPOINTS` slug (`cds`, `ads`,
        `ewds`, `ecds`, `xds`) — of sorted, de-duplicated dataset ids.
    """
    grouped: dict[str, list[str]] = {}
    for store, base in _store_collections_urls().items():
        ids: set[str] = set()
        url: str | None = base
        pages = 0
        while url and pages < _MAX_PAGES:
            body = get_json(url)
            for collection in body.get("collections", []):
                cid = collection.get("id")
                if cid:
                    ids.add(str(cid))
            url = next(
                (
                    link.get("href")
                    for link in body.get("links", [])
                    if link.get("rel") == "next"
                ),
                None,
            )
            pages += 1
        grouped[store] = sorted(ids)
    return grouped


def coverage(catalog: Any) -> tuple[dict[str, int], list[str]]:
    """Classify every `available_datasets:` id across all five stores.

    The per-store availability index (CDS / ADS / EWDS / ECDS / XDS, written by
    `refresh ecmwf --write`) is unioned into `catalog.available_datasets`. A
    dataset with a curated row is `DONE`; every other id is `addressable`
    (reachable now via the raw-request passthrough, curatable on demand).

    Args:
        catalog: The loaded ECMWF `Catalog`.

    Returns:
        `(counts, todo)` — per-bucket counts and the sorted uncurated ids.

    Raises:
        ValueError: If the `available_datasets:` index is empty.
    """
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    if not available:
        raise ValueError(
            "available_datasets: is empty — run `refresh ecmwf --write` first"
        )
    curated = set(catalog.datasets)
    buckets: dict[str, list[str]] = {}
    for dataset_id in available:
        bucket = "DONE" if dataset_id in curated else "addressable"
        buckets.setdefault(bucket, []).append(dataset_id)
    counts = {bucket: len(buckets.get(bucket, [])) for bucket in COVERAGE_BUCKETS}
    return counts, sorted(buckets.get("addressable", []))


def _ecmwf_constraints(dataset: str) -> list[dict[str, Any]]:
    """Return a dataset's public `constraints.json` rows (no creds).

    Resolves the dataset's CADS endpoint from the catalog so EWDS/ADS datasets
    are fetched from their own catalogue host rather than the CDS host (which
    would 404 and silently return no rows).
    """
    from earthlens.ecmwf.constraints import fetch_constraints
    from earthlens.ecmwf.endpoints import constraints_base_url

    return fetch_constraints(dataset, constraints_base_url(_endpoint_for(dataset)))


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
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


def _from_info(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract per-band `NETCDF_VARNAME` / long_name / units from `gdal.Info`.

    Args:
        info: A `gdal.Info(..., format="json")` mapping for one NetCDF handle.

    Returns:
        A `{variable_name: {"long_name": ..., "units": ...}}` mapping covering
        every band that carries a `long_name` or `units` attribute.
    """
    out: dict[str, dict[str, Any]] = {}
    for band in info.get("bands", []) or []:
        meta = band.get("metadata", {}).get("", {})
        name = meta.get("NETCDF_VARNAME")
        long_name, units = meta.get("long_name", ""), meta.get("units", "")
        if name and (long_name or units):
            out[str(name)] = {"long_name": long_name, "units": units}
    return out


def _variable_meta(variable: Any) -> dict[str, Any] | None:
    """Pull `long_name` / `units` off one pyramids variable, whatever its shape.

    A NetCDF variable reaches us as one of two objects, because not every
    variable can be presented as a raster: a gridded one arrives as a pyramids
    `Variable` carrying `band_units` and `global_attributes`, while a variable
    with no x/y dimensions — an observation table's columns — arrives as the
    underlying GDAL `MDArray`, whose unit and attributes are read through its
    own accessors.

    Args:
        variable: The object `NetCDF.get_variable` returned.

    Returns:
        A `{"long_name": ..., "units": ...}` mapping, or None when the variable
        carries neither.
    """
    units = long_name = ""
    if hasattr(variable, "band_units"):
        units = next(iter(variable.band_units or []), "") or ""
        long_name = (getattr(variable, "global_attributes", None) or {}).get(
            "long_name", ""
        )
    elif hasattr(variable, "GetUnit"):
        units = variable.GetUnit() or ""
        attributes = {
            attribute.GetName(): attribute.ReadAsString()
            for attribute in (variable.GetAttributes() or [])
        }
        units = units or attributes.get("units", "")
        long_name = attributes.get("long_name", "")
    if not (units or long_name):
        return None
    return {"long_name": long_name, "units": units}


def _exposed_variable(container: Any, name: str) -> Any | None:
    """Return one variable of `container`, or None when it cannot be exposed.

    Not every variable in a NetCDF file can be presented as an array: a string
    column — an observation table's variable-name or units column — raises
    rather than returning. That is a property of the one variable, so it is
    reported as None and the rest of the file is still read.

    Args:
        container: The pyramids NetCDF container.
        name: The variable to fetch.

    Returns:
        The variable, or None when the reader cannot expose it.
    """
    try:
        return container.get_variable(name)
    except (RuntimeError, ValueError, TypeError):
        return None


def _read_via_pyramids(path: str) -> dict[str, dict[str, Any]]:
    """Read each variable's `long_name` / `units` through pyramids.

    pyramids is this repository's GIS backend and owns NetCDF reading, so the
    metadata is taken from it rather than from a hand-rolled GDAL walk. It also
    reaches variables the classic raster API cannot: that API only surfaces what
    it can present as a raster band, so a file whose variables are not
    raster-shaped reads as empty through it and completely through this.

    A variable whose data type cannot be exposed at all — a string column — is
    skipped rather than failing the file, because the rest of the file is still
    worth reading.

    Args:
        path: Path to a NetCDF file written by a `cdsapi` retrieve.

    Returns:
        A `{variable_name: {"long_name": ..., "units": ...}}` mapping for every
        variable that carries a `long_name` or `units`.
    """
    from pyramids.netcdf import NetCDF

    container = NetCDF.read_file(path)
    schema: dict[str, dict[str, Any]] = {}
    for name in container.variable_names or []:
        variable = _exposed_variable(container, name)
        meta = _variable_meta(variable) if variable is not None else None
        if meta is not None:
            schema[str(name)] = meta
    return schema


def _read_netcdf_var_meta(path: str) -> dict[str, dict[str, Any]]:
    """Read each NetCDF variable's `long_name` / `units`.

    Goes through pyramids first, and falls back to the classic GDAL raster walk
    when that yields nothing. The fallback is kept because the classic API is
    what every hydrated row in the catalog was read with: a file it can still
    describe must keep reading the same way, whatever the newer path makes of
    it.

    Args:
        path: Path to a NetCDF file written by a `cdsapi` retrieve.

    Returns:
        A `{variable_name: {"long_name": ..., "units": ...}}` mapping for every
        variable that carries a `long_name` or `units` attribute.
    """
    try:
        schema = _read_via_pyramids(path)
    except Exception:  # noqa: BLE001 — an unreadable container falls back
        schema = {}
    if schema:
        return schema

    from osgeo import gdal

    gdal.UseExceptions()

    top = gdal.Info(path, format="json")
    subs = top.get("metadata", {}).get("SUBDATASETS", {})
    if not subs:
        return _from_info(top)
    fallback: dict[str, dict[str, Any]] = {}
    for key, sub_path in subs.items():
        if key.endswith("_NAME"):
            fallback.update(_from_info(gdal.Info(sub_path, format="json")))
    return fallback


def _deep_sample_row(
    rows: list[dict[str, Any]], cds_variable: str | None = None
) -> dict[str, Any] | None:
    """Return the constraints entry to build a probe request from.

    Constraints partition a dataset into combination blocks, and a variable is
    only retrievable under the selectors of a block that lists it — GloFAS
    serves discharge and runoff under `timespan: time_mean` but snow depth and
    soil wetness only under `instantaneous`. Choosing the block by the variable
    is therefore what makes a per-variable probe a valid request rather than a
    400, and it is also where that variable's required selectors come from.

    Args:
        rows: The dataset's `constraints.json` entries.
        cds_variable: The variable the probe is for; `None` keeps the legacy
            behaviour of taking the first entry that enumerates any variable.

    Returns:
        The chosen entry, or `None` when `rows` is empty or no entry serves
        `cds_variable`.
    """
    if not rows:
        return None
    if cds_variable is None:
        return next((entry for entry in rows if entry.get("variable")), rows[0])
    return next(
        (entry for entry in rows if cds_variable in (entry.get("variable") or [])),
        None,
    )


def _deep_sample_request(
    row: dict[str, Any], cds_variable: str | None = None
) -> dict[str, Any]:
    """Build the smallest valid retrieve request from one constraints entry.

    One value per selector, so the family selectors a dataset requires beyond
    year/month/day (a satellite CDR's sensor / version / record-type, CMIP's
    experiment/model, ...) are carried and the retrieve is a valid combination.
    Only keys the entry actually enumerates are sent, so a product that does not
    partition by day/time is not handed a spurious one.

    Args:
        row: The constraints entry to reduce.
        cds_variable: When given, the request asks for exactly this variable
            instead of the entry's first.

    Returns:
        A `cdsapi` request mapping.
    """
    request: dict[str, Any] = {"data_format": "netcdf"}
    for key, value in row.items():
        request[key] = value[:1] if isinstance(value, list) and value else value
    if cds_variable is not None:
        request["variable"] = [cds_variable]
    # A dataset with no variable dimension still needs the widget's "all".
    request.setdefault("variable", ["all"])
    return request


def _retrieve_probe(dataset: str, request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Retrieve one tiny slice and read each NetCDF variable's metadata.

    The retrieve goes to the dataset's **own** store, resolved from the catalog
    the same way :func:`_ecmwf_constraints` resolves it: a bare client would
    always talk to CDS and every ADS / EWDS / ECDS / XDS dataset would 404 with
    `process not found`. A zip-of-NetCDF response (satellite CDRs deliver one)
    is unwrapped to its first member before the metadata is read via GDAL.

    Writes under `EARTHLENS_CACHE_DIR` when that is set, and removes the scratch
    directory once the metadata has been read.

    Retries a throttled store the same way a download does. A sweep issues one
    of these per placeholder row, so it is the caller most likely to meet the
    per-dataset queue limit — and without the retry a store that accepts a job
    and then rejects it ends the whole pass on the first refusal.

    Args:
        dataset: The Copernicus dataset id to retrieve from.
        request: The request mapping to submit.

    Returns:
        Mapping of NetCDF short name to `{long_name, units}`.
    """
    import os
    import shutil
    import tempfile
    import zipfile

    from earthlens.ecmwf._helpers import _retrieve_with_retry
    from earthlens.ecmwf.endpoints import open_client

    # A probe is a minimal slice, but "minimal" is the store's judgement, not
    # ours: one CAMS inventory answers a single-variable request with a 425 MB
    # zip. A sweep issues one of these per placeholder row, so they are written
    # under EARTHLENS_CACHE_DIR when set - a data volume, not the system disk -
    # and through TemporaryDirectory so each is removed once it has been read
    # rather than accumulating until something runs out of space.
    scratch_root = os.environ.get("EARTHLENS_CACHE_DIR") or None
    with tempfile.TemporaryDirectory(dir=scratch_root) as scratch:
        target = Path(scratch) / "probe.nc"
        endpoint = _endpoint_for(dataset)
        client = open_client(endpoint)
        _retrieve_with_retry(client, dataset, request, target, endpoint)
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as archive:
                members = [name for name in archive.namelist() if name.endswith(".nc")]
                if members:
                    inner = target.parent / Path(members[0]).name
                    with archive.open(members[0]) as src, inner.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    target = inner
        return _read_netcdf_var_meta(str(target))


def _ecmwf_deep_sample(dataset: str) -> dict[str, dict[str, Any]]:
    """Retrieve a tiny CDS NetCDF and read each variable's long_name/units.

    Probes the dataset's first usable constraints entry, asking for one value
    per selector. What comes back describes whichever variable that entry lists
    first, so this cannot finish a multi-variable dataset on its own — see
    :func:`_ecmwf_deep_sample_variable` for the per-variable form the catalog
    hydrator uses.

    Args:
        dataset: The Copernicus dataset id to sample.

    Returns:
        Mapping of NetCDF short name to `{long_name, units}`; empty when the
        dataset publishes no constraints.
    """
    row = _deep_sample_row(_ecmwf_constraints(dataset))
    if row is None:
        return {}
    return _retrieve_probe(dataset, _deep_sample_request(row))


def _required_selectors(
    rows: list[dict[str, Any]], cds_variable: str
) -> dict[str, Any]:
    """Return the selectors a variable is only ever served under.

    A probe request is one sampled combination, not a requirement: it takes the
    first entry that lists the variable and keeps one value per selector. Most
    of what it carries is a free choice the caller makes — a CMIP variable is
    served under every model, experiment and period the dataset offers, and
    pinning the sampled one into the row would override whatever the caller
    later asks for.

    A selector is a requirement only when every entry serving the variable
    agrees on it. That isolates the real constraint — GloFAS serves snow depth
    solely under `timespan: instantaneous` — and says nothing about the
    selectors the caller is free to vary.

    Args:
        rows: The dataset's `constraints.json` entries.
        cds_variable: The variable whose requirements to derive.

    Returns:
        The selectors common to every entry serving `cds_variable`; empty when
        nothing is required or no entry serves it.
    """
    serving = [entry for entry in rows if cds_variable in (entry.get("variable") or [])]
    if not serving:
        return {}
    first = serving[0]
    return {
        key: value
        for key, value in first.items()
        if key != "variable"
        and isinstance(value, list)
        and all(entry.get(key) == value for entry in serving)
    }


def _ecmwf_deep_sample_variable(
    dataset: str, cds_variable: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Probe ONE variable and report the selectors its constraints block needs.

    Asking for a single named variable is what lets the hydrator finish a
    multi-variable dataset, and the block that serves it also carries the
    selectors that variable requires — the GloFAS `timespan` split is exactly
    this. Returning both together means a per-variable `extras:` override can be
    derived rather than hand-written.

    Args:
        dataset: The Copernicus dataset id to sample.
        cds_variable: The `cds_variable` to request.

    Returns:
        A `(metadata, selectors)` pair. `metadata` maps NetCDF short name to
        `{long_name, units}`; `selectors` is what the variable is *only ever*
        served under (see :func:`_required_selectors`), not the combination this
        one probe happened to sample. Both are empty when no constraints block
        serves `cds_variable`.
    """
    rows = _ecmwf_constraints(dataset)
    row = _deep_sample_row(rows, cds_variable)
    if row is None:
        return {}, {}
    request = _deep_sample_request(row, cds_variable)
    return _retrieve_probe(dataset, request), _required_selectors(rows, cds_variable)


def deep_prober(_catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe an ECMWF/CDS dataset by retrieving a tiny NetCDF (creds).

    Unlike the light constraints prober (variable *names* only), this
    actually retrieves a minimal slice via cdsapi to read each variable's
    real `long_name` / `units`. Needs `~/.cdsapirc`; the CDS queue can take
    minutes.
    """
    return _ecmwf_deep_sample(dataset)


def _ecmwf_token() -> str | None:
    """Return the shared CDS Personal Access Token from `~/.cdsapirc`, if any."""
    rc = Path.home() / ".cdsapirc"
    if not rc.is_file():
        return None
    for line in rc.read_text().splitlines():
        if line.strip().startswith("key"):
            return line.partition(":")[2].strip() or None
    return None


def _ecmwf_endpoint_for(catalog: Any, dataset_id: str) -> str:
    """Resolve which store hosts `dataset_id` (per-store index, else prefix)."""
    store = getattr(catalog, "store_for", lambda _id: None)(dataset_id)
    if store:
        return str(store)
    if dataset_id.startswith("cams-"):
        return "ads"
    if dataset_id.startswith(("cems-", "efas-")):
        return "ewds"
    return "cds"


def _collect_variable_values(node: Any, out: list[str]) -> None:
    """Recurse a `variable` widget's (possibly grouped) values into `out`."""
    if isinstance(node, dict):
        for value in node.get("values", []) or []:
            if isinstance(value, str):
                out.append(value)
        for group in node.get("groups", []) or []:
            _collect_variable_values(group, out)


def _ecmwf_form_variables(form: list[Any]) -> list[str]:
    """Enumerate the `variable` widget's allowed values from a `form.json`."""
    out: list[str] = []
    for entry in form:
        if isinstance(entry, dict) and entry.get("name") == "variable":
            _collect_variable_values(entry.get("details", {}) or {}, out)
    return out


def _hindcast_request_kind(fields: set[str | None]) -> str:
    """Pick the hindcast kind for a form that carries `hyear`.

    Three shapes exist. A form with `hyear` but no `hday` is a seasonal
    reforecast (year/month only). A form with `hday` *and* the plain `day` axis
    carries **both** dates — the model cycle and the reforecast — which are
    paired, so it needs `s2s_reforecast` (which copies month/day across);
    `glofas_hindcast` would rename `year` to `hyear` and delete the model-cycle
    date. A form with `hday` and no `day` is the GloFAS/EFAS shape, where the
    hindcast date is the only one.

    Args:
        fields: The `name` of every widget in the dataset's `form.json`. The
            caller has already established that `hyear` is among them; a form
            without it is not a hindcast and never reaches here.

    Returns:
        str: One of `seasonal_hindcast`, `s2s_reforecast`, `glofas_hindcast`.
    """
    if "hday" not in fields:
        return "seasonal_hindcast"
    return "s2s_reforecast" if "day" in fields else "glofas_hindcast"


def _ecmwf_request_kind(form: list[Any], upstream_id: str = "") -> str:
    """Guess the `request_kind` from a dataset id + its `form.json` fields.

    The ESA-CCI satellite CDRs are all `satellite-*` ids and their forms carry
    no `grid` widget, so the id is the reliable signal for `satellite_cdr`;
    every other kind is inferred from the form's date / selector fields.
    """
    if upstream_id.startswith("satellite-"):
        return "satellite_cdr"
    if upstream_id.startswith("cems-fire"):
        # Both fire-danger datasets are `cems-fire-*`; the historical form has a
        # grid but the seasonal form keys on `leadtime_hour`, so the field
        # heuristic alone would seed seasonal fire as `form` — key on the id.
        return "fire"
    fields = {f.get("name") for f in form if isinstance(f, dict)}
    if "hyear" in fields:
        return _hindcast_request_kind(fields)
    # A real seasonal forecast keys on `leadtime_month`. Without it, a
    # year/month-only form is a monthly reanalysis / emission inventory /
    # radiative-forcing product, not a seasonal forecast — so require the lead
    # field rather than firing on `year` + no `day` (which over-caught ~15
    # non-seasonal families).
    if "leadtime_month" in fields:
        return "seasonal"
    if "date" in fields:
        return "cams_date"
    # `cams_inversion` (CAMS GHG inversion / EU air-quality reanalyses) is
    # CAMS-only: a `quantity` widget, or a `cams-*` id carrying a model/level
    # selector with no `day`. A `model` widget on a `projections-*` id is a
    # climate model, not a CAMS inversion — do not misread it.
    if "quantity" in fields or (
        upstream_id.startswith("cams-") and "model" in fields and "day" not in fields
    ):
        return "cams_inversion"
    if "grid" in fields and "leadtime_hour" not in fields:
        # CEMS fire-danger forms carry a `dataset_type` selector alongside the
        # grid; the satellite CDRs are matched by id above.
        return "fire" if "dataset_type" in fields else "satellite_cdr"
    return "form"


def emitter(catalog: Any, upstream_id: str, **_opts: Any) -> dict[str, Any]:
    """Seed an ECMWF `datasets:` row from the live CADS `form.json`.

    Resolves the dataset's store from the per-store index, fetches its
    `form.json`, guesses the `request_kind` from the date/selector fields, and
    enumerates every variable the `variable` widget exposes.
    `nc_variable` / `units` are placeholders (the form does not carry them) —
    confirm them from a live retrieve (`curate ecmwf --fill-empty`).

    Args:
        catalog: The loaded ECMWF `Catalog` (its per-store index resolves the
            endpoint).
        upstream_id: The Copernicus dataset id.
        **opts: Unused.

    Returns:
        The seeded row (`endpoint` / `request_kind` / `variables`).
    """
    endpoint = _ecmwf_endpoint_for(catalog, upstream_id)
    token = _ecmwf_token()
    headers = {"PRIVATE-TOKEN": token} if token else None
    url = f"{_store_urls()[endpoint]}/catalogue/v1/collections/{upstream_id}/form.json"
    raw: Any = get_json(url, headers=headers)
    # CADS form.json is usually {"form": [...]} but is sometimes the bare
    # [...] list; `raw` is Any so both shapes narrow without a mypy conflict.
    fields = raw.get("form") or [] if isinstance(raw, dict) else raw
    variables = _ecmwf_form_variables(cast("list[Any]", fields)) or ["all"]
    return {
        "endpoint": endpoint,
        "request_kind": _ecmwf_request_kind(cast("list[Any]", fields), upstream_id),
        "variables": {
            v.replace("_", "-"): {
                "cds_variable": v,
                "nc_variable": v,
                "units": "unknown",
            }
            for v in variables
        },
    }


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each ECMWF dataset can build a constraint-valid minimal request.

    Folds the local gate of the retired `tools/ecmwf/probe_open_datasets.py`:
    for every curated dataset, build a minimal request from its public
    `constraints.json` and run the same `RequestValidator` the backend uses
    before a retrieve. Datasets that publish no constraints (so no request can
    be built) are skipped, not flagged. Stateless — no CDS credentials or
    queue submission (per-dataset live retrieval stays `probe ecmwf --deep`).
    """
    from earthlens.ecmwf.constraints import RequestValidator

    issues: list[str] = []
    checked = 0
    for key in catalog.datasets:
        try:
            request = catalog.minimal_valid_request(key)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: constraints fetch failed ({exc})")
            continue
        if set(request) <= {"data_format"}:
            continue  # no published constraints -> nothing to validate
        checked += 1
        try:
            RequestValidator(key, request).check()
        except ValueError as exc:
            issues.append(f"{key}: {str(exc).splitlines()[0][:90]}")
    return checked, issues


def hydrator(
    *, limit: int | None = None, timeout: float | None = None
) -> dict[str, Any]:
    """Bulk-hydrate placeholder ECMWF rows in place (`curate ecmwf --fill-empty`).

    Args:
        limit: Only hydrate the first N placeholder rows (None = all).
        timeout: Per-dataset retrieve deadline in seconds (None = the module
            default).

    Returns:
        The `{candidates, hydrated, skipped, timed_out}` summary.
    """
    return _hydrate.bulk_hydrate_empty(limit=limit, timeout=timeout)


def categoriser(upstream_id: str, title: str = "") -> str:
    """Auto-pick the per-family shard stem for an ECMWF dataset id.

    Args:
        upstream_id: The Copernicus dataset id.
        title: Unused (accepted for the uniform categoriser role signature).

    Returns:
        The per-family shard file stem under the sharded `catalog/` directory.
    """
    del title
    return categorise_dataset(upstream_id)


def seeder(*, limit: int | None = None) -> dict[str, Any]:
    """Bulk-seed every uncurated ECMWF dataset into its shard (`curate ecmwf --all`).

    Args:
        limit: Only seed the first N uncurated datasets (None = all).

    Returns:
        The `{candidates, seeded, skipped}` summary.
    """
    return _seed.bulk_seed_uncurated(limit=limit)
