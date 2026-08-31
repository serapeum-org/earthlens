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


class TestLooksTransient:
    """Tests for the module-private `_looks_transient` helper."""

    def test_every_declared_marker_is_recognised(self, checker):
        """Each entry in `_TRANSIENT_MARKERS` is honoured, not only the spelled-out ones."""
        for marker in checker._TRANSIENT_MARKERS:
            detail = f"the provider replied: {marker} while listing datasets"
            assert checker._looks_transient(detail), f"{marker!r} was not recognised"

    @pytest.mark.parametrize(
        "detail",
        ["Connection reset by peer", "CONNECTION RESET BY PEER", "Timed Out"],
        ids=["mixed", "upper", "title"],
    )
    def test_matching_ignores_case(self, checker, detail):
        """Upstream wording varies in case, so the match is case-insensitive."""
        assert checker._looks_transient(detail), f"{detail!r} should read as transient"

    @pytest.mark.parametrize(
        "detail",
        ["", "401 Unauthorized", "dataset 'x' is no longer served", "404 Not Found"],
        ids=["empty", "unauthorized", "gone", "not-found"],
    )
    def test_anything_else_stays_hard(self, checker, detail):
        """An unexplained or contractual failure is not forgiven as transient."""
        assert not checker._looks_transient(detail), f"{detail!r} was excused"


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

    @pytest.mark.parametrize(
        "detail",
        [
            "Connection timed out",
            "502 Bad Gateway",
            "service unavailable",
            "read timeout",
        ],
    )
    def test_a_transient_reach_failure_warns_rather_than_fails(
        self, checker, tmp_path, capsys, detail
    ):
        """One of 26 live services being briefly unreachable is not drift."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "gee", "status": "error", "detail": detail},
        )
        assert checker.main([str(report)]) == 0, f"{detail!r} should warn, not fail"
        out = capsys.readouterr().out
        assert "::warning::" in out, f"expected a warning for {detail!r}: {out}"
        assert "::error::" not in out, f"a transient failure was escalated: {out}"

    @pytest.mark.parametrize(
        "detail", ["401 Unauthorized", "no such provider", "invalid key"]
    )
    def test_a_hard_failure_still_fails(self, checker, tmp_path, capsys, detail):
        """A configuration or contract failure is not forgiven."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "gee", "status": "error", "detail": detail},
        )
        assert checker.main([str(report)]) == 1, f"{detail!r} should fail the gate"
        assert "::error::" in capsys.readouterr().out

    def test_a_hard_failure_beside_a_transient_one_still_fails(
        self, checker, tmp_path, capsys
    ):
        """A forgivable error does not mask an unforgivable one, and both are printed."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "gee", "status": "error", "detail": "timed out"},
            {"provider": "cmems", "status": "error", "detail": "401 Unauthorized"},
        )
        assert checker.main([str(report)]) == 1, "a hard failure must still fail"
        out = capsys.readouterr().out
        assert "::warning::gee" in out, f"the transient row lost its warning: {out}"
        assert "::error::cmems" in out, f"the hard row lost its error: {out}"
        assert "::error::gee" not in out, f"the transient row was escalated: {out}"

    def test_a_transient_only_report_tallies_the_unreachable_separately(
        self, checker, tmp_path, capsys
    ):
        """A provider that timed out is neither audited nor unsupported; it is its own count.

        Counting it as unsupported would overstate how much of the catalogue
        was actually checked.
        """
        report = _report(
            tmp_path / "a.json",
            {"provider": "erddap", "status": "ok"},
            {"provider": "gee", "status": "error", "detail": "504 Gateway Timeout"},
        )
        assert checker.main([str(report)]) == 0, "a transient-only report must pass"
        out = capsys.readouterr().out
        assert "::warning::gee" in out, f"the unreachable provider is not named: {out}"
        assert "1 provider(s) audited, 0 unsupported, 1 unreachable this run" in out, (
            f"the transient row was not tallied on its own: {out}"
        )

    def test_a_report_with_nothing_unreachable_omits_that_clause(
        self, checker, tmp_path, capsys
    ):
        """The unreachable count appears only when there is one to report."""
        report = _report(tmp_path / "a.json", {"provider": "erddap", "status": "ok"})
        assert checker.main([str(report)]) == 0, "an all-ok report must pass"
        out = capsys.readouterr().out
        assert "unreachable" not in out, (
            f"an empty unreachable clause was printed: {out}"
        )

    @pytest.mark.parametrize(
        "detail, expected",
        [(502, 0), (None, 1), (True, 1)],
        ids=["int", "null", "bool"],
    )
    def test_a_non_string_detail_does_not_crash(
        self, checker, tmp_path, detail, expected
    ):
        """`detail` is any JSON scalar, so it is coerced rather than assumed text.

        The shape guard only establishes that rows are mappings; a numeric
        detail previously reached `.lower()` and raised.
        """
        report = _report(
            tmp_path / "a.json",
            {"provider": "gee", "status": "error", "detail": detail},
        )
        assert checker.main([str(report)]) == expected, f"detail={detail!r} mishandled"

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
            # Both hard failures: a transient detail would be a warning, which
            # the transient/hard split covers separately.
            {"provider": "gee", "status": "error", "detail": "401 Unauthorized"},
            {"provider": "cmems", "status": "error", "detail": "invalid key"},
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

    def test_a_variable_audit_error_fails_the_gate(self, checker, tmp_path, capsys):
        """A row that passed the id audit but errored its variable fetch fails the gate.

        The id-level audit can be `ok` while the variable dimension could not run
        (`variable_status="error"`); without this the gate would go green having
        verified no variable drift.
        """
        report = _report(
            tmp_path / "a.json",
            {
                "provider": "erddap",
                "status": "ok",
                "variable_status": "error",
                "variable_detail": "404 Not Found",
            },
        )
        assert checker.main([str(report)]) == 1, "a variable-audit error must fail"
        out = capsys.readouterr().out
        assert "::error::erddap: variable audit could not run" in out, (
            f"the variable-audit error is not surfaced: {out}"
        )
        assert "404 Not Found" in out, f"the reason is not surfaced: {out}"

    def test_a_transient_variable_audit_error_warns_rather_than_fails(
        self, checker, tmp_path, capsys
    ):
        """A briefly-unreachable variable fetch warns, mirroring the id-level rule."""
        report = _report(
            tmp_path / "a.json",
            {
                "provider": "erddap",
                "status": "ok",
                "variable_status": "error",
                "variable_detail": "503 service unavailable",
            },
        )
        assert checker.main([str(report)]) == 0, "a transient variable error must warn"
        out = capsys.readouterr().out
        assert "::warning::erddap: variable audit could not reach" in out, (
            f"expected a transient variable warning: {out}"
        )
        assert "::error::" not in out, (
            f"a transient variable error was escalated: {out}"
        )

    def test_a_variable_audit_ok_row_passes(self, checker, tmp_path):
        """A row whose variable audit ran cleanly does not fail the gate."""
        report = _report(
            tmp_path / "a.json",
            {"provider": "erddap", "status": "ok", "variable_status": "ok"},
        )
        assert checker.main([str(report)]) == 0, "a clean variable audit must pass"

    def test_a_non_dds_variable_body_warns_like_a_503(self, checker, tmp_path, capsys):
        """A 200 maintenance/interstitial body ("did not return a DDS") is transient.

        The same server answering 503 only warns, so a 200 holding page must not
        fail the gate harder than a 503 would.
        """
        report = _report(
            tmp_path / "a.json",
            {
                "provider": "erddap",
                "status": "ok",
                "variable_status": "error",
                "variable_detail": (
                    "cwwcNDBCMet: https://x/erddap/tabledap/cwwcNDBCMet.dds "
                    "did not return a DDS"
                ),
            },
        )
        assert checker.main([str(report)]) == 0, (
            "a maintenance page must warn, not fail"
        )
        out = capsys.readouterr().out
        assert "::warning::erddap: variable audit could not reach" in out, (
            f"the non-DDS body was not treated as transient: {out}"
        )
        assert "::error::" not in out, f"a non-DDS body was escalated: {out}"
