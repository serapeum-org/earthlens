"""Per-entry catalog validation — the verb for curated-enumeration providers.

Several providers (`nwp`, `s3`, `firms`, `radar`, `tropycal`, …) are
hand-curated selections with no discoverable upstream index, so the
index-diff `refresh` / `audit` verbs do not apply. What *does* apply is
checking each curated entry individually: is it internally coherent
(offline structural lint), or does it still resolve upstream (liveness)?
That is what `validate` does. This is the CLI home for the
`tools/*/audit_*` checks that are per-entry rather than index-diff.

Each provider plugs a validator into :data:`_VALIDATORS` returning
`(checked, issues)`; providers without one report `"unsupported"`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _TIMEOUT, _get_json


@dataclass
class ValidateResult:
    """The result of validating one provider's curated entries.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no validator), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        checked: Number of curated entries inspected.
        issues: One message per entry that failed validation (empty = clean).
    """

    provider: str
    status: str
    detail: str = ""
    checked: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A clean provider has an empty `issues` list:

                ```python
                >>> from earthlens.cli.validate import ValidateResult
                >>> ValidateResult("nwp", "ok", checked=32).to_dict()["issues"]
                []

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "checked": self.checked,
            "issues": self.issues,
        }


def _validate_nwp(catalog: Any) -> tuple[int, list[str]]:
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
        if backend in _DIRECT_URL_BACKENDS and not getattr(record, "url_template", None):
            issues.append(f"{key}: {backend} model has no url_template")
        if backend == "herbie" and not getattr(record, "model_family", None):
            issues.append(f"{key}: herbie model has no model_family")
        if not (getattr(record, "bands", None) or {}):
            issues.append(f"{key}: empty band map")
        for hour in getattr(record, "cycles_utc", None) or []:
            if isinstance(hour, int) and not 0 <= hour <= 23:
                issues.append(f"{key}: cycle hour {hour} out of range")
    return len(models), issues


def _lint(
    catalog: Any, check: Callable[[str, Any], list[str]]
) -> tuple[int, list[str]]:
    """Run a per-record `check(key, record) -> [issue, …]` over a catalog."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        issues.extend(check(key, record))
    return len(catalog.datasets), issues


def _require(key: str, record: Any, fields: tuple[str, ...]) -> list[str]:
    """Return an issue per `field` that is empty/None on `record`."""
    return [
        f"{key}: missing {field}"
        for field in fields
        if not getattr(record, field, None)
    ]


def _validate_s3(catalog: Any) -> tuple[int, list[str]]:
    """Each S3 dataset needs a bucket and a format."""
    return _lint(catalog, lambda k, r: _require(k, r, ("bucket", "format")))


def _validate_ghsl(catalog: Any) -> tuple[int, list[str]]:
    """Each GHSL product needs a code and at least one release.

    `family` is a soft grouping that is legitimately empty for top-level
    products (e.g. GHS_POP is its own family), so it is not required.
    """
    return _lint(catalog, lambda k, r: _require(k, r, ("code", "releases")))


def _validate_overture(catalog: Any) -> tuple[int, list[str]]:
    """Each Overture theme needs types and a default_type drawn from them."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a theme missing types/default_type or whose default is unlisted."""
        issues = _require(key, record, ("types", "default_type"))
        types = getattr(record, "types", None) or []
        default = getattr(record, "default_type", None)
        if default and types and default not in types:
            issues.append(f"{key}: default_type {default!r} not in types")
        return issues

    return _lint(catalog, check)


def _validate_fdsn(catalog: Any) -> tuple[int, list[str]]:
    """Each FDSN network needs an fdsn_id."""
    return _lint(catalog, lambda k, r: _require(k, r, ("fdsn_id",)))


def _validate_firms(catalog: Any) -> tuple[int, list[str]]:
    """Each FIRMS sensor needs a code and a non-empty columns map."""
    return _lint(catalog, lambda k, r: _require(k, r, ("code", "columns")))


def _validate_radar(catalog: Any) -> tuple[int, list[str]]:
    """Each radar station needs a name and in-range latitude / longitude."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a station missing a name or with out-of-range coordinates."""
        issues = _require(key, record, ("name",))
        lat = getattr(record, "latitude", None)
        lon = getattr(record, "longitude", None)
        if not (isinstance(lat, (int, float)) and -90 <= lat <= 90):
            issues.append(f"{key}: latitude {lat!r} out of range")
        if not (isinstance(lon, (int, float)) and -180 <= lon <= 180):
            issues.append(f"{key}: longitude {lon!r} out of range")
        return issues

    return _lint(catalog, check)


