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

from earthlens.cli.adapter import BackendInfo, load_catalog


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
    models = catalog.datasets
    for key, record in models.items():
        backend = getattr(record, "backend", None)
        if backend == "direct-https" and not getattr(record, "url_template", None):
            issues.append(f"{key}: direct-https model has no url_template")
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
        issues = _require(key, record, ("name",))
        lat = getattr(record, "latitude", None)
        lon = getattr(record, "longitude", None)
        if not (isinstance(lat, (int, float)) and -90 <= lat <= 90):
            issues.append(f"{key}: latitude {lat!r} out of range")
        if not (isinstance(lon, (int, float)) and -180 <= lon <= 180):
            issues.append(f"{key}: longitude {lon!r} out of range")
        return issues

    return _lint(catalog, check)


def _validate_tropycal(catalog: Any) -> tuple[int, list[str]]:
    """Each Tropycal basin needs at least one declared source."""
    return _lint(catalog, lambda k, r: _require(k, r, ("sources",)))


def _validate_gdacs(catalog: Any) -> tuple[int, list[str]]:
    """Each GDACS hazard type needs a name and a description."""
    return _lint(catalog, lambda k, r: _require(k, r, ("name", "description")))


def _validate_chc(catalog: Any) -> tuple[int, list[str]]:
    """Each CHC dataset needs FTP bases, a file pattern, and variables."""

    def check(key: str, record: Any) -> list[str]:
        issues = _require(key, record, ("ftp_bases", "variables"))
        if not (
            getattr(record, "file_patterns", None)
            or getattr(record, "discrete_files", None)
        ):
            issues.append(f"{key}: no file_patterns or discrete_files")
        return issues

    return _lint(catalog, check)


#: Provider id -> a callable taking the loaded catalog and returning
#: `(checked, issues)`. Providers without one report `"unsupported"`.
_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "nwp": _validate_nwp,
    "s3": _validate_s3,
    "ghsl": _validate_ghsl,
    "overture": _validate_overture,
    "fdsn": _validate_fdsn,
    "firms": _validate_firms,
    "radar": _validate_radar,
    "tropycal": _validate_tropycal,
    "gdacs": _validate_gdacs,
    "chc": _validate_chc,
}


def supported_providers() -> list[str]:
    """Return the provider ids that have a validator wired up.

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
    return sorted(_VALIDATORS)


def validate_one(info: BackendInfo) -> ValidateResult:
    """Validate one provider's curated entries.

    A provider with no validator returns `"unsupported"`; any error
    running the validator returns `"error"` — neither raises.

    Args:
        info: The backend to validate.

    Returns:
        The :class:`ValidateResult` for `info`.
    """
    validator = _VALIDATORS.get(info.provider)
    if validator is None:
        return ValidateResult(
            provider=info.provider,
            status="unsupported",
            detail="no validator wired up for this provider",
        )
    try:
        catalog = load_catalog(info)
        checked, issues = validator(catalog)
    except Exception as exc:  # noqa: BLE001 — validation failures are reported
        return ValidateResult(provider=info.provider, status="error", detail=str(exc))
    return ValidateResult(
        provider=info.provider, status="ok", checked=checked, issues=issues
    )
