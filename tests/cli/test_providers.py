"""Tests for the `earthlens providers …` group."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from earthlens.cli.adapter import BackendInfo, list_backends
from earthlens.cli.app import app
from earthlens.cli.providers import _dataset_count, _sdk_available

pytestmark = pytest.mark.cli

runner = CliRunner()


class TestListProviders:
    """Tests for `providers list`."""

    def test_lists_every_backend(self):
        """The default view shows the provider column and a known backend."""
        result = runner.invoke(app, ["providers", "list"])
        assert result.exit_code == 0, f"providers list failed: {result.output}"
        assert "PROVIDER" in result.output, "header present"
        assert "chc" in result.output, "a known backend is listed"

    def test_json_carries_aliases_and_extra(self):
        """--json emits one record per backend with aliases and extra."""
        result = runner.invoke(app, ["providers", "list", "--json"])
        payload = json.loads(result.output)
        assert len(payload) == len(list_backends()), "one record per backend"
        chc = next(p for p in payload if p["provider"] == "chc")
        assert "chirps" in chc["aliases"], "alias retained"
        assert chc["extra"] == "", "chc is SDK-free"

    def test_fast_path_omits_check_columns(self):
        """Without --check, the SDK / datasets columns are absent."""
        result = runner.invoke(app, ["providers", "list"])
        assert "DATASETS" not in result.output, "no probe columns by default"

    @pytest.mark.slow
    def test_check_adds_probe_columns(self):
        """--check imports each backend and adds the SDK / DATASETS columns."""
        result = runner.invoke(app, ["providers", "list", "--check"])
        assert result.exit_code == 0, f"--check failed: {result.output}"
        assert "DATASETS" in result.output, "probe columns shown under --check"


class TestProbeHelpers:
    """Tests for the SDK/catalog probe helpers used by --check."""

    def test_dataset_count_for_loaded_backend(self):
        """A healthy backend reports a positive catalog size."""
        info = next(b for b in list_backends() if b.provider == "chc")
        assert _dataset_count(info) > 0, "chc catalog is non-empty"

    def test_sdk_available_for_sdk_free_backend(self):
        """An SDK-free backend resolves its class without error."""
        info = next(b for b in list_backends() if b.provider == "chc")
        available, detail = _sdk_available(info)
        assert available is True, f"chc should resolve: {detail}"

    def test_sdk_unavailable_is_reported(self):
        """A backend whose registry key cannot resolve reports unavailable.

        Test scenario:
            A crafted BackendInfo with an unregistered alias makes the
            registry lookup raise; the probe must catch it and return a
            `(False, reason)` pair rather than propagating.
        """
        bogus = BackendInfo(
            provider="bogus", module="earthlens.bogus", extra="", aliases=("nope",)
        )
        available, detail = _sdk_available(bogus)
        assert available is False, "unresolved key -> unavailable"
        assert detail, "a reason is captured"