#: tropycal's basin universe and which sources serve each (no `jtwc` source;
#: `both` is HURDAT NA+EP, `all` is IBTrACS global). Ported from the retired
#: `tools/tropycal/audit_tropycal_catalog.py`.
_SDK_BASIN_SOURCES: dict[str, list[str]] = {
    "north_atlantic": ["ibtracs", "hurdat"],
    "east_pacific": ["ibtracs", "hurdat"],
    "both": ["hurdat"],
    "west_pacific": ["ibtracs"],
    "north_indian": ["ibtracs"],
    "south_indian": ["ibtracs"],
    "australia": ["ibtracs"],
    "south_pacific": ["ibtracs"],
    "south_atlantic": ["ibtracs"],
    "all": ["ibtracs"],
}


def _validate_tropycal(catalog: Any) -> tuple[int, list[str]]:
    """Each Tropycal basin needs a source, and must match the SDK universe.

    Beyond the per-row `sources` requirement, this diffs the catalog against
    tropycal's supported basin/source universe (the offline check the retired
    `audit_tropycal_catalog.py` ran): a curated basin tropycal no longer
    serves, a tropycal basin missing from the catalog, or a declared
    `(basin, source)` pair tropycal does not support.

    Args:
        catalog: The loaded Tropycal `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    checked, issues = _lint(catalog, lambda k, r: _require(k, r, ("sources",)))
    catalog_basins = set(catalog.datasets)
    sdk_basins = set(_SDK_BASIN_SOURCES)
    issues += [
        f"{code}: basin not in tropycal's supported universe"
        for code in sorted(catalog_basins - sdk_basins)
    ]
    issues += [
        f"{code}: tropycal basin missing from the catalog"
        for code in sorted(sdk_basins - catalog_basins)
    ]
    for code in sorted(catalog_basins & sdk_basins):
        declared = set(getattr(catalog.datasets[code], "sources", None) or [])
        for bad in sorted(declared - set(_SDK_BASIN_SOURCES[code])):
            issues.append(f"{code}: source {bad!r} not supported by tropycal")
    return checked, issues


def _validate_gdacs(catalog: Any) -> tuple[int, list[str]]:
    """Each GDACS hazard type needs a name and a description."""
    return _lint(catalog, lambda k, r: _require(k, r, ("name", "description")))


def _validate_chc(catalog: Any) -> tuple[int, list[str]]:
    """Each CHC dataset needs FTP bases, a file pattern, and variables."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a dataset missing ftp_bases, variables, or a file pattern."""
        issues = _require(key, record, ("ftp_bases", "variables"))
        if not (
            getattr(record, "file_patterns", None)
            or getattr(record, "discrete_files", None)
        ):
            issues.append(f"{key}: no file_patterns or discrete_files")
        return issues

    return _lint(catalog, check)


def _validate_usgs_water(catalog: Any) -> tuple[int, list[str]]:
    """Each USGS Water parameter's `services` must be known service names.

    Mirrors `tools/usgs_water/refresh_usgs_catalog.py:validate`: every
    declared service must be in `earthlens.usgs_water.backend.SERVICES`.

    Args:
        catalog: The loaded USGS Water `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.usgs_water.backend import SERVICES

    def check(key: str, record: Any) -> list[str]:
        """Flag a parameter declaring a service that is not a known service name."""
        return [
            f"{key}: unknown service {service!r}"
            for service in getattr(record, "services", None) or []
            if service not in SERVICES
        ]

    return _lint(catalog, check)


def _validate_sentinel_hub(catalog: Any) -> tuple[int, list[str]]:
    """Each Sentinel Hub recipe's evalscript `.js` must be well-formed.

    Mirrors `tools/sentinel_hub/refresh_sh_catalog.py:validate-recipe`
    (offline): the bundled `.js` must exist, start with `//VERSION=3`, and
    a `"stats"` recipe must declare a `dataMask` band.

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (exposes `recipes`).

    Returns:
        `(checked, issues)` — the recipe count and one message per problem.
    """
    from earthlens.sentinel_hub import read_evalscript

    recipes = getattr(catalog, "recipes", None) or {}
    issues: list[str] = []
    for key, recipe in recipes.items():
        script_name = getattr(recipe, "evalscript", None)
        if not script_name:
            continue
        try:
            script = read_evalscript(script_name)
        except FileNotFoundError as exc:
            issues.append(f"{key}: {exc}")
            continue
        if script.splitlines()[0].strip() != "//VERSION=3":
            issues.append(f"{key}: {script_name} does not start with //VERSION=3")
        if getattr(recipe, "kind", None) == "stats" and "dataMask" not in script:
            issues.append(f"{key}: {script_name} stats recipe has no dataMask band")
    return len(recipes), issues


def _validate_worldpop(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated WorldPop products.

    Reuses the backend's own `Catalog.health()` (mirrors
    `tools/worldpop/refresh_worldpop_catalog.py:validate_structure`):
    flags products with no sub-aliases, `demographic` products whose `kind`
    is not `"mixed"`, and sub-aliases with an unknown generation or an
    unparseable years spec.

    Args:
        catalog: The loaded WorldPop `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per offender.
    """
    issues = [
        f"{offender}: {check}"
        for check, offenders in catalog.health().items()
        for offender in offenders
    ]
    return len(catalog.datasets), issues


