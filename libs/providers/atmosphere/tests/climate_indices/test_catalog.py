"""Unit tests for the climate-indices catalogue loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.climate_indices import Catalog, Index
from earthlens.climate_indices import catalog as catalog_mod

pytestmark = pytest.mark.climate_indices


def _write_yaml(tmp_path: Path, body: str) -> Path:
    """Write a catalog YAML to a temp file and clear the parse cache."""
    path = tmp_path / "cat.yaml"
    path.write_text(body)
    catalog_mod.clear_catalog_cache()
    return path


def test_catalog_loads_and_lists_indices() -> None:
    """The bundled catalog loads and lists the shipped index ids."""
    cat = Catalog()
    available = cat.available()
    assert available == sorted(available)
    assert {"oni", "nao", "amo"}.issubset(set(available))


def test_get_returns_typed_row() -> None:
    """get() resolves an id to a typed Index with a joined URL."""
    row = Catalog().get("oni")
    assert isinstance(row, Index)
    assert row.source == "noaa-psl"
    assert row.dialect == "psl"
    assert row.url == "https://psl.noaa.gov/data/correlation/oni.data"


def test_climexp_row_url_and_citation() -> None:
    """A climexp row joins its base URL and carries the source citation."""
    row = Catalog().get("amo")
    assert row.source == "knmi-climexp"
    assert row.dialect == "climexp"
    assert row.url == "https://climexp.knmi.nl/data/iamo_ersst.dat"
    assert "Climate Explorer" in row.citation


def test_catalog_integrity_every_row_well_formed() -> None:
    """Every row has a valid source, dialect, and a non-empty URL."""
    for index_id, row in Catalog().datasets.items():
        assert row.source in {"noaa-psl", "knmi-climexp"}, index_id
        assert row.dialect in {"psl", "climexp"}, index_id
        assert row.url.startswith("https://"), index_id
        assert row.citation, index_id


def test_get_unknown_id_raises_did_you_mean() -> None:
    """An unknown but close id raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'nao'"):
        Catalog().get("noo")


def test_load_missing_file_raises() -> None:
    """Loading a non-existent catalog path raises FileNotFoundError."""
    catalog_mod.clear_catalog_cache()
    with pytest.raises(FileNotFoundError):
        Catalog.load(Path("does-not-exist-climate-indices.yaml"))


def test_empty_datasets_block_raises(tmp_path: Path) -> None:
    """A catalog with no datasets block raises a clear ValueError."""
    path = _write_yaml(tmp_path, "sources:\n  noaa-psl:\n    base_url: https://x/\n")
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(path)


def test_unknown_source_raises(tmp_path: Path) -> None:
    """A row naming an undeclared source raises a ValueError."""
    body = (
        "sources:\n  noaa-psl:\n    base_url: https://x/\n    citation: c\n"
        "datasets:\n  foo:\n    source: nope\n    dialect: psl\n    file: foo.data\n"
    )
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="unknown source"):
        Catalog.load(path)


def test_malformed_row_raises(tmp_path: Path) -> None:
    """A row with an invalid dialect fails validation with a ValueError."""
    body = (
        "sources:\n  noaa-psl:\n    base_url: https://x/\n    citation: c\n"
        "datasets:\n  foo:\n    source: noaa-psl\n    dialect: bogus\n    file: foo.data\n"
    )
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(path)


def test_catalog_cache_reuses_parsed_rows() -> None:
    """A second load of the bundled catalog returns the cached rows."""
    catalog_mod.clear_catalog_cache()
    first = Catalog().datasets
    second = Catalog().datasets
    assert first.keys() == second.keys()
