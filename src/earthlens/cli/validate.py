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


#: Provider id -> a callable taking the loaded catalog and returning
#: `(checked, issues)`. Providers without one report `"unsupported"`.
_VALIDATORS: dict[str, Callable[[Any], tuple[int, list[str]]]] = {
    "nwp": _validate_nwp,
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
