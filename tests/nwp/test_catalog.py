"""Unit tests for the NWP catalog loader."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.nwp import Catalog, NWPModel
from earthlens.nwp import catalog as catalog_mod
from earthlens.nwp.catalog import KNOWN_BACKENDS, clear_catalog_cache

pytestmark = [pytest.mark.nwp, pytest.mark.unit]

_CATALOG = """
datasets:
  gfs:
    provider: noaa-nodd
    model_family: gfs
    cycles_utc: [0, 6, 12, 18]
    horizon_h: 384
    backend: herbie
    mirrors: [aws, google]
    bands:
      temperature_2m: ":TMP:2 m above ground:"
  icon-global:
    provider: dwd-opendata
    backend: direct-https
    horizon_h: 180
    url_template: "https://x/{var}.bz2"
    bands:
      temperature_2m: T_2M
"""


def _write_catalog(tmp_path, body=_CATALOG):
    """Write a catalog YAML under tmp_path and return its path."""
    path = tmp_path / "nwp_data_catalog.yaml"
    path.write_text(body)
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts and ends with a clean parse cache."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestCatalogLoad:
    """Tests for Catalog construction and the bundled / on-disk load."""

    def test_bundled_catalog_has_five_models(self):
        """The shipped catalog resolves the five MVP models."""
        models = sorted(Catalog().datasets)
        assert models == ["gefs", "gfs", "hrrr", "icon-global", "ifs-hres"], models

    def test_load_from_disk(self, tmp_path, monkeypatch):
        """A monkey-patched CATALOG_PATH is parsed into typed rows."""
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path))
        cat = Catalog()
        assert set(cat.datasets) == {"gfs", "icon-global"}, cat.datasets

    def test_injected_datasets_skip_disk(self):
        """Passing datasets= bypasses the disk read entirely."""
        cat = Catalog(datasets={"x": NWPModel(provider="p")})
        assert list(cat.datasets) == ["x"], cat.datasets

    def test_second_load_hits_cache(self):
        """A second construction reuses the cached parse for the same file."""
        first = sorted(Catalog().datasets)
        second = sorted(Catalog().datasets)
        assert first == second == ["gefs", "gfs", "hrrr", "icon-global", "ifs-hres"]

    def test_empty_datasets_block_raises(self, tmp_path, monkeypatch):
        """A YAML with no datasets: block raises ValueError."""
        monkeypatch.setattr(
            catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path, "datasets:\n")
        )
        with pytest.raises(ValueError, match="empty 'datasets:'"):
            Catalog()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A row with an unknown field surfaces a validation ValueError."""
        body = "datasets:\n  bad:\n    provider: p\n    nope: 1\n"
        monkeypatch.setattr(
            catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path, body)
        )
        with pytest.raises(ValueError, match="failed validation"):
            Catalog()


class TestCatalogResolve:
    """Tests for get_model / resolve / get_catalog and did-you-mean."""

    def test_get_model(self):
        """get_model returns the typed row for a known key."""
        model = Catalog().get_model("gfs")
        assert model.backend == "herbie"
        assert model.bands["temperature_2m"] == ":TMP:2 m above ground:"

    def test_resolve_is_get_model(self):
        """resolve is an alias returning the same row as get_model."""
        cat = Catalog()
        assert cat.resolve("hrrr") is cat.get_model("hrrr")

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same mapping as .datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_unknown_key_did_you_mean(self):
        """An unknown key raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'gfs'"):
            Catalog().get_model("gffs")


class TestNWPModel:
    """Tests for the NWPModel row model."""

    def test_defaults(self):
        """Optional fields fall back to documented defaults."""
        model = NWPModel(provider="p")
        assert model.backend == "herbie"
        assert model.format == "grib2"
        assert model.idx is True
        assert model.cycles_utc == [] and model.bands == {}

    def test_extra_forbidden(self):
        """An unexpected field is rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            NWPModel(provider="p", bogus=1)

    def test_known_backends_membership(self):
        """Every backend literal is listed in KNOWN_BACKENDS."""
        for backend in ("herbie", "ecmwf-opendata", "direct-https", "direct-boto3"):
            assert backend in KNOWN_BACKENDS


class TestClearCache:
    """Tests for clear_catalog_cache."""

    def test_clear_forces_reparse(self, tmp_path, monkeypatch):
        """Clearing the cache picks up an on-disk edit between loads."""
        path = _write_catalog(tmp_path)
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", path)
        assert set(Catalog().datasets) == {"gfs", "icon-global"}
        path.write_text("datasets:\n  only:\n    provider: p\n")
        clear_catalog_cache()
        assert set(Catalog().datasets) == {"only"}
