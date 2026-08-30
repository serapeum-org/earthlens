"""Fail a CI lane whose tests were all skipped for a configuration reason.

A lane where every test skips is indistinguishable, in the checks UI, from a
lane where everything passed: both are a green tick. That is how four e2e
lanes ran zero tests for an unknown number of weeks while reporting success
(see #1133), and why the catalog drift in #1129 surfaced only when it broke
an unrelated pull request.

**This extends an existing guard rather than introducing the idea.**
`earthlens.testing.pytest_sessionfinish` already fails a lane that collected
tests, passed none, and skipped at least one for *upstream availability* -
it counts skips carrying that module's `live e2e skipped — ` prefix. The two
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

Exits 0 when every backend in the report executed at least one test (passed,
failed, or errored); when every backend the report names is listed in
`_EXPECTED_EMPTY`, so a lane devoted to a declared-empty backend is not failed
for being empty; and when the report is missing, truncated, or holds no tests
at all - "collected nothing" is the exit-5 case, which the caller has already
decided about. Exits 1 when a backend not listed in `_EXPECTED_EMPTY`
contributed only skips, whether it sat beside passing neighbours or the whole
lane was skipped. Exits 2 on a bad command line.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: Backends whose tests are expected to contribute only skips, with the reason.
#: A lane is not failed on their account. Keep this list short and evidenced -
#: an entry here is a backend nobody is watching.
#:
#: Deliberately NOT pre-seeded from a survey of what currently lacks
#: credentials. This guard is expected to turn several lanes red on the first
#: runs after it lands, and that is the point: a backend with no credentials in
#: CI is not being tested, and the whole reason this file exists is that the
#: previous arrangement reported success instead of saying so. Adding entries
#: up front to quiet the board would recreate exactly that. Each red is either
#: fixed by supplying the credential or recorded here with a reason someone
#: stands behind.
_EXPECTED_EMPTY = {
    "wdpa": "WDPA_TOKEN not issued yet (awaiting UNEP-WCMC approval)",
    "osm": "the osm-pbf extra is deliberately outside [all]",
    "mswep": "the GloH2O share is granted per person; CI cannot hold one",
    "airnow": "AIRNOW_API_KEY has never been issued for this repository",
}


def _backend(classname: str) -> str:
    """Return the backend directory a test belongs to, or `""` for none.

    pytest's `classname` is the dotted path of the test's module, plus the
    class when there is one. Its prefix depends on how the lane invoked
    pytest, and both forms occur in this repository:

        tests.cmems.test_cmems_e2e.TestLive              (a package-scoped lane)
        libs.providers.imagery.tests.gee.test_auth       (a marker-only lane)

    so the search starts at the last `tests` segment. What follows is
    `<backend>/<module>` plus an optional class; a module sitting directly
    under `tests/` has no backend and groups at lane level instead.

    A backend directory is not identified by its name: `test_ecmwf/` is a
    backend even though it is spelled like a module. Position decides it -
    anything before the module segment is the directory.

    Args:
        classname: A `<testcase>` element's `classname`, or its `name` when
            `classname` is empty - a collection-level skip carries the same
            dotted module path there instead.

    Returns:
        str: The backend directory name, or `""` when the test has none.
    """
    parts = classname.split(".")
    if "tests" not in parts:
        return ""
    parts = parts[len(parts) - 1 - parts[::-1].index("tests") + 1 :]
    # Drop a trailing class name. A test module is always `test_*`; anything
    # else in the final position is the class the test lives on.
    if parts and not parts[-1].startswith("test_"):
        parts = parts[:-1]
    # What remains is `<backend>/<module>`, or just `<module>` when the test
    # sits directly under `tests/`.
    return parts[0] if len(parts) >= 2 else ""


def _per_backend(report: Path) -> dict[str, tuple[int, int]]:
    """Return `{backend: (executed, skipped)}` for every backend in the report.

    A lane usually holds several backends, so a lane-level count hides a
    backend that contributed nothing: CMEMS's three tests sat inside a
    nineteen-test ocean lane, and the sixteen that passed kept the lane green
    for months. Grouping restores the granularity the problem actually has.

    Tests sitting directly under `tests/` belong to no backend and collect
    under the `""` key, which the caller skips. An xfail counts as executed.

    Args:
        report: Path to a pytest `--junitxml` report.

    Returns:
        dict[str, tuple[int, int]]: Executed and skipped counts per backend,
            keyed by backend directory name.
    """
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts: dict[str, list[int]] = {}
    for suite in suites:
        for case in suite.iter("testcase"):
            marker = case.find("skipped")
            is_skip = marker is not None and marker.get("type") != "pytest.xfail"
            # A collection-level skip - `pytest.importorskip` at module scope,
            # say - has an empty `classname` and carries the module path in
            # `name` instead, so it would otherwise land in the lane-level
            # bucket and be invisible per backend. That is precisely the shape
            # an uninstalled optional extra produces.
            where = case.get("classname") or case.get("name") or ""
            slot = counts.setdefault(_backend(where), [0, 0])
            slot[1 if is_skip else 0] += 1
    return {name: (ran, skipped) for name, (ran, skipped) in counts.items()}


def _totals(report: Path) -> tuple[int, int]:
    """Return `(collected, skipped)` summed over every `<testsuite>`.

    `collected` reads the suite `tests` attribute, but `skipped` is counted
    from the `<testcase>` elements so an xfail is not mistaken for a skip; a
    summary-only report with no `<testcase>` elements falls back to the suite
    attribute, the only signal it carries.

    Args:
        report: Path to a pytest `--junitxml` report.

    Returns:
        tuple[int, int]: Total tests collected, and total skipped excluding
            xfails.
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
        int: 0 when every backend the lane collected executed at least one
            test, when every backend the report names is a declared
            `_EXPECTED_EMPTY` exemption, and when the report is missing,
            truncated, or empty; 1 when a backend outside `_EXPECTED_EMPTY`
            contributed only skips, whether it sat beside passing neighbours
            or the whole lane was skipped; 2 when `argv` is not the two
            expected arguments.
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

    try:
        collected, skipped = _totals(report)
    except ET.ParseError as exc:
        # pytest killed mid-write leaves a truncated report. That is the
        # timeout's failure, not this lane's, and the caller's own exit code
        # already carries it - failing here would blame the guard instead.
        print(
            f"{lane}: junit report at {report} is incomplete ({exc}); nothing to assert"
        )
        return 0
    if collected == 0:
        print(f"{lane}: no tests collected; the exit-5 rule governs this case")
        return 0
    executed = collected - skipped
    if executed > 0:
        print(f"{lane}: {executed} of {collected} test(s) executed")

        # A lane-level count only notices a *wholly* dead lane. A backend that
        # went quiet inside a shared lane stays hidden behind its neighbours,
        # which is exactly how CMEMS went unexercised. Check each separately.
        try:
            per_backend = _per_backend(report)
        except ET.ParseError:
            # The report is re-read here, so a file still being written can
            # fail this parse even though the first one succeeded. Same
            # verdict as there: the caller's exit code carries the real
            # failure, and the lane did execute tests.
            return 0
        dead = sorted(
            name
            for name, (ran, skipped) in per_backend.items()
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

    # A wholly-skipped lane is judged by the same exemptions: a dedicated lane
    # holds one backend, so if that backend is declared empty the lane is too.
    # Without this the exemption only reached shared lanes, and a lane devoted
    # to an exempt backend still failed.
    try:
        present = {name for name in _per_backend(report) if name}
    except ET.ParseError:
        # Same truncated-report case the first parse already forgave; the
        # caller's exit code carries the real failure.
        return 0
    if present and present <= set(_EXPECTED_EMPTY):
        reasons = "; ".join(f"{n}: {_EXPECTED_EMPTY[n]}" for n in sorted(present))
        print(f"{lane}: every backend present is a declared exemption ({reasons})")
        return 0

    print(
        f"::error::{lane}: all {collected} collected test(s) skipped - this lane "
        f"exercised nothing and would otherwise report success. Supply the "
        f"lane's credentials, or mark it as legitimately empty.",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