def _validate_nwm(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated NWM products and configurations.

    Mirrors the offline half of the retired `tools/nwm/audit_nwm_catalog.py`:
    every product needs an `s3_token` and a non-empty `variables` map, and
    every configuration's `products` must reference a curated product key.

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    products = catalog.datasets
    issues: list[str] = []
    for key, product in products.items():
        issues.extend(_require(key, product, ("s3_token", "variables")))
    for cfg_key, config in catalog.configurations.items():
        for product_key in getattr(config, "products", None) or []:
            if product_key not in products:
                issues.append(f"{cfg_key}: references unknown product {product_key!r}")
    return len(products), issues


#: Provider id -> a callable taking the loaded catalog and returning
#: `(checked, issues)`. Providers without one report `"unsupported"`.
_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "nwp": _validate_nwp,
    "nwm": _validate_nwm,
    "s3": _validate_s3,
    "ghsl": _validate_ghsl,
    "overture": _validate_overture,
    "fdsn": _validate_fdsn,
    "firms": _validate_firms,
    "radar": _validate_radar,
    "tropycal": _validate_tropycal,
    "gdacs": _validate_gdacs,
    "chc": _validate_chc,
    "usgs_water": _validate_usgs_water,
    "sentinel_hub": _validate_sentinel_hub,
    "worldpop": _validate_worldpop,
}


# --------------------------------------------------------------------------- #
# Live reachability validators (the `--live` half) — confirm a curated entry
# still resolves upstream. A superset of the offline lint; opt-in because it
# goes to the network / SDK. Each live source sits behind a mockable helper.
# --------------------------------------------------------------------------- #
def _s3_live_keys(bucket: str, prefix: str, region: str | None) -> list[str]:
    """Return one object key under `prefix` (unsigned `boto3`)."""
    from earthlens.s3.auth import S3Auth, S3Credentials

    client = S3Auth(S3Credentials(region=region)).client()
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return [item["Key"] for item in response.get("Contents", [])]


def _live_s3(catalog: Any) -> tuple[int, list[str]]:
    """Confirm every S3 dataset's bucket still serves an object (unsigned)."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        try:
            keys = _s3_live_keys(
                record.bucket,
                getattr(record, "prefix", "") or "",
                getattr(record, "region", None),
            )
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: bucket error ({exc})")
            continue
        if not keys:
            issues.append(f"{key}: no objects under s3://{record.bucket}")
    return len(catalog.datasets), issues


#: A tiny bbox (Times Square block) for the live Overture fetch.
_OVERTURE_BBOX = (-73.9876, 40.7561, -73.9851, 40.7577)


def _overture_live_sample(overture_type: str) -> tuple[int, bool]:
    """Fetch a tiny bbox; return `(row_count, has_sources_column)`."""
    from overturemaps.core import geodataframe

    frame = geodataframe(overture_type, bbox=_OVERTURE_BBOX)
    return len(frame), "sources" in frame.columns


def _live_overture(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each Overture type resolves live and carries a `sources` column."""
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        overture_type = getattr(record, "default_type", None) or key
        try:
            _rows, has_sources = _overture_live_sample(overture_type)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}/{overture_type}: fetch failed ({exc})")
            continue
        if not has_sources:
            issues.append(f"{key}/{overture_type}: no 'sources' column")
    return len(catalog.datasets), issues


