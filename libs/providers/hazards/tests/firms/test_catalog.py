"""Tests for the FIRMS sensor catalog loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from earthlens.firms import Catalog, Sensor, SensorColumn
from earthlens.firms.catalog import Temporal

pytestmark = pytest.mark.firms


def test_bundled_catalog_loads():
    """The default Catalog() loads the bundled YAML with every live sensor."""
    cat = Catalog()
    assert cat.codes() == [
        "GOES_NRT",
        "LANDSAT_NRT",
        "MODIS_NRT",
        "MODIS_SP",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA20_SP",
        "VIIRS_NOAA21_NRT",
        "VIIRS_SNPP_NRT",
        "VIIRS_SNPP_SP",
    ]


def test_get_sensor_resolves_metadata():
    """get_sensor returns the family, resolution, and column map."""
    sensor = Catalog().get_sensor("VIIRS_SNPP_NRT")
    assert sensor.family == "VIIRS"
    assert sensor.resolution_m == 375
    assert "bright_ti4" in sensor.columns


def test_modis_confidence_is_percent_viirs_is_dimensionless():
    """The confidence column units differ by sensor family."""
    cat = Catalog()
    assert cat.get_column("MODIS_NRT", "confidence").units == "%"
    assert cat.get_column("VIIRS_SNPP_NRT", "confidence").units == "1"


def test_contains_and_membership():
    """The dict-like surface is inherited from AbstractCatalog."""
    cat = Catalog()
    assert "MODIS_NRT" in cat
    assert "NOPE" not in cat
    assert len(cat) == 9
    assert "BA_MODIS" not in cat  # burned-area: not an area-CSV source


def test_unknown_sensor_raises_did_you_mean():
    """A near-miss code raises ValueError naming the closest sensor."""
    with pytest.raises(ValueError, match="Did you mean 'MODIS_NRT'"):
        Catalog().get_sensor("MODIS_NR")


def test_unknown_column_raises_keyerror():
    """An undeclared column raises KeyError."""
    with pytest.raises(KeyError):
        Catalog().get_column("MODIS_NRT", "not_a_column")


def test_temporal_quality_recorded():
    """Each sensor records its NRT/SP quality tier and coverage start."""
    cat = Catalog()
    assert cat.get_sensor("VIIRS_SNPP_NRT").temporal.quality == "NRT"
    assert cat.get_sensor("VIIRS_SNPP_SP").temporal.quality == "SP"
    assert cat.get_sensor("MODIS_NRT").temporal.end is None


def test_extra_keys_rejected(tmp_path: Path):
    """A typo'd top-level sensor key is rejected by extra='forbid'."""
    bad = tmp_path / "firms.yaml"
    bad.write_text(textwrap.dedent("""
            sensors:
              MODIS_NRT:
                code: MODIS_NRT
                family: MODIS
                resolution_m: 1000
                typo_field: oops
            """).strip())
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)


def test_missing_sensors_block_raises(tmp_path: Path):
    """A YAML with no sensors: block fails loud."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("other: {}\n")
    with pytest.raises(ValueError, match="missing or has an empty 'sensors:'"):
        Catalog.load(empty)


def test_goes_and_landsat_families():
    """GOES and LANDSAT sensors carry their own family + resolution."""
    cat = Catalog()
    assert cat.get_sensor("GOES_NRT").family == "GOES"
    assert cat.get_sensor("GOES_NRT").resolution_m == 2000
    landsat = cat.get_sensor("LANDSAT_NRT")
    assert landsat.family == "LANDSAT"
    assert landsat.resolution_m == 30
    # Landsat carries no FRP / brightness columns.
    assert "frp" not in landsat.columns
    assert "bright_ti4" not in landsat.columns


def test_burned_area_sources_excluded():
    """Burned-area data_ids are not catalogued (not area-CSV sources)."""
    cat = Catalog()
    for code in ("BA_MODIS", "BA_VIIRS"):
        assert code not in cat
        with pytest.raises(ValueError):
            cat.get_sensor(code)


def test_noaa20_sp_archive():
    """VIIRS_NOAA20_SP is the archive twin with the VIIRS schema."""
    sensor = Catalog().get_sensor("VIIRS_NOAA20_SP")
    assert sensor.family == "VIIRS"
    assert sensor.temporal.quality == "SP"
    assert "bright_ti4" in sensor.columns


def test_get_catalog_returns_datasets():
    """get_catalog() returns the same map as datasets (abstract contract)."""
    cat = Catalog()
    assert cat.get_catalog() is cat.datasets


def test_sensor_built_from_models():
    """A Sensor accepts nested SensorColumn / Temporal value objects."""
    sensor = Sensor(
        code="X",
        family="VIIRS",
        resolution_m=375,
        temporal=Temporal(quality="NRT"),
        columns={"frp": SensorColumn(units="MW", long_name="FRP")},
    )
    assert sensor.columns["frp"].units == "MW"
