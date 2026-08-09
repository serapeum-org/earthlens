"""Unit tests for the Aqueduct helper functions (column grammar, zip IO)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from earthlens.aqueduct import AdminLevel, Catalog, _helpers
from earthlens.base import SpatialExtent

pytestmark = pytest.mark.aqueduct


def test_resolve_columns_builds_expected_names() -> None:
    """A metric/year/scenario/rp selection resolves to the .dbf column names."""
    columns = _helpers.resolve_columns(
        Catalog(), "population_affected", "2030", "ssp2-rcp8p5", [2, 1000]
    )
    assert columns == {2: "P30_28_2", 1000: "P30_28_1T"}


def test_resolve_columns_unknown_metric_raises() -> None:
    """An unknown metric is rejected."""
    with pytest.raises(ValueError, match="metric"):
        _helpers.resolve_columns(Catalog(), "deaths", "2010", "baseline", [100])


def test_resolve_columns_unknown_year_raises() -> None:
    """An unknown year is rejected."""
    with pytest.raises(ValueError, match="year"):
        _helpers.resolve_columns(
            Catalog(), "population_affected", "2099", "baseline", [100]
        )


def test_resolve_columns_unknown_scenario_raises() -> None:
    """An entirely unknown scenario name is rejected."""
    with pytest.raises(ValueError, match="scenario 'nope' is not"):
        _helpers.resolve_columns(
            Catalog(), "population_affected", "2030", "nope", [100]
        )


def test_resolve_columns_unknown_return_period_raises() -> None:
    """A return period outside the shipped nine is rejected."""
    with pytest.raises(ValueError, match="return_period"):
        _helpers.resolve_columns(
            Catalog(), "population_affected", "2010", "baseline", [3]
        )


def _zip_with(tmp_path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip containing the named members and return its path."""
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_extract_shapefile_missing_inner_zip_raises(tmp_path: Path) -> None:
    """A nested level whose container lacks the inner zip raises."""
    zip_path = _zip_with(tmp_path, {"other.zip": b"PK\x03\x04"})
    row = AdminLevel(zip="inner.zip", shapefile_stem="s", container_zip="bundle.zip")
    with pytest.raises(FileNotFoundError, match="inner.zip"):
        _helpers.extract_shapefile(zip_path, row, tmp_path / "out")


def test_extract_shapefile_missing_shp_member_raises(tmp_path: Path) -> None:
    """A zip with sidecars but no .shp raises."""
    zip_path = _zip_with(tmp_path, {"s.dbf": b"x", "s.shx": b"y"})
    row = AdminLevel(zip="s.zip", shapefile_stem="s")
    with pytest.raises(FileNotFoundError, match="s.shp"):
        _helpers.extract_shapefile(zip_path, row, tmp_path / "out")


def test_is_global_true_for_whole_globe() -> None:
    """The whole-globe extent applies no bbox filter."""
    space = SpatialExtent.from_pairs(lat_lim=[-90, 90], lon_lim=[-180, 180])
    assert _helpers._is_global(space) is True


def test_is_global_false_for_a_box() -> None:
    """A narrower box is not global."""
    space = SpatialExtent.from_pairs(lat_lim=[0, 10], lon_lim=[0, 10])
    assert _helpers._is_global(space) is False