def _http_head(url: str) -> int:
    """Return the HTTP status of a HEAD request (following redirects)."""
    return requests.head(url, timeout=_TIMEOUT, allow_redirects=True).status_code


def _live_ghsl(catalog: Any) -> tuple[int, list[str]]:
    """HEAD one whole-globe artefact per GHSL product/release.

    Skips releases whose every resolution ships only as tiles (real-tile
    sampling stays in `tools/ghsl/refresh_ghsl_catalog.py`).
    """
    from earthlens.ghsl._helpers import RES_TO_TOKEN, ghsl_url

    issues: list[str] = []
    for code, product in catalog.datasets.items():
        # Tabular products (resolution "table") have no raster artefact URL —
        # their live check is the maintainer table-zip path in tools/ghsl.
        if getattr(product, "kind", "raster") == "tabular":
            continue
        for release, blocks in (getattr(product, "releases", None) or {}).items():
            block = blocks[0]
            whole_globe = [
                r
                for r in block.resolutions
                if r not in block.tiled() and r in RES_TO_TOKEN
            ]
            if not whole_globe:
                continue
            try:
                url = ghsl_url(
                    product.family or code,
                    code,
                    block.epochs[0],
                    release,
                    whole_globe[0],
                    version=block.version,
                    region=block.region,
                    nested=block.nested,
                )
                status = _http_head(url)
            except Exception as exc:  # noqa: BLE001 — reported as drift
                issues.append(f"{code} ({release}): {exc}")
                continue
            if status != 200:
                issues.append(f"{code} ({release}): HTTP {status} for {url}")
    return len(catalog.datasets), issues


#: CDSE openEO processes endpoint (public; pairs with the collections one).
_OPENEO_PROCESSES_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/processes"


def _openeo_live_lists() -> tuple[set[str], set[str]]:
    """Return the live `(collection_ids, process_ids)` sets (public CDSE)."""
    from earthlens.cli.refresh import _OPENEO_COLLECTIONS_URL

    collections = {
        c["id"]
        for c in _get_json(_OPENEO_COLLECTIONS_URL).get("collections", [])
        if c.get("id")
    }
    processes = {
        p["id"]
        for p in _get_json(_OPENEO_PROCESSES_URL).get("processes", [])
        if p.get("id")
    }
    return collections, processes


def _live_openeo(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each openEO recipe's base collection + processes exist live."""
    recipes = getattr(catalog, "recipes", None) or {}
    if not recipes:
        return 0, []
    collections, processes = _openeo_live_lists()
    issues: list[str] = []
    for key, recipe in recipes.items():
        base = getattr(recipe, "base_collection", None)
        if base and base not in collections:
            issues.append(f"{key}: base_collection {base!r} not served live")
        for process in getattr(recipe, "processes", None) or []:
            if process not in processes:
                issues.append(f"{key}: process {process!r} not served live")
    return len(recipes), issues


def _radar_feed_stations(region: str = "us-east-1") -> set[str]:
    """Return the station ids currently present in the NEXRAD chunk feed.

    Lists the top-level `{STATION}/` prefixes in the unsigned
    `unidata-nexrad-level2-chunks` bucket (the real-time feed
    `earthlens.radar` fetches from). Ported from the retired
    `tools/radar/audit_radar_catalog.py`.

    Args:
        region: AWS region of the bucket.

    Returns:
        The set of station-id prefixes currently in the feed.
    """
    from earthlens.radar.backend import BUCKET, _s3_client

    client = _s3_client(region)
    stations: set[str] = set()
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": BUCKET, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        stations.update(
            prefix["Prefix"].rstrip("/")
            for prefix in response.get("CommonPrefixes", [])
        )
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated"):
            break
    return stations


def _live_radar(catalog: Any) -> tuple[int, list[str]]:
    """Confirm the real-time NEXRAD chunk feed is reachable and lines up.

    Flags a hard failure (feed served nothing → unreachable / outage) or an
    id-format mismatch (the feed is non-empty but no catalogued station is in
    it). Per-station idleness is expected — the feed is a rolling ~1-2 h
    buffer — so it is not flagged.
    """
    catalogued = set(catalog.datasets)
    feed = _radar_feed_stations()
    if not feed:
        return len(catalogued), [
            "NEXRAD chunk feed served no stations (unreachable / outage?)"
        ]
    if not (catalogued & feed):
        return len(catalogued), [
            "no catalogued station is in the live feed "
            "(id format may not match the feed prefixes)"
        ]
    return len(catalogued), []


