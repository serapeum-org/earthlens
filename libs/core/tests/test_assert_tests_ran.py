"""Tests for the CI masked-lane guard at `.github/scripts/assert_tests_ran.py`.

The script is a workflow helper, not part of any distribution, so it is loaded
by path rather than imported. It complements `earthlens.testing`'s in-process
guard: that one fails a lane masked by upstream outages, this one fails a lane
masked by missing configuration.
"""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3] / ".github" / "scripts" / "assert_tests_ran.py"
)

pytestmark = pytest.mark.skipif(
    not _SCRIPT.is_file(), reason=f"workflow helper not present at {_SCRIPT}"
)


def _load():
    """Load the guard script from its path in the repository."""
    spec = importlib.util.spec_from_file_location("assert_tests_ran", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    """The loaded guard module."""
    return _load()


def _report(path: Path, *suites: tuple[int, int], wrap: bool = True) -> Path:
    """Write a junit report of `(tests, skipped)` suites and return its path."""
    body = "".join(
        f'<testsuite name="s{i}" tests="{tests}" skipped="{skipped}"></testsuite>'
        for i, (tests, skipped) in enumerate(suites)
    )
    xml = f"<testsuites>{body}</testsuites>" if wrap else body
    path.write_text(xml, encoding="utf-8")
    return path


def _case_report(path: Path, *cases: tuple[str, str | None]) -> Path:
    """Write a junit report with per-`<testcase>` outcomes and return its path.

    Args:
        path: File to write.
        cases: `(name, skip_type)` pairs; `skip_type` is `None` for a test that
            ran, or the `type` pytest stamps on `<skipped>` — `pytest.skip` for
            a genuine skip, `pytest.xfail` for an expected failure.
    """
    body = "".join(
        f'<testcase classname="t" name="{name}">'
        + (f'<skipped type="{kind}" message="m"/>' if kind else "")
        + "</testcase>"
        for name, kind in cases
    )
    skipped = sum(1 for _, kind in cases if kind)
    path.write_text(
        f'<testsuites><testsuite name="s" tests="{len(cases)}" '
        f'skipped="{skipped}">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _backend_report(path: Path, *cases: tuple[str, str, str | None]) -> Path:
    """Write a junit report of `(backend, name, skip_type)` cases.

    Args:
        path: File to write.
        cases: `(backend, test name, skip_type)`; `skip_type` is `None` when the
            test ran. An empty backend puts the test directly under `tests/`.
    """
    body = ""
    for backend, name, kind in cases:
        classname = (
            f"tests.{backend}.test_mod.TestX" if backend else "tests.test_mod.TestX"
        )
        body += (
            f'<testcase classname="{classname}" name="{name}">'
            + (f'<skipped type="{kind}" message="m"/>' if kind else "")
            + "</testcase>"
        )
    skipped = sum(1 for _, _, kind in cases if kind)
    path.write_text(
        f'<testsuites><testsuite name="s" tests="{len(cases)}" '
        f'skipped="{skipped}">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _raise_parse_error(report):
    """Stand in for a second junit parse that lands on a truncated report."""
    raise ET.ParseError("no element found")


class TestTotals:
    """Tests for the module-private `_totals` helper."""

    def test_sums_a_wrapped_report(self, guard, tmp_path):
        """Every `<testsuite>` under a `<testsuites>` root is summed."""
        report = _report(tmp_path / "r.xml", (10, 3), (5, 2))
        assert guard._totals(report) == (15, 5), "suites were not summed"

    def test_reads_a_bare_testsuite_root(self, guard, tmp_path):
        """A report whose root is `<testsuite>` is read directly, not searched."""
        report = _report(tmp_path / "r.xml", (7, 1), wrap=False)
        assert guard._totals(report) == (7, 1), "a bare testsuite root was missed"

    def test_missing_attributes_default_to_zero(self, guard, tmp_path):
        """A suite without `tests` / `skipped` contributes nothing rather than raising."""
        (tmp_path / "r.xml").write_text(
            '<testsuites><testsuite name="s"/></testsuites>', encoding="utf-8"
        )
        assert guard._totals(tmp_path / "r.xml") == (0, 0), (
            "absent attributes mishandled"
        )

    def test_an_xfail_is_not_a_skip(self, guard, tmp_path):
        """pytest folds `xfail` into the suite's skipped count, but an xfail ran."""
        report = _case_report(tmp_path / "r.xml", ("a", "pytest.xfail"))
        assert guard._totals(report) == (1, 0), "an xfail was counted as a skip"

    def test_genuine_skips_are_still_counted(self, guard, tmp_path):
        """A `pytest.skip` outcome counts, so a credential-less lane still fails."""
        report = _case_report(
            tmp_path / "r.xml", ("a", "pytest.skip"), ("b", "pytest.skip")
        )
        assert guard._totals(report) == (2, 2), "genuine skips were not counted"

    def test_mixed_outcomes_count_only_real_skips(self, guard, tmp_path):
        """An xfail beside a skip leaves exactly one skip."""
        report = _case_report(
            tmp_path / "r.xml", ("a", "pytest.xfail"), ("b", "pytest.skip"), ("c", None)
        )
        assert guard._totals(report) == (3, 1), "mixed outcomes miscounted"

    def test_summary_only_report_falls_back_to_the_attribute(self, guard, tmp_path):
        """With no `<testcase>` elements the suite attribute is the only signal."""
        report = _report(tmp_path / "r.xml", (4, 4))
        assert guard._totals(report) == (4, 4), "the attribute fallback was lost"


class TestMain:
    """Tests for the guard's `main` entry point."""

    def test_all_skipped_fails_and_names_the_lane(self, guard, tmp_path, capsys):
        """A lane whose every test skipped exits 1 with a GitHub error naming it."""
        report = _report(tmp_path / "r.xml", (3, 3))
        code = guard.main([str(report), "e2e-cmems"])
        out = capsys.readouterr().out
        assert code == 1, f"an all-skipped lane must fail, got {code}"
        assert "::error::" in out, f"no GitHub error annotation in: {out}"
        assert "e2e-cmems" in out, f"the lane is not named in: {out}"

    def test_an_all_xfail_lane_passes(self, guard, tmp_path):
        """A lane whose every test xfailed exercised its tests and must not fail."""
        report = _case_report(
            tmp_path / "r.xml", ("a", "pytest.xfail"), ("b", "pytest.xfail")
        )
        assert guard.main([str(report), "lane"]) == 0, "an all-xfail lane was failed"

    def test_executed_tests_pass(self, guard, tmp_path, capsys):
        """A lane that executed anything passes and reports the ratio."""
        report = _report(tmp_path / "r.xml", (10, 2))
        code = guard.main([str(report), "e2e-erddap"])
        assert code == 0, f"a lane that ran tests must pass, got {code}"
        assert "8 of 10" in capsys.readouterr().out, (
            "the executed ratio is not reported"
        )

    def test_a_single_execution_is_enough(self, guard, tmp_path):
        """One non-skipped test clears the guard; it is not a skip-ratio check."""
        report = _report(tmp_path / "r.xml", (100, 99))
        assert guard.main([str(report), "lane"]) == 0, "one executed test should pass"

    def test_failures_count_as_executed(self, guard, tmp_path):
        """A failing test still ran, so the guard defers to pytest's own exit code."""
        (tmp_path / "r.xml").write_text(
            '<testsuites><testsuite tests="4" skipped="0" failures="4"/></testsuites>',
            encoding="utf-8",
        )
        assert guard.main([str(tmp_path / "r.xml"), "lane"]) == 0, (
            "failures are executions"
        )

    def test_nothing_collected_defers_to_the_exit_five_rule(
        self, guard, tmp_path, capsys
    ):
        """An empty report passes, because the caller already decided about exit 5."""
        report = _report(tmp_path / "r.xml", (0, 0))
        code = guard.main([str(report), "lane"])
        assert code == 0, f"an empty collection must not fail here, got {code}"
        assert "no tests collected" in capsys.readouterr().out

    def test_missing_report_does_not_mask_the_real_exit_code(
        self, guard, tmp_path, capsys
    ):
        """With no report pytest died first, so the guard stays silent about it."""
        code = guard.main([str(tmp_path / "absent.xml"), "lane"])
        assert code == 0, f"a missing report must not invent a failure, got {code}"
        assert "no junit report" in capsys.readouterr().out

    def test_a_truncated_report_does_not_blame_the_guard(self, guard, tmp_path, capsys):
        """pytest killed mid-write leaves unparseable XML; that is the timeout's failure."""
        report = tmp_path / "r.xml"
        report.write_text('<testsuites><testsuite tests="3"', encoding="utf-8")
        code = guard.main([str(report), "lane"])
        assert code == 0, f"a truncated report must not fail the lane, got {code}"
        assert "incomplete" in capsys.readouterr().out

    def test_a_truncated_second_parse_leaves_a_running_lane_green(
        self, guard, tmp_path, monkeypatch, capsys
    ):
        """The per-backend re-parse failing is the timeout's failure, not the lane's."""
        monkeypatch.setattr(guard, "_per_backend", _raise_parse_error)
        report = _report(tmp_path / "r.xml", (10, 2))
        assert guard.main([str(report), "lane"]) == 0, (
            "a re-parse failure invented a lane failure"
        )
        assert "8 of 10" in capsys.readouterr().out, "the ratio was not still reported"

    def test_a_truncated_second_parse_does_not_fail_a_skipped_lane(
        self, guard, tmp_path, monkeypatch
    ):
        """The exemption lookup's parse is guarded too, so a dead lane is not blamed."""
        monkeypatch.setattr(guard, "_per_backend", _raise_parse_error)
        report = _report(tmp_path / "r.xml", (3, 3))
        assert guard.main([str(report), "lane"]) == 0, (
            "a re-parse failure was reported as a dead lane"
        )

    @pytest.mark.parametrize("argv", [[], ["only-one"], ["a", "b", "c"]])
    def test_wrong_argument_count_is_a_usage_error(self, guard, argv, capsys):
        """Anything but `<report> <lane>` exits 2 and prints usage to stderr."""
        assert guard.main(argv) == 2, f"expected usage exit 2 for {argv!r}"
        assert "usage:" in capsys.readouterr().err, "usage was not printed to stderr"


class TestBackendGrouping:
    """Tests for the per-backend view that makes the guard match the problem."""

    @pytest.mark.parametrize(
        "classname, expected",
        [
            ("tests.cmems.test_cmems_e2e.TestLive", "cmems"),
            ("tests.erddap.test_catalog.TestBundled", "erddap"),
            ("tests.test_aggregate.TestReal", ""),
            ("", ""),
            ("weird", ""),
            ("tests", ""),
            # A module directly under tests/ has no backend directory.
            ("tests.cmems", ""),
            # A marker-only lane emits the full package path; the search starts
            # at the last `tests` segment, so the backend still resolves.
            ("libs.core.tests.cmems.test_mod.TestX", "cmems"),
            ("libs.providers.imagery.tests.gee.test_auth", "gee"),
            # A backend directory spelled like a module is still a directory.
            ("tests.test_ecmwf.test_catalog.TestCatalog", "test_ecmwf"),
            # A bare test function has no class segment to drop.
            ("tests.jaxa.test_e2e", "jaxa"),
            ("tests..test_mod.TestX", ""),
            # The search starts at the *last* `tests`, so a package path that
            # itself begins with one still resolves the backend, not `libs`.
            ("tests.libs.core.tests.gee.test_mod.TestX", "gee"),
            # A backend directory with its own subdirectory still groups under
            # the backend, which is the level the exemption list speaks about.
            ("tests.gee.sub.test_mod.TestX", "gee"),
            # `tests` in the final position leaves nothing after the slice.
            ("libs.core.tests", ""),
        ],
    )
    def test_backend_is_the_segment_after_tests(self, guard, classname, expected):
        """The backend is the directory under `tests/`, not the module."""
        assert guard._backend(classname) == expected, f"misparsed {classname!r}"

    def test_counts_are_grouped_per_backend(self, guard, tmp_path):
        """Each backend gets its own executed/skipped pair."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("cmems", "a", "pytest.skip"),
            ("erddap", "b", None),
            ("erddap", "c", None),
        )
        assert guard._per_backend(report) == {"cmems": (0, 1), "erddap": (2, 0)}

    def test_a_collection_level_skip_is_attributed_to_its_backend(
        self, guard, tmp_path
    ):
        """A module-scope skip has no classname and carries the path in `name`.

        `pytest.importorskip` at module scope produces exactly this, which is
        the shape an uninstalled optional extra takes.
        """
        (tmp_path / "r.xml").write_text(
            '<testsuites><testsuite name="s" tests="1" skipped="1">'
            '<testcase classname="" name="tests.argo.test_x">'
            '<skipped type="pytest.skip" message="m"/></testcase>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        assert guard._per_backend(tmp_path / "r.xml") == {"argo": (0, 1)}, (
            "a collection-level skip was not attributed to its backend"
        )

    def test_a_present_classname_wins_over_the_name_fallback(self, guard, tmp_path):
        """`name` stands in only when `classname` is empty, never overriding it."""
        (tmp_path / "r.xml").write_text(
            '<testsuites><testsuite name="s" tests="1" skipped="0">'
            '<testcase classname="tests.gee.test_mod.TestX" name="tests.argo.test_x"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        assert guard._per_backend(tmp_path / "r.xml") == {"gee": (1, 0)}, (
            "the name fallback displaced a present classname"
        )

    def test_an_xfail_counts_as_executed_per_backend(self, guard, tmp_path):
        """The xfail rule applies inside a group too."""
        report = _backend_report(tmp_path / "r.xml", ("gee", "a", "pytest.xfail"))
        assert guard._per_backend(report) == {"gee": (1, 0)}

    def test_reads_a_bare_testsuite_root(self, guard, tmp_path):
        """A report whose root is `<testsuite>` groups just like a wrapped one."""
        (tmp_path / "r.xml").write_text(
            '<testsuite name="s" tests="1" skipped="0">'
            '<testcase classname="tests.cmems.test_mod.TestX" name="a"/>'
            "</testsuite>",
            encoding="utf-8",
        )
        assert guard._per_backend(tmp_path / "r.xml") == {"cmems": (1, 0)}, (
            "a bare testsuite root was missed"
        )

    def test_a_case_without_a_classname_has_no_backend(self, guard, tmp_path):
        """A `<testcase>` lacking `classname` falls into the lane-level group."""
        (tmp_path / "r.xml").write_text(
            '<testsuites><testsuite name="s" tests="1" skipped="0">'
            '<testcase name="a"/></testsuite></testsuites>',
            encoding="utf-8",
        )
        assert guard._per_backend(tmp_path / "r.xml") == {"": (1, 0)}, (
            "a missing classname was mishandled"
        )


class TestDeadBackendDetection:
    """Tests for failing a lane on one silent backend among healthy ones."""

    def test_a_silent_backend_fails_the_lane(self, guard, tmp_path, capsys):
        """The case a lane-level count cannot see: cmems skipped, erddap green."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("cmems", "a", "pytest.skip"),
            ("cmems", "b", "pytest.skip"),
            ("erddap", "c", None),
            ("erddap", "d", None),
        )
        code = guard.main([str(report), "e2e (rest-of-ocean)"])
        out = capsys.readouterr().out
        assert code == 1, f"a silent backend must fail the lane, got {code}"
        assert "cmems" in out, f"the silent backend is not named: {out}"
        assert "erddap" not in out, f"a healthy backend was blamed: {out}"

    def test_a_healthy_lane_still_passes(self, guard, tmp_path):
        """Every backend executing something leaves the lane green."""
        report = _backend_report(
            tmp_path / "r.xml", ("cmems", "a", None), ("erddap", "b", None)
        )
        assert guard.main([str(report), "lane"]) == 0, "a healthy lane was failed"

    def test_a_declared_empty_backend_is_exempt(self, guard, tmp_path):
        """A backend listed in `_EXPECTED_EMPTY` does not fail its lane."""
        assert "wdpa" in guard._EXPECTED_EMPTY, "wdpa should be a declared exemption"
        report = _backend_report(
            tmp_path / "r.xml", ("wdpa", "a", "pytest.skip"), ("iucn", "b", None)
        )
        assert guard.main([str(report), "lane"]) == 0, "a declared exemption failed"

    @pytest.mark.parametrize("backend", ["wdpa", "mswep", "airnow"])
    def test_a_dedicated_lane_of_an_exempt_backend_passes(
        self, guard, tmp_path, capsys, backend
    ):
        """A lane holding only a declared-empty backend is exempt too."""
        report = _backend_report(
            tmp_path / "r.xml",
            (backend, "a", "pytest.skip"),
            (backend, "b", "pytest.skip"),
        )
        assert guard.main([str(report), f"e2e-{backend}"]) == 0, (
            f"the {backend} lane was failed despite its exemption"
        )
        assert "declared exemption" in capsys.readouterr().out

    def test_an_exempt_backend_does_not_carry_a_dead_neighbour(
        self, guard, tmp_path, capsys
    ):
        """A wholly-skipped lane mixing an exemption with a live backend still fails."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("wdpa", "a", "pytest.skip"),
            ("cmems", "b", "pytest.skip"),
        )
        assert guard.main([str(report), "lane"]) == 1, "a mixed dead lane must fail"
        assert "::error::" in capsys.readouterr().out, "no annotation for a dead lane"

    def test_a_wholly_skipped_lane_of_backendless_tests_fails(
        self, guard, tmp_path, capsys
    ):
        """No backend at all is not a vacuous exemption, even though `set() <= x`."""
        report = _backend_report(
            tmp_path / "r.xml", ("", "a", "pytest.skip"), ("", "b", "pytest.skip")
        )
        assert guard.main([str(report), "lane"]) == 1, (
            "an empty backend set was read as a blanket exemption"
        )
        assert "exercised nothing" in capsys.readouterr().out

    def test_a_wholly_skipped_unexempt_lane_still_fails(self, guard, tmp_path, capsys):
        """The exemption does not blanket every wholly-skipped lane."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("cmems", "a", "pytest.skip"),
            ("cmems", "b", "pytest.skip"),
        )
        assert guard.main([str(report), "e2e-cmems"]) == 1, "an unexempt lane must fail"

    @pytest.mark.parametrize("backend", ["mswep", "airnow"])
    def test_the_structurally_empty_backends_are_declared(self, guard, backend):
        """mswep and airnow cannot hold credentials in CI, so they are exempt."""
        assert backend in guard._EXPECTED_EMPTY, f"{backend} should be declared empty"
        assert guard._EXPECTED_EMPTY[backend], f"{backend} needs a stated reason"

    def test_tests_without_a_backend_do_not_form_a_group(self, guard, tmp_path):
        """A module directly under `tests/` is judged at lane level only."""
        report = _backend_report(
            tmp_path / "r.xml", ("", "a", "pytest.skip"), ("erddap", "b", None)
        )
        assert guard.main([str(report), "lane"]) == 0, "a lane-level skip was blamed"

    def test_every_silent_backend_is_named(self, guard, tmp_path, capsys):
        """Two dead backends in one lane are both reported, not just the first."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("cmems", "a", "pytest.skip"),
            ("gee", "b", "pytest.skip"),
            ("erddap", "c", None),
        )
        assert guard.main([str(report), "lane"]) == 1, "silent backends must fail"
        out = capsys.readouterr().out
        assert "cmems" in out, f"cmems is not named among the dead: {out}"
        assert "gee" in out, f"gee is not named among the dead: {out}"
        assert out.count("::error::") == 2, f"expected one annotation each: {out}"

    def test_a_wholly_skipped_lane_is_reported_once_at_lane_level(
        self, guard, tmp_path, capsys
    ):
        """With nothing executed the lane-level message stands in for per-backend ones."""
        report = _backend_report(
            tmp_path / "r.xml",
            ("cmems", "a", "pytest.skip"),
            ("gee", "b", "pytest.skip"),
        )
        assert guard.main([str(report), "lane"]) == 1, "a dead lane must fail"
        out = capsys.readouterr().out
        assert out.count("::error::") == 1, f"expected a single annotation: {out}"
        assert "all 2 collected test(s) skipped" in out, f"wrong verdict: {out}"

    def test_an_exempt_backend_alone_still_leaves_the_lane_green(self, guard, tmp_path):
        """An exemption is per backend, so a second exempt backend is exempt too."""
        assert "osm" in guard._EXPECTED_EMPTY, "osm should be a declared exemption"
        report = _backend_report(
            tmp_path / "r.xml",
            ("osm", "a", "pytest.skip"),
            ("wdpa", "b", "pytest.skip"),
            ("erddap", "c", None),
        )
        assert guard.main([str(report), "lane"]) == 0, "declared exemptions failed"
