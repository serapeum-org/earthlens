"""Fail a CI lane whose tests were all skipped.

A lane where every test skips is indistinguishable, in the checks UI, from a
lane where everything passed: both are a green tick. That is how four e2e
lanes ran zero tests for an unknown number of weeks while reporting success
(see #1133), and why the catalog drift in #1129 surfaced only when it broke
an unrelated pull request.

The e2e workflows already treat "collected nothing" (pytest exit code 5) as a
failure unless the lane opts out. This extends that from *collected* to
*executed*: a lane that collects tests but runs none of them is just as blind,
and fails the same way.

Usage:
    python .github/scripts/assert_tests_ran.py <report.xml> <lane-name>

Exits 0 when at least one test actually executed (passed, failed, or errored),
or when the report holds no tests at all - "collected nothing" is the exit-5
case, which the caller has already decided about. Exits 1 when every collected
test was skipped.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _totals(report: Path) -> tuple[int, int]:
    """Return `(collected, skipped)` summed over every `<testsuite>`.

    Args:
        report: Path to a pytest `--junitxml` report.

    Returns:
        tuple[int, int]: Total tests collected and total skipped.
    """
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    collected = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    return collected, skipped


def main(argv: list[str]) -> int:
    """Check the report and explain the verdict on stdout.

    Args:
        argv: Command-line arguments after the program name: the report path
            and the lane name used in the failure message.

    Returns:
        int: 0 when the lane executed at least one test (or collected none),
            1 when every collected test was skipped.
    """
    if len(argv) != 2:
        print(f"usage: {Path(__file__).name} <report.xml> <lane-name>", file=sys.stderr)
        return 2
    report, lane = Path(argv[0]), argv[1]

    # No report means pytest died before writing one; the caller already
    # propagated that exit code, so do not second-guess it here.
    if not report.is_file():
        print(f"{lane}: no junit report at {report}; nothing to assert")
        return 0

    collected, skipped = _totals(report)
    if collected == 0:
        print(f"{lane}: no tests collected; the exit-5 rule governs this case")
        return 0
    executed = collected - skipped
    if executed > 0:
        print(f"{lane}: {executed} of {collected} test(s) executed")
        return 0

    print(
        f"::error::{lane}: all {collected} collected test(s) skipped - this lane "
        f"exercised nothing and would otherwise report success. Supply the "
        f"lane's credentials, or mark it as legitimately empty.",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
