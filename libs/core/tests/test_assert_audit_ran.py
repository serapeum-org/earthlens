"""Tests for the catalog-drift checker at `.github/scripts/assert_audit_ran.py`.

Loaded by path, like its sibling guard: it is a workflow helper rather than
part of a distribution. It exists because `datasets audit --strict` reports
only *drift*, and stays silent about a provider whose audit could not run.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3] / ".github" / "scripts" / "assert_audit_ran.py"
)

pytestmark = pytest.mark.skipif(
    not _SCRIPT.is_file(), reason=f"workflow helper not present at {_SCRIPT}"
)


@pytest.fixture(scope="module")
def checker():
    """The loaded checker module."""
    spec = importlib.util.spec_from_file_location("assert_audit_ran", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(path: Path, *rows: dict) -> Path:
    """Write an audit JSON report and return its path."""
    path.write_text(json.dumps(list(rows)), encoding="utf-8")
    return path


class TestMain:
    """Tests for the checker's `main` entry point."""

    def test_all_audited_passes(self, checker, tmp_path, capsys):
        """Every provider reporting `ok` leaves the gate green."""
        report = _report(tmp_path / "a.json", {"provider": "erddap", "status": "ok"})
        assert checker.main([str(report)]) == 0, "an all-ok report must pass"
        assert "1 provider(s) audited" in capsys.readouterr().out

    def test_an_errored_provider_fails_and_is_named(self, checker, tmp_path, capsys):
        """A provider whose audit could not run fails the gate, with its reason."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "erddap", "status": "ok"},
            {"provider": "gee", "status": "error", "detail": "401 Unauthorized"},
        )
        code = checker.main([str(report)])
        out = capsys.readouterr().out
        assert code == 1, f"an errored audit must fail the gate, got {code}"
        assert "gee" in out, f"the errored provider is not named: {out}"
        assert "401 Unauthorized" in out, f"the reason is not surfaced: {out}"
        assert "erddap" not in out, f"a healthy provider was blamed: {out}"

    def test_unsupported_is_not_an_error(self, checker, tmp_path):
        """A provider with no listing endpoint is expected, not a failure."""
        report = _report(
            tmp_path / "a.json", {"provider": "hdx", "status": "unsupported"}
        )
        assert checker.main([str(report)]) == 0, "unsupported must not fail the gate"

    def test_missing_detail_still_reports(self, checker, tmp_path, capsys):
        """An errored provider without a detail still names itself."""
        report = _report(tmp_path / "a.json", {"provider": "gee", "status": "error"})
        assert checker.main([str(report)]) == 1
        assert "no detail given" in capsys.readouterr().out

    def test_malformed_json_fails_rather_than_passing(self, checker, tmp_path, capsys):
        """An unreadable report is a failure, not a silent pass."""
        (tmp_path / "a.json").write_text("not json", encoding="utf-8")
        assert checker.main([str(tmp_path / "a.json")]) == 1, "bad JSON must not pass"
        assert "not valid JSON" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "payload, kind",
        [("{}", "dict"), ('"a string"', "str"), ("[1, 2, 3]", "list")],
        ids=["object", "scalar", "list-of-scalars"],
    )
    def test_valid_json_of_the_wrong_shape_fails(
        self, checker, tmp_path, capsys, payload, kind
    ):
        """A report that parses but is not a list of records must not pass.

        `{}` would otherwise report "0 provider(s) audited" and exit 0 - a green
        gate that verified nothing - and a list of scalars would raise
        AttributeError, reading as a bug in the checker.
        """
        report = tmp_path / "a.json"
        report.write_text(payload, encoding="utf-8")
        assert checker.main([str(report)]) == 1, f"{kind} payload must fail"
        assert "not a list of provider records" in capsys.readouterr().out

    def test_an_empty_list_is_a_legitimate_pass(self, checker, tmp_path):
        """No providers selected is empty, not malformed."""
        report = tmp_path / "a.json"
        report.write_text("[]", encoding="utf-8")
        assert checker.main([str(report)]) == 0, "an empty list must pass"

    def test_missing_report_defers_to_the_caller(self, checker, tmp_path):
        """With no report the audit's own exit code already governs."""
        assert checker.main([str(tmp_path / "absent.json")]) == 0, (
            "must not invent a failure"
        )

    @pytest.mark.parametrize("argv", [[], ["a", "b"]])
    def test_wrong_argument_count_is_a_usage_error(self, checker, argv, capsys):
        """Anything but a single report path exits 2 with usage on stderr."""
        assert checker.main(argv) == 2, f"expected usage exit for {argv!r}"
        assert "usage:" in capsys.readouterr().err

    def test_mixed_report_counts_audited_and_unsupported(
        self, checker, tmp_path, capsys
    ):
        """The summary line tallies the audited providers apart from the rest."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "erddap", "status": "ok"},
            {"provider": "hdx", "status": "unsupported"},
        )
        assert checker.main([str(report)]) == 0, "a mixed report must pass"
        assert "1 provider(s) audited, 1 unsupported" in capsys.readouterr().out, (
            "the audited/unsupported tally is wrong"
        )

    def test_an_empty_report_passes(self, checker, tmp_path, capsys):
        """A report listing no provider at all is not turned into a failure here."""
        report = _report(tmp_path / "a.json")
        assert checker.main([str(report)]) == 0, "an empty report must not fail"
        assert "0 provider(s) audited" in capsys.readouterr().out, (
            "an empty report should still print its tally"
        )

    def test_every_errored_provider_is_named(self, checker, tmp_path, capsys):
        """Two failed audits are both reported, not only the first."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "gee", "status": "error", "detail": "401"},
            {"provider": "cmems", "status": "error", "detail": "timed out"},
        )
        assert checker.main([str(report)]) == 1, "errored providers must fail the gate"
        out = capsys.readouterr().out
        assert "gee" in out, f"gee is not named among the failures: {out}"
        assert "cmems" in out, f"cmems is not named among the failures: {out}"
        assert out.count("::error::") == 2, f"expected one annotation each: {out}"

    def test_a_row_without_a_provider_is_still_reported(
        self, checker, tmp_path, capsys
    ):
        """A row missing its provider name reports a placeholder rather than crashing."""
        report = _report(tmp_path / "a.json", {"status": "error", "detail": "boom"})
        assert checker.main([str(report)]) == 1, "an errored row must fail the gate"
        assert "::error::?:" in capsys.readouterr().out, (
            "the placeholder name is missing"
        )
