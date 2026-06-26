"""Unit tests for the climate-indices catalogue loader."""

from __future__ import annotations

import pytest

from earthlens.climate_indices import Catalog, Index


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
