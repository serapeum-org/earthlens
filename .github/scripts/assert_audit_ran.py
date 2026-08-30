"""Fail a catalog-drift run whose audit could not reach a provider.

`earthlens datasets audit --strict` exits non-zero when a curated dataset is no
longer served, which is the drift the gate exists to catch. It says nothing
about a provider whose audit could not run at all: that reports
`status="error"` with an empty `broken` list, so the gate would report success
having verified nothing - the same "green while checking nothing" blindness the
masked-lane guard exists to prevent.

Usage:
    python .github/scripts/assert_audit_ran.py <audit.json>

Exits 0 when no provider errored, and when no report was written at all - the
caller's own exit code already carries that case. Exits 1 when a provider
reported `status="error"`, or when the report is not valid JSON. Exits 2 on a
bad command line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    """Report any provider whose audit errored.

    Args:
        argv: Command-line arguments after the program name: the path to the
            JSON emitted by `datasets audit --json`.

    Returns:
        int: 0 when no provider reported `status="error"` — providers with
            `status="unsupported"` have no prober and are only counted — and
            when no report was written, which the caller's own exit code
            already covers; 1 when at least one provider errored or the
            report is not valid JSON; 2 when `argv` is not the single
            expected argument.
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

    # Valid JSON of the wrong shape is not a pass. `{}` would otherwise report
    # "0 provider(s) audited" and exit 0 - a green gate that verified nothing,
    # the failure this script exists to prevent - and a list of scalars would
    # raise AttributeError, which reads as a bug in the checker rather than a
    # malformed report.
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        print(
            f"::error::audit report at {report} is not a list of provider "
            f"records (got {type(rows).__name__})",
        )
        return 1

    errored = [r for r in rows if r.get("status") == "error"]
    for row in errored:
        detail = row.get("detail") or "no detail given"
        print(
            f"::error::{row.get('provider', '?')}: audit could not run "
            f"({detail}) - drift is unverified for this provider",
        )
    if errored:
        return 1
    audited = [r for r in rows if r.get("status") == "ok"]
    print(f"{len(audited)} provider(s) audited, {len(rows) - len(audited)} unsupported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
