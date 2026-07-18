"""Smoke tests for the Typer application wiring (`earthlens.cli.app`)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from functools import partial

import pytest
from typer.testing import CliRunner

from earthlens.cli import app as cli_app
from earthlens.cli.app import _provider_backend_hint, app, main

pytestmark = pytest.mark.cli

runner = CliRunner()

# The `app` submodule and the `app` Typer object share a name, so the dotted
# attribute `earthlens.cli.app` resolves to the Typer object; reach the real
# module (to patch its `app` global) through `import_module` instead.
_app_module = importlib.import_module("earthlens.cli.app")


def _raise(exc: BaseException) -> None:
    """Module-level thunk that raises `exc` when called (stands in for `app`)."""
    raise exc


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

    def test_main_runs_the_app(self, monkeypatch):
        """main() drives the app; --help exits cleanly with code 0."""
        monkeypatch.setattr(sys, "argv", ["earthlens", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0, "main() --help exits 0"

    def test_python_m_entrypoint(self):
        """`python -m earthlens.cli --help` runs and prints usage."""
        result = subprocess.run(
            [sys.executable, "-m", "earthlens.cli", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == 0, f"module entrypoint failed: {result.stderr}"
        assert "Usage" in result.stdout, "usage banner printed"

    def test_dunder_main_module_imports_main(self):
        """`earthlens.cli.__main__` re-exports the same entrypoint as the app."""
        import importlib

        dunder_main = importlib.import_module("earthlens.cli.__main__")
        assert dunder_main.main is main, "__main__ wires the app's main()"


class TestProviderBackendHint:
    """Tests for the missing-provider-backend install hint."""

    @pytest.mark.parametrize(
        "missing",
        ["earthlens.ecmwf", "earthlens.nwm", "earthlens.gee", "earthlens.usgs_water"],
    )
    def test_top_level_provider_miss_returns_hint(self, missing):
        """A missing top-level provider package yields a `pip install earthlens` hint."""
        exc = ModuleNotFoundError(f"No module named {missing!r}", name=missing)
        hint = _provider_backend_hint(exc)
        backend = missing.split(".")[1]
        assert hint is not None, "top-level provider miss should produce a hint"
        assert "pip install earthlens" in hint, "hint recommends the meta package"
        assert backend in hint, "hint names the missing backend"

    @pytest.mark.parametrize(
        "missing",
        [
            "earthlens.ecmwf.catalog",  # provider installed, its own import broke
            "earthlens.gee.auth",
            "earthlens.base",  # core module
            "earthlens.core",
            "earthlens",  # bare namespace
            "cdsapi",  # third-party SDK
            "boto3",
            None,
            "",
        ],
    )
    def test_deep_core_or_sdk_miss_returns_none(self, missing):
        """A deeper (provider-internal), core, or third-party SDK miss is not rewritten."""
        exc = ModuleNotFoundError("boom", name=missing)
        assert _provider_backend_hint(exc) is None, "only top-level provider misses rewrite"


class TestMainGuard:
    """Tests for main()'s missing-provider rewrite."""

    def test_provider_miss_prints_hint_and_exits_one(self, monkeypatch, capsys):
        """main() turns a missing top-level provider into a friendly hint and exit 1."""
        exc = ModuleNotFoundError("No module named 'earthlens.ecmwf'", name="earthlens.ecmwf")
        monkeypatch.setattr(_app_module, "app", partial(_raise, exc))
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1, "provider miss exits non-zero"
        err = capsys.readouterr().err
        assert "pip install earthlens" in err and "ecmwf" in err, "hint printed to stderr"

    def test_deep_provider_miss_propagates(self, monkeypatch):
        """main() re-raises an installed provider's own import fault, not a fake hint."""
        exc = ModuleNotFoundError(
            "No module named 'earthlens.ecmwf.catalog'", name="earthlens.ecmwf.catalog"
        )
        monkeypatch.setattr(_app_module, "app", partial(_raise, exc))
        with pytest.raises(ModuleNotFoundError) as exc_info:
            main()
        assert exc_info.value.name == "earthlens.ecmwf.catalog", "deep miss is not rewritten"

    def test_sdk_miss_propagates(self, monkeypatch):
        """main() re-raises a missing backend SDK unchanged (not a provider distribution)."""
        exc = ModuleNotFoundError("No module named 'cdsapi'", name="cdsapi")
        monkeypatch.setattr(_app_module, "app", partial(_raise, exc))
        with pytest.raises(ModuleNotFoundError) as exc_info:
            main()
        assert exc_info.value.name == "cdsapi", "unrelated import error is not rewritten"
