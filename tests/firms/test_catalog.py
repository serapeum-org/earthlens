"""Tests for the FIRMS sensor catalog loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from earthlens.firms import Catalog, Sensor, SensorColumn
from earthlens.firms.catalog import Temporal


def test_bundled_catalog_loads():
    """The default Catalog() loads the bundled YAML with all six sensors."""
    cat = Catalog()
    assert cat.codes() == [
        "MODIS_NRT",
        "MODIS_SP",
        "VIIRS_NOAA20_NRT",
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
    assert len(cat) == 6


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
    bad.write_text(
        textwrap.dedent(
            """
            sensors:
              MODIS_NRT:
                code: MODIS_NRT
                family: MODIS
                resolution_m: 1000
                typo_field: oops
            """
        ).strip()
    )
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)


def test_missing_sensors_block_raises(tmp_path: Path):
    """A YAML with no sensors: block fails loud."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("other: {}\n")
    with pytest.raises(ValueError, match="missing or has an empty 'sensors:'"):
        Catalog.load(empty)


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