def _nwp_latest_cycle(model: Any, hours_ago: int = 6) -> dt.datetime | None:
    """Return a model's most recent expected run datetime (or None).

    Ported from the retired `tools/nwp/refresh_nwp_catalog.py`.

    Args:
        model: A curated NWP model record (duck-typed: `cycles_utc`).
        hours_ago: How far back to look for the latest published cycle.

    Returns:
        The most recent cycle datetime at or before `now - hours_ago`,
        or None when the model declares no cycle hours.
    """
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


def _live_nwp(catalog: Any) -> tuple[int, list[str]]:
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
            status = _http_head(url)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}: latest cycle unreachable ({type(exc).__name__})")
            continue
        if status != 200:
            issues.append(f"{key}: HTTP {status} for latest cycle {url}")
    return checked, issues


def _live_ecmwf(catalog: Any) -> tuple[int, list[str]]:
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


#: First-page object cap when sampling a configuration directory's tokens.
_NWM_SAMPLE_KEYS = 400


def _nwm_sample_tokens(client: Any, day: str, directory: str) -> set[str]:
    """Return the distinct product `{output}` tokens under one config directory.

    Samples the first :data:`_NWM_SAMPLE_KEYS` objects of
    `{day}/{directory}/` on `noaa-nwm-pds` and parses each file's `{output}`
    token from the `nwm.tHHz.<family>.<output>.<step>.<domain>.nc` layout.
    For an ensemble directory the token carries the member suffix
    (`channel_rt_1`); :func:`_nwm_token_present` handles that.

    Args:
        client: An unsigned S3 client (see `refresh._nwm_unsigned_client`).
        day: An `nwm.YYYYMMDD` prefix.
        directory: A configuration directory name under `day`.

    Returns:
        The set of product `{output}` tokens seen in the sample.
    """
    from earthlens.nwm import BUCKET

    sample = client.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{day}/{directory}/", MaxKeys=_NWM_SAMPLE_KEYS
    )
    tokens: set[str] = set()
    for entry in sample.get("Contents", []):
        parts = entry["Key"].split("/")[-1].split(".")
        if len(parts) >= 6:
            tokens.add(parts[3])
    return tokens


def _nwm_token_present(s3_token: str, tokens: set[str]) -> bool:
    """Whether a product's bare `s3_token` shows among sampled file tokens.

    Matches the bare token (a deterministic carrier's file token *is* the
    `s3_token`) or its ensemble form `{s3_token}_{member}` (an ensemble
    carrier rides the member on the token, e.g. `channel_rt_1`), so an
    ensemble-only carrier is honoured rather than mis-reported.

    Args:
        s3_token: The product's bare `{output}` token (e.g. `channel_rt`).
        tokens: The sampled file tokens for one configuration directory.

    Returns:
        `True` if the product's token (bare or member-suffixed) is present.

    Examples:
        - A deterministic carrier's bare token counts as present:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"channel_rt", "land"})
            True

            ```
        - An ensemble carrier's `{token}_{member}` file token counts too:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"channel_rt_1"})
            True

            ```
        - A token absent from the sample is not present:
            ```python
            >>> from earthlens.cli.validate import _nwm_token_present
            >>> _nwm_token_present("channel_rt", {"land", "reservoir"})
            False

            ```
    """
    prefix = f"{s3_token}_"
    return any(token == s3_token or token.startswith(prefix) for token in tokens)


def _nwm_config_directory(config: Any, key: str) -> str:
    """Return the live bucket directory for an NWM configuration.

    A deterministic configuration is published under its bare `key`
    directory; an ensemble configuration (`members > 0`) publishes each
    member under `{key}_mem<N>`, so member 1 (`{key}_mem1`) is the directory
    sampled for the product-token check.

    Args:
        config: The configuration row (duck-typed: reads `members`).
        key: The configuration key (its bare directory name).

    Returns:
        The configuration's live directory name (`{key}_mem1` for an
        ensemble, `key` otherwise).

    Examples:
        - A deterministic configuration maps to its bare directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.validate import _nwm_config_directory
            >>> _nwm_config_directory(SimpleNamespace(members=0), "short_range")
            'short_range'

            ```
        - An ensemble configuration maps to its member-1 directory:
            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.cli.validate import _nwm_config_directory
            >>> _nwm_config_directory(SimpleNamespace(members=6), "medium_range")
            'medium_range_mem1'

            ```
    """
    return f"{key}_mem1" if getattr(config, "members", 0) else key


