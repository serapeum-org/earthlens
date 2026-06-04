"""Smoke tests for the Typer application wiring (`earthlens.cli.app`)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from earthlens.cli import app as cli_app
from earthlens.cli.app import app

pytestmark = pytest.mark.cli

runner = CliRunner()


class TestApp:
    """Tests for the root Typer application."""

    def test_app_is_exported(self):
        """The package re-exports the same app object the script points at."""
        assert cli_app is app, "earthlens.cli.app re-exports the root app"

    def test_root_help_lists_command_groups(self):
        """`earthlens --help` advertises the datasets and providers groups."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0, f"--help failed: {result.output}"
        assert "datasets" in result.output, "datasets group is mounted"
        assert "providers" in result.output, "providers group is mounted"

    def test_no_args_shows_help(self):
        """Invoking with no command shows help (no_args_is_help)."""
        result = runner.invoke(app, [])
        assert "Usage" in result.output, "bare invocation prints usage"

    def test_datasets_group_help(self):
        """`earthlens datasets --help` renders the group's help."""
        result = runner.invoke(app, ["datasets", "--help"])
        assert result.exit_code == 0, f"datasets --help failed: {result.output}"
        assert "datasets" in result.output.lower(), "group help mentions datasets"

    def test_providers_group_help(self):
        """`earthlens providers --help` renders the group's help."""
        result = runner.invoke(app, ["providers", "--help"])
        assert result.exit_code == 0, f"providers --help failed: {result.output}"
