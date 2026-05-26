"""Unit tests for the NWM configuration catalog (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.nwm.catalog import (
    NWMCatalog,
    NWMConfig,
    _load_configs,
    clear_catalog_cache,
)

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the module parse cache before and after each test."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestNWMCatalog:
    """Tests for the bundled-catalog loader."""

    def test_loads_bundled_configs(self):
        """The bundled YAML exposes the curated CONUS configurations."""
        cat = NWMCatalog()
        assert "short_range" in cat.datasets
        assert "medium_range_mem1" in cat.datasets

    def test_get_config_returns_row(self):
        """get_config resolves a known key to its NWMConfig with metadata."""
        sr = NWMCatalog().get_config("short_range")
        assert sr.horizon_h == 18
        assert "channel_rt" in sr.products
        assert sr.domain == "conus"

    def test_get_config_unknown_raises_did_you_mean(self):
        """An unknown key raises ValueError naming the catalog."""
        with pytest.raises(ValueError, match="NWM catalog"):
            NWMCatalog().get_config("shortrange")

    def test_get_catalog_returns_map(self):
        """get_catalog returns the same per-config map as datasets."""
        cat = NWMCatalog()
        assert cat.get_catalog() == cat.datasets

    def test_key_template_formats_to_live_layout(self):
        """The short_range template formats to the verified S3 key layout."""
        import datetime as dt

        sr = NWMCatalog().get_config("short_range")
        cycle = dt.datetime(2026, 5, 25, 0)
        key = sr.key_template.format(
            date=cycle, cycle=cycle, step=1, product="channel_rt"
        )
        assert key == (
            "nwm.20260525/short_range/nwm.t00z.short_range.channel_rt.f001.conus.nc"
        )


class TestLoadConfigs:
    """Tests for the low-level `_load_configs` reader/validator."""

    def test_missing_block_raises(self, tmp_path: Path):
        """A YAML without a configurations block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="configurations"):
            _load_configs(bad)

    def test_invalid_row_raises(self, tmp_path: Path):
        """A row with an unknown field fails validation with a clear message."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "configurations:\n  x:\n    bogus_field: 1\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="failed validation"):
            _load_configs(bad)

    def test_cache_returns_same_object(self, tmp_path: Path):
        """A second load of an unchanged file returns the cached dict."""
        good = tmp_path / "c.yaml"
        good.write_text(
            "configurations:\n  x:\n    horizon_h: 3\n", encoding="utf-8"
        )
        first = _load_configs(good)
        second = _load_configs(good)
        assert first is second

    def test_missing_file_raises(self, tmp_path: Path):
        """A non-existent path raises FileNotFoundError from the YAML loader."""
        with pytest.raises(FileNotFoundError):
            _load_configs(tmp_path / "does_not_exist.yaml")

    def test_explicit_datasets_skip_autoload(self):
        """Supplying datasets= bypasses the bundled-YAML auto-load."""
        custom = {"x": NWMConfig(horizon_h=5)}
        cat = NWMCatalog(datasets=custom)
        assert cat.datasets == custom
        assert cat.get_config("x").horizon_h == 5


class TestNWMConfig:
    """Tests for the NWMConfig row model."""

    def test_defaults(self):
        """An empty row carries safe defaults."""
        cfg = NWMConfig()
        assert cfg.domain == "conus"
        assert cfg.first_step == 1
        assert cfg.products == []

    def test_extra_field_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            NWMConfig(unexpected=1)
