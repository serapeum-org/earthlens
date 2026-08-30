"""Fail a CI lane whose tests were all skipped for a configuration reason.

A lane where every test skips is indistinguishable, in the checks UI, from a
lane where everything passed: both are a green tick. That is how four e2e
lanes ran zero tests for an unknown number of weeks while reporting success
(see #1133), and why the catalog drift in #1129 surfaced only when it broke
an unrelated pull request.

**This extends an existing guard rather than introducing the idea.**
`earthlens.testing.pytest_sessionfinish` already fails a lane that collected
tests, passed none, and skipped at least one for *upstream availability* -
it counts skips carrying that module's `live e2e skipped - ` prefix. The two
partition the problem and never double-fire:

- upstream was down    -> the in-process guard fails the lane, pytest exits
                          non-zero, and this script is never reached
- nothing was configured -> the skips carry no availability prefix, so that
                          guard counts zero, pytest exits 0, and the lane
                          reports green. That is the gap this script closes -
                          the CMEMS case, where the credentials were simply
                          never passed in.

The workflows also treat "collected nothing" (pytest exit code 5) as a failure
unless the lane opts out. This carries that from *collected* to *executed*.

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

#: Backends whose tests are expected to contribute only skips, with the reason.
#: A lane is not failed on their account. Keep this list short and evidenced -
#: an entry here is a backend nobody is watching.
_EXPECTED_EMPTY = {
    "wdpa": "WDPA_TOKEN not issued yet (awaiting UNEP-WCMC approval)",
    "osm": "the osm-pbf extra is deliberately outside [all]",
}


def _backend(classname: str) -> str:
    """Return the backend directory a test belongs to, or `""` for none.

    pytest stamps `classname` as `tests.<backend>.<module>.<Class>`, so the
    segment after `tests` names the backend. A module sitting directly under
    `tests/` has no backend directory - its second segment is the module, which
    starts with `test_` - and groups at lane level instead.

    Args:
        classname: The `classname` attribute of a `<testcase>` element.

    Returns:
        str: The backend directory name, or `""` when the test has none.
    """
    parts = classname.split(".")
    if len(parts) > 1 and parts[0] == "tests" and not parts[1].startswith("test_"):
        return parts[1]
    return ""


def _per_backend(report: Path) -> dict[str, tuple[int, int]]:
    """Return `{backend: (executed, skipped)}` for every backend in the report.

    A lane usually holds several backends, so a lane-level count hides a
    backend that contributed nothing: CMEMS's three tests sat inside a
    nineteen-test ocean lane, and the sixteen that passed kept the lane green
    for months. Grouping restores the granularity the problem actually has.

    Args:
        report: Path to a pytest `--junitxml` report.

    Returns:
        dict[str, tuple[int, int]]: Executed and skipped counts per backend.
    """
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts: dict[str, list[int]] = {}
    for suite in suites:
        for case in suite.iter("testcase"):
            marker = case.find("skipped")
            is_skip = marker is not None and marker.get("type") != "pytest.xfail"
            slot = counts.setdefault(_backend(case.get("classname", "")), [0, 0])
            slot[1 if is_skip else 0] += 1
    return {name: (ran, skipped) for name, (ran, skipped) in counts.items()}


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

    # Count skips from the per-testcase elements rather than the suite
    # attribute. pytest records an expected failure as
    # `<skipped type="pytest.xfail">` and folds it into the suite's `skipped`
    # count, but an xfail *ran* - treating it as a skip would fail a lane that
    # executed everything it had, and tell it to supply credentials it does
    # not need.
    skipped = 0
    saw_cases = False
    for suite in suites:
        for case in suite.iter("testcase"):
            saw_cases = True
            marker = case.find("skipped")
            if marker is not None and marker.get("type") != "pytest.xfail":
                skipped += 1
    if not saw_cases:
        # A summary-only report (no <testcase> elements) leaves the attribute
        # as the only signal available.
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

        # A lane-level count only notices a *wholly* dead lane. A backend that
        # went quiet inside a shared lane stays hidden behind its neighbours,
        # which is exactly how CMEMS went unexercised. Check each separately.
        dead = sorted(
            name
            for name, (ran, skipped) in _per_backend(report).items()
            if name and ran == 0 and skipped > 0 and name not in _EXPECTED_EMPTY
        )
        if dead:
            for name in dead:
                print(
                    f"::error::{lane}: backend '{name}' contributed only skips - "
                    f"it exercised nothing while the rest of the lane passed. "
                    f"Supply its credentials, or add it to _EXPECTED_EMPTY with "
                    f"a reason.",
                )
            return 1
        return 0

    print(
        f"::error::{lane}: all {collected} collected test(s) skipped - this lane "
        f"exercised nothing and would otherwise report success. Supply the "
        f"lane's credentials, or mark it as legitimately empty.",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
