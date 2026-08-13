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


#: Provider id -> a live reachability validator (the `--live` half). May add
#: a provider not in :data:`_VALIDATORS` (e.g. openeo / ecmwf are live-only).
_LIVE_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("live_validator"),
}


def supported_providers(live: bool = False) -> list[str]:
    """Return the provider ids that have a validator wired up.

    Args:
        live: When `True`, include providers that only have a `--live`
            reachability validator (e.g. openeo).

    Returns:
        The sorted provider ids `validate` can check.

    Examples:
        - The wired-up ids come back as a sorted list:

            ```python
            >>> from earthlens.cli.validate import supported_providers
            >>> ids = supported_providers()
            >>> ids == sorted(ids)
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
