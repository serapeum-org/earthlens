"""Tests for the CI masked-lane guard at `.github/scripts/assert_tests_ran.py`.

The script is a workflow helper, not part of any distribution, so it is loaded
by path rather than imported. It complements `earthlens.testing`'s in-process
guard: that one fails a lane masked by upstream outages, this one fails a lane
masked by missing configuration.
"""

from __future__ import annotations

import importlib.util
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
            '<testsuites><testsuite name="s"/></testsuites>'
        )
        assert guard._totals(tmp_path / "r.xml") == (0, 0), (
            "absent attributes mishandled"
        )


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
            '<testsuites><testsuite tests="4" skipped="0" failures="4"/></testsuites>'
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

    @pytest.mark.parametrize("argv", [[], ["only-one"], ["a", "b", "c"]])
    def test_wrong_argument_count_is_a_usage_error(self, guard, argv, capsys):
        """Anything but `<report> <lane>` exits 2 and prints usage to stderr."""
        assert guard.main(argv) == 2, f"expected usage exit 2 for {argv!r}"
        assert "usage:" in capsys.readouterr().err, "usage was not printed to stderr"
