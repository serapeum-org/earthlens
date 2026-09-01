"""Fail a catalog-drift run whose audit could not reach a provider.

`earthlens datasets audit --strict` exits non-zero when a curated dataset is no
longer served, which is the drift the gate exists to catch. It says nothing
about a provider whose audit could not run at all: that reports
`status="error"` with an empty `broken` list, so the gate would report success
having verified nothing - the same "green while checking nothing" blindness the
masked-lane guard exists to prevent.

Usage:
    python .github/scripts/assert_audit_ran.py <audit.json>

Exits 0 when no provider errored; when every error reads as a transient reach
failure, which is printed as a warning instead, because a gate fanning out
over this many live services would otherwise go red whenever one of them is
briefly slow; and when no report was written at all - the caller's own exit
code already carries that case. Exits 1 when a provider reported
`status="error"` (or `variable_status="error"`, the variable-drift dimension's
own "could not run") for any other reason, or when the report is not valid JSON
or not the list of provider records the audit emits. Exits 2 on a bad command
line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Substrings marking a provider error as a transient reach failure rather than
#: a configuration or contract problem. Mirrors the eumetsat backend's
#: `_TRANSIENT_MARKERS`, which exists for the same reason.
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "bad gateway",
    "service unavailable",
    "502",
    "503",
    "504",
    # A 200 whose body is not a DDS is a maintenance / interstitial page from a
    # variable-lister (see erddap `variables_for`): the server is briefly not
    # serving real data, the same transient class as a 503. Without this it
    # would fail the gate hard while the equivalent 503 only warned.
    "did not return a dds",
)


def _detail(row: dict, key: str = "detail") -> str:
    """Return a row's detail field as text, whatever the provider put there.

    The shape guard only establishes that each row is a mapping, so a detail
    field can be any JSON scalar. Coercing here keeps a malformed report a
    *reported* failure rather than an `AttributeError` traceback that reads as a
    bug in this checker.

    Args:
        row: One provider record from the audit report.
        key: Which detail field to read — `"detail"` for the id-level audit or
            `"variable_detail"` for the variable-drift dimension.

    Returns:
        str: The detail text, or `""` when the row carries none.
    """
    detail = row.get(key)
    return "" if detail is None else str(detail)


def _looks_transient(detail: str) -> bool:
    """Whether a provider's failure detail reads as a transient reach failure.

    A case-insensitive substring match against `_TRANSIENT_MARKERS`. A row
    that carries no detail at all matches nothing and so counts as a hard
    error - an unexplained failure is not something to warn about and move on
    from.

    Args:
        detail: The `detail` field of an audit row.

    Returns:
        bool: True when the text contains a known transient marker.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def main(argv: list[str]) -> int:
    """Report any provider whose audit errored, failing on the hard ones.

    Every errored provider is printed. A provider whose detail reads as a
    transient reach failure is printed as a warning and does not change the
    exit code; anything else is an error that fails the gate, because drift
    went unverified for a reason that will not fix itself.

    Args:
        argv: Command-line arguments after the program name: the path to the
            JSON emitted by `datasets audit --json`.

    Returns:
        int: 0 when no provider reported `status="error"`, when every such
            error looks transient - providers with `status="unsupported"`
            have no prober and are only counted - and when no report was
            written, which the caller's own exit code already covers; 1 when
            a provider errored for a non-transient reason, or the report is
            not valid JSON or not a list of provider records; 2 when `argv`
            is not the single expected argument.
    """
    if len(argv) != 1:
        print(f"usage: {Path(__file__).name} <audit.json>", file=sys.stderr)
        return 2
    report = Path(argv[0])
    if not report.is_file():
        print(f"no audit report at {report}; nothing to assert")
        return 0
    try:
        rows = json.loads(report.read_text(encoding="utf-8"))
    except ValueError:
        print(f"::error::audit report at {report} is not valid JSON")
        return 1

    # Valid JSON of the wrong shape is not a pass. The audit always emits a
    # JSON *array*, so anything else is a malformed report rather than an empty
    # one: `{}` would otherwise report "0 provider(s) audited" and exit 0, a
    # green gate that verified nothing, and a list of scalars would raise
    # AttributeError, reading as a bug in the checker. An empty *array* is
    # different in kind - it is what the audit emits when no provider was
    # selected - so it stays a pass.
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        print(
            f"::error::audit report at {report} is not a list of provider "
            f"records (got {type(rows).__name__})",
        )
        return 1

    errored = [r for r in rows if r.get("status") == "error"]
    transient = [r for r in errored if _looks_transient(_detail(r))]
    hard = [r for r in errored if r not in transient]

    # A transient reach failure is not drift and not a defect. This gate spans
    # 26 live third-party services, so on any given run one of them being slow
    # or briefly unreachable is ordinary, and failing the whole audit for it
    # would train the reader to ignore the gate - the outcome this branch
    # exists to avoid. Report it as a warning; the next scheduled run re-checks.
    for row in transient:
        print(
            f"::warning::{row.get('provider', '?')}: audit could not reach the "
            f"provider ({_detail(row) or 'no detail given'}) - drift is "
            f"unverified this run",
        )
    for row in hard:
        print(
            f"::error::{row.get('provider', '?')}: audit could not run "
            f"({_detail(row) or 'no detail given'}) - drift is unverified "
            f"for this provider",
        )

    # The variable-drift dimension is a second "could not run" surface: a row
    # can pass the id-level audit (status="ok") while its variable fetch failed
    # (variable_status="error"). Hold it to the same rule so a variable-fetch
    # failure cannot leave the gate green having verified no variable drift.
    var_errored = [r for r in rows if r.get("variable_status") == "error"]
    var_transient = [
        r for r in var_errored if _looks_transient(_detail(r, "variable_detail"))
    ]
    var_hard = [r for r in var_errored if r not in var_transient]
    for row in var_transient:
        print(
            f"::warning::{row.get('provider', '?')}: variable audit could not "
            f"reach the provider ({_detail(row, 'variable_detail') or 'no detail given'})"
            f" - variable drift is unverified this run",
        )
    for row in var_hard:
        print(
            f"::error::{row.get('provider', '?')}: variable audit could not run "
            f"({_detail(row, 'variable_detail') or 'no detail given'}) - variable "
            f"drift is unverified for this provider",
        )
    if hard or var_hard:
        return 1
    audited = sum(1 for r in rows if r.get("status") == "ok")
    unsupported = sum(1 for r in rows if r.get("status") == "unsupported")
    # Counted by status rather than as "everything else": with the transient
    # branch in place, a provider that timed out is neither audited nor
    # unsupported, and lumping it in with the latter would understate how much
    # of the catalogue actually got checked.
    line = f"{audited} provider(s) audited, {unsupported} unsupported"
    if transient:
        line += f", {len(transient)} unreachable this run"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
