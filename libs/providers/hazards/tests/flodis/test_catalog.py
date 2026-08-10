"""Unit tests for the FLODIS catalog loader and its row models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.flodis import Catalog, FlodisDataset, ZenodoRecord
from earthlens.flodis import catalog as catalog_module

_GOOD_YAML = """\
record:
  record: 8123096
  concept_doi: "10.5281/zenodo.8123096"
  data_period: "2000-2018"
  license: CC-BY-4.0
  attribution: Mester et al. 2023.
datasets:
  damages:
    file: FLODIS_mortality_damage.csv
    description: EM-DAT deaths/damages.
    key_columns: [disasterno]
  displacement:
    file: FLODIS_displacement.csv
    description: IDMC displacements.
    key_columns: [GID_1, GID_2]
columns:
  iso3: ISO3
  year: year
"""


def _write(tmp_path: Path, text: str) -> Path:
    """Write catalog YAML text to a temp file and return its path."""
    path = tmp_path / "flodis_data_catalog.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestZenodoRecord:
    """Tests for the ZenodoRecord row model."""

    def test_fields(self) -> None:
        """The record carries the pinned id, licence and data period."""
        record = ZenodoRecord(
            record=8123096, license="CC-BY-4.0", data_period="2000-2018"
        )
        assert record.record == 8123096
        assert record.license == "CC-BY-4.0"
        assert record.data_period == "2000-2018"

    def test_frozen(self) -> None:
        """The record is immutable."""
        record = ZenodoRecord(record=1)
        with pytest.raises(ValidationError):
            record.record = 2  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        """An unknown field is rejected."""
        with pytest.raises(ValidationError):
            ZenodoRecord(record=1, bogus="x")  # type: ignore[call-arg]


class TestFlodisDataset:
    """Tests for the FlodisDataset row model."""

    def test_content_url(self) -> None:
        """The content URL is composed from the record id and file name."""
        row = FlodisDataset(file="FLODIS_displacement.csv")
        assert row.content_url(8123096) == (
            "https://zenodo.org/api/records/8123096/files/FLODIS_displacement.csv/content"
        )

    def test_key_columns_coerced_to_tuple(self) -> None:
        """A YAML list of key columns is stored as a tuple."""
        row = FlodisDataset(file="x.csv", key_columns=["GID_1", "GID_2"])
        assert row.key_columns == ("GID_1", "GID_2")


class TestCatalogLoad:
    """Tests for reading the bundled and custom FLODIS catalogs."""

    def test_bundled_tables(self) -> None:
        """The bundled catalog exposes the two tables, sorted."""
        assert Catalog().tables() == ["damages", "displacement"]

    def test_bundled_record(self) -> None:
        """The bundled catalog pins Zenodo record 8123096 under CC-BY-4.0."""
        record = Catalog().record
        assert record is not None
        assert record.record == 8123096
        assert record.license == "CC-BY-4.0"

    def test_dataset_resolves(self) -> None:
        """A known table name resolves to its FlodisDataset row."""
        row = Catalog().dataset("damages")
        assert row.file == "FLODIS_mortality_damage.csv"
        assert row.key_columns == ("disasterno",)

    def test_dataset_unknown_hints(self) -> None:
        """An unknown table raises with a did-you-mean hint."""
        cat = Catalog()
        with pytest.raises(ValueError, match="Did you mean 'damages'"):
            cat.dataset("damage")

    def test_column_maps_header(self) -> None:
        """A friendly key maps to its exact FLODIS header."""
        assert Catalog().column("total_damages_000_usd") == "total_damages_(000_USD)"

    def test_column_unknown_raises(self) -> None:
        """An unmapped friendly key raises KeyError."""
        cat = Catalog()
        with pytest.raises(KeyError):
            cat.column("nope")

    def test_dict_surface(self) -> None:
        """The catalog exposes the dict surface over its tables."""
        cat = Catalog()
        assert "damages" in cat
        assert len(cat) == 2
        assert cat["displacement"].file == "FLODIS_displacement.csv"

    def test_load_from_path(self, tmp_path: Path) -> None:
        """A custom catalog path loads through the shared loader."""
        catalog_module.clear_catalog_cache()
        cat = Catalog.load(_write(tmp_path, _GOOD_YAML))
        assert cat.tables() == ["damages", "displacement"]

    def test_get_catalog_is_datasets(self) -> None:
        """get_catalog returns the same mapping as datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets


class TestParseCatalog:
    """Tests for the _parse_catalog error branches."""

    def test_missing_record(self, tmp_path: Path) -> None:
        """A catalog with no record block is rejected."""
        text = (
            "datasets:\n  damages:\n    file: a.csv\n  displacement:\n    file: b.csv\n"
        )
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match="'record:' block"):
            catalog_module._parse_catalog([path])

    def test_missing_datasets(self, tmp_path: Path) -> None:
        """A catalog with no datasets block is rejected."""
        text = "record:\n  record: 1\n"
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match="'datasets:' block"):
            catalog_module._parse_catalog([path])

    def test_missing_required_table(self, tmp_path: Path) -> None:
        """A datasets block missing a required table is rejected."""
        text = "record:\n  record: 1\ndatasets:\n  damages:\n    file: a.csv\n"
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match="missing required table 'displacement'"):
            catalog_module._parse_catalog([path])

    def test_row_validation_error(self, tmp_path: Path) -> None:
        """A row with an unknown field fails validation."""
        text = (
            "record:\n  record: 1\n"
            "datasets:\n"
            "  damages:\n    file: a.csv\n    bogus: 1\n"
            "  displacement:\n    file: b.csv\n"
        )
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match="failed validation"):
            catalog_module._parse_catalog([path])


class TestClearCache:
    """Tests for the module-level parse cache helper."""

    def test_clear_cache_runs(self) -> None:
        """Clearing the cache is a no-op that does not raise."""
        catalog_module.clear_catalog_cache()
        assert Catalog().tables() == ["damages", "displacement"]
