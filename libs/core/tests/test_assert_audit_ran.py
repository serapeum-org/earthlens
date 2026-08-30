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
        assert "gee" in out and "401 Unauthorized" in out, f"reason not surfaced: {out}"
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