def _live_nwm(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each NWM product's `s3_token` appears live under a carrier config.

    Ports the product-token check of the retired
    `tools/nwm/audit_nwm_catalog.py`: every curated product's file token
    (`channel_rt`, `land`, ...) must show up among the live files of at least
    one configuration that publishes it. Carriers are tried deterministic-first
    and sampling stops at the first hit, so a clean run touches only as many
    configuration directories as needed (reusing the shared NWM bucket
    primitives from `earthlens.cli.refresh`).

    Args:
        catalog: The loaded NWM `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per token not
        found live under any carrier configuration.
    """
    from earthlens.cli.refresh import _nwm_latest_complete_day, _nwm_unsigned_client

    client = _nwm_unsigned_client()
    day = _nwm_latest_complete_day(client)
    issues: list[str] = []
    for key, product in catalog.datasets.items():
        carriers = sorted(
            (
                cfg_key
                for cfg_key, config in catalog.configurations.items()
                if key in (getattr(config, "products", None) or [])
            ),
            key=lambda k: catalog.configurations[k].members,
        )
        seen = False
        for cfg_key in carriers:
            directory = _nwm_config_directory(catalog.configurations[cfg_key], cfg_key)
            if _nwm_token_present(
                product.s3_token, _nwm_sample_tokens(client, day, directory)
            ):
                seen = True
                break
        if not seen:
            issues.append(
                f"{key}: s3_token {product.s3_token!r} not found live under "
                "any carrier configuration"
            )
    return len(catalog.datasets), issues


#: Provider id -> a live reachability validator (the `--live` half). May add
#: a provider not in :data:`_VALIDATORS` (e.g. openeo / ecmwf are live-only).
_LIVE_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "s3": _live_s3,
    "nwm": _live_nwm,
    "overture": _live_overture,
    "ghsl": _live_ghsl,
    "openeo": _live_openeo,
    "radar": _live_radar,
    "nwp": _live_nwp,
    "ecmwf": _live_ecmwf,
}


def supported_providers(live: bool = False) -> list[str]:
    """Return the provider ids that have a validator wired up.

    Args:
        live: When `True`, include providers that only have a `--live`
            reachability validator (e.g. openeo).

    Returns:
        The sorted provider ids `validate` can check.

    Examples:
        - nwp is wired up:

            ```python
            >>> from earthlens.cli.validate import supported_providers
            >>> "nwp" in supported_providers()
            True

            ```
    """
    providers = set(_VALIDATORS)
    if live:
        providers |= set(_LIVE_VALIDATORS)
    return sorted(providers)


def validate_one(info: BackendInfo, live: bool = False) -> ValidateResult:
    """Validate one provider's curated entries.

    Always runs the offline structural lint when one exists; with
    `live=True` it additionally runs the live reachability validator (a
    network / SDK round-trip per entry). A provider with neither returns
    `"unsupported"`; any error returns `"error"` — neither raises.

    Args:
        info: The backend to validate.
        live: When `True`, also run the live reachability validator.

    Returns:
        The :class:`ValidateResult` for `info`.
    """
    offline = _VALIDATORS.get(info.provider)
    live_validator = _LIVE_VALIDATORS.get(info.provider) if live else None
    if offline is None and live_validator is None:
        detail = (
            "no validator wired up for this provider"
            if not live or info.provider not in _LIVE_VALIDATORS
            else "no live validator wired up for this provider"
        )
        return ValidateResult(
            provider=info.provider, status="unsupported", detail=detail
        )
    try:
        catalog = load_catalog(info)
        checked = 0
        issues: list[str] = []
        for validator in (offline, live_validator):
            if validator is not None:
                count, found = validator(catalog)
                checked += count
                issues.extend(found)
    except Exception as exc:  # noqa: BLE001 — validation failures are reported
        return ValidateResult(provider=info.provider, status="error", detail=str(exc))
    return ValidateResult(
        provider=info.provider, status="ok", checked=checked, issues=issues
    )
