"""Unit tests for the NEXRAD station catalog."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.radar import Station, StationCatalog
from earthlens.radar import catalog as catalog_mod
from earthlens.radar.catalog import clear_catalog_cache

pytestmark = [pytest.mark.radar, pytest.mark.unit]

_YAML = """
stations:
  KTLX: {name: "Oklahoma City, OK", latitude: 35.3331, longitude: -97.2778, state: OK}
  KFWS: {name: "Dallas/Fort Worth, TX", latitude: 32.5731, longitude: -97.3031, state: TX}
"""


@pytest.fixture(autouse=True)
def _clear():
    """Each test starts and ends with a clean parse cache."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestStationCatalog:
    """Tests for StationCatalog loading + lookup."""

    def test_bundled_has_ktlx(self):
        """The shipped catalog resolves KTLX with its published location."""
        ktlx = StationCatalog().get_station("KTLX")
        assert ktlx.state == "OK"
        assert round(ktlx.latitude, 2) == 35.33 and round(ktlx.longitude, 2) == -97.28

    def test_load_from_disk(self, tmp_path, monkeypatch):
        """A monkey-patched CATALOG_PATH is parsed into typed rows."""
        p = tmp_path / "stations.yaml"
        p.write_text(_YAML)
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", p)
        cat = StationCatalog()
        assert set(cat.datasets) == {"KTLX", "KFWS"}

    def test_second_load_hits_cache(self):
        """A second construction reuses the cached parse."""
        first = sorted(StationCatalog().datasets)
        second = sorted(StationCatalog().datasets)
        assert first == second and "KTLX" in first

    def test_unknown_station_did_you_mean(self):
        """An unknown id raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'KTLX'"):
            StationCatalog().get_station("KTLZ")

    def test_in_bbox(self):
        """in_bbox returns the sites inside the box, sorted."""
        hits = StationCatalog().in_bbox(-100, 33, -95, 37)
        assert "KTLX" in hits and hits == sorted(hits)
        assert "KAMX" not in hits  # Miami is well outside

    def test_empty_block_raises(self, tmp_path, monkeypatch):
        """A YAML with no stations: block raises ValueError."""
        p = tmp_path / "stations.yaml"
        p.write_text("stations:\n")
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", p)
        with pytest.raises(ValueError, match="empty 'stations:'"):
            StationCatalog()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A station row with a bad field surfaces a validation ValueError."""
        p = tmp_path / "stations.yaml"
        p.write_text("stations:\n  KBAD: {latitude: 999, longitude: 0}\n")
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", p)
        with pytest.raises(ValueError, match="failed validation"):
            StationCatalog()

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same mapping as .datasets."""
        cat = StationCatalog()
        assert cat.get_catalog() is cat.datasets


class TestStation:
    """Tests for the Station row model."""

    def test_extra_forbidden(self):
        """An unexpected field is rejected."""
        with pytest.raises(ValidationError):
            Station(latitude=0, longitude=0, bogus=1)

    def test_latitude_range_enforced(self):
        """An out-of-range latitude is rejected."""
        with pytest.raises(ValidationError):
            Station(latitude=999, longitude=0)
