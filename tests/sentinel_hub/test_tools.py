"""Unit tests for the Sentinel Hub catalog tooling (`tools/sentinel_hub/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "sentinel_hub"
sys.path.insert(0, str(_TOOLS_DIR))

import refresh_sh_catalog as refresh  # noqa: E402

pytestmark = pytest.mark.sentinel_hub


class TestValidate:
    """`validate-recipe` / `validate-all` check recipes against the bundled .js."""

    def test_validate_all_bundled_recipes_pass(self):
        """Every curated recipe validates (exit 0)."""
        assert refresh.main(["validate-all"]) == 0

    def test_validate_render_recipe(self):
        """A render recipe validates."""
        assert refresh.main(["validate-recipe", "sentinel-2-l2a-ndvi"]) == 0

    def test_validate_stats_recipe(self):
        """A stats recipe (dataMask) validates."""
        assert refresh.main(["validate-recipe", "sentinel-2-l2a-ndvi-stats"]) == 0

    def test_validate_unknown_recipe_fails(self):
        """An unknown recipe key exits non-zero."""
        assert refresh.main(["validate-recipe", "no-such-recipe"]) == 1

    def test_validate_one_reports_no_problems(self):
        """`_validate_one` returns an empty list for a good recipe."""
        assert refresh._validate_one("sentinel-2-l2a-true-colour") == []


class TestRefresh:
    """`refresh` rebuilds the available-collections index from the Catalog API."""

    def test_dry_run_prints_collections(self, fake_sh, monkeypatch, capsys):
        """`refresh --dry-run` prints the regenerated index without writing."""
        monkeypatch.setenv("SH_CLIENT_ID", "a")
        monkeypatch.setenv("SH_CLIENT_SECRET", "b")
        assert refresh.main(["refresh", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "available_collections:" in out
        assert "sentinel-2-l2a" in out
