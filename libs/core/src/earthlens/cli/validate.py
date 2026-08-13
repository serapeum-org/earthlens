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

from earthlens._cli_tooling import dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog
from earthlens.cli.refresh import _TIMEOUT


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


#: Provider id -> a callable taking the loaded catalog and returning
#: `(checked, issues)`. Providers without one report `"unsupported"`.
_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("validator"),
    "nwp": _validate_nwp,
}


# --------------------------------------------------------------------------- #
# Live reachability validators (the `--live` half) — confirm a curated entry
# still resolves upstream. A superset of the offline lint; opt-in because it
# goes to the network / SDK. Each live source sits behind a mockable helper.
# --------------------------------------------------------------------------- #
def _http_head(url: str) -> int:
    """Return the HTTP status of a HEAD request (following redirects)."""
    return requests.head(url, timeout=_TIMEOUT, allow_redirects=True).status_code


#: CDSE openEO processes endpoint (public; pairs with the collections one).


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


#: Provider id -> a live reachability validator (the `--live` half). May add
#: a provider not in :data:`_VALIDATORS` (e.g. openeo / ecmwf are live-only).
_LIVE_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("live_validator"),
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
