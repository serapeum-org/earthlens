"""Tests for the HANZE catalog loader and its row models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.hanze import (
    CATALOG_PATH,
    Catalog,
    FloodType,
    GeometryJoin,
    HanzeFile,
    ZenodoRecord,
)
from earthlens.hanze import catalog as catalog_module

_MINIMAL = """
record:
  record: 999
  version: v0-test
  license: CC-BY-4.0
files:
  events: {name: events.csv}
  regions: {name: regions.zip}
  region_names: {name: names.csv}
flood_types:
  River: {description: Riverine.}
geometry:
  member_stem: regions_shp
columns:
  country_code: Country code
"""


def _write(tmp_path: Path, body: str) -> Path:
    """Write a catalog YAML under `tmp_path` and return its path."""
    path = tmp_path / "hanze_catalog.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.hanze
class TestShippedCatalog:
    """The bundled catalog loads and exposes the pinned record and vocabulary."""

    def test_catalog_path_exists(self) -> None:
        """The bundled YAML ships next to the loader."""
        assert CATALOG_PATH.is_file()

    def test_flood_types_sorted(self) -> None:
        """The four flood types are present and sorted."""
        assert Catalog().flood_types() == ["Coastal", "Flash", "River", "River/Coastal"]

    def test_record_pinned(self) -> None:
        """The record pins version 20478847 / v3.0.1-beta / CC-BY-4.0."""
        record = Catalog().record
        assert (record.record, record.version, record.license) == (
            20478847,
            "v3.0.1-beta",
            "CC-BY-4.0",
        )

    def test_files_present(self) -> None:
        """The events, regions and region-names files are all mapped."""
        cat = Catalog()
        assert cat.file("events").name == "HANZE_events_v3_0_1b.csv"
        assert cat.file("regions").name == "Regions_v2024_simplified.zip"
        assert cat.file("region_names").name.startswith("S2_regions")

    def test_geometry_join(self) -> None:
        """The geometry join pins the member stem, Code field and EPSG:3035."""
        geometry = Catalog().geometry
        assert (geometry.member_stem, geometry.join_field, geometry.crs) == (
            "NUTS3_regions_v2024_simplified",
            "Code",
            "EPSG:3035",
        )

    def test_column_map(self) -> None:
        """A friendly key resolves to its exact HANZE header."""
        assert Catalog().column("regions_nuts3") == "Regions affected (NUTS 3)"

    def test_dict_surface(self) -> None:
        """The catalog behaves as a mapping keyed by flood type."""
        cat = Catalog()
        assert len(cat) == 4
        assert "River/Coastal" in cat
        assert set(iter(cat)) == set(cat.flood_types())

    def test_repr_counts(self) -> None:
        """The repr summarises the row count."""
        assert "datasets=4" in repr(Catalog())

    def test_get_catalog_is_datasets(self) -> None:
        """`get_catalog` returns the flood-type map (the abstract contract)."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets


@pytest.mark.hanze
class TestGetFloodType:
    """`get_flood_type` resolves a type or raises with a did-you-mean hint."""

    def test_resolves_known_type(self) -> None:
        """A known type returns its FloodType row."""
        assert isinstance(Catalog().get_flood_type("River"), FloodType)

    def test_unknown_type_raises_with_hint(self) -> None:
        """An unknown but close type names the closest match."""
        cat = Catalog()
        with pytest.raises(ValueError, match="Did you mean 'River'") as exc:
            cat.get_flood_type("Rivers")
        assert "HANZE catalog" in str(exc.value)

    def test_getitem_raises_keyerror(self) -> None:
        """`cat[bad]` raises KeyError (the dict-style miss)."""
        with pytest.raises(KeyError):
            _ = Catalog()["nope"]


@pytest.mark.hanze
class TestRowModels:
    """The frozen row models behave as declared."""

    def test_content_url(self) -> None:
        """`HanzeFile.content_url` composes the Zenodo REST content URL."""
        url = HanzeFile(name="x.csv").content_url(42)
        assert url == "https://zenodo.org/api/records/42/files/x.csv/content"

    def test_record_is_frozen(self) -> None:
        """`ZenodoRecord` is immutable."""
        record = ZenodoRecord(record=1)
        with pytest.raises(ValidationError):
            record.record = 2  # type: ignore[misc]

    def test_geometry_defaults(self) -> None:
        """`GeometryJoin` defaults the join field, name field and CRS."""
        geometry = GeometryJoin(member_stem="s")
        assert (geometry.join_field, geometry.name_field, geometry.crs) == (
            "Code",
            "Name",
            "EPSG:3035",
        )

    def test_extra_field_rejected(self) -> None:
        """An unexpected field is rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            FloodType(bogus=1)  # type: ignore[call-arg]


@pytest.mark.hanze
class TestLoad:
    """`load`, the parse cache, and the malformed-file guards."""

    def test_load_from_custom_path(self, tmp_path: Path) -> None:
        """A custom YAML path loads into a populated catalog."""
        cat = Catalog.load(_write(tmp_path, _MINIMAL))
        assert cat.flood_types() == ["River"]
        assert cat.record.record == 999

    def test_cache_reuse_then_clear(self, tmp_path: Path) -> None:
        """The parse cache serves a repeat load, and `clear` empties it."""
        path = _write(tmp_path, _MINIMAL)
        first = Catalog.load(path)
        second = Catalog.load(path)
        assert first.flood_types() == second.flood_types()
        catalog_module.clear_catalog_cache()
        assert Catalog.load(path).record.record == 999

    def test_missing_record_block_raises(self, tmp_path: Path) -> None:
        """A YAML without a `record:` block is rejected."""
        body = _MINIMAL.replace(
            "record:\n  record: 999\n  version: v0-test\n  license: CC-BY-4.0\n", ""
        )
        path = _write(tmp_path, body)
        with pytest.raises(ValueError, match="record:"):
            Catalog.load(path)

    def test_missing_flood_types_raises(self, tmp_path: Path) -> None:
        """A YAML without a `flood_types:` block is rejected."""
        body = _MINIMAL.replace("flood_types:\n  River: {description: Riverine.}\n", "")
        path = _write(tmp_path, body)
        with pytest.raises(ValueError, match="flood_types:"):
            Catalog.load(path)

    def test_missing_required_file_raises(self, tmp_path: Path) -> None:
        """A `files:` block missing a required key (regions) is rejected."""
        body = _MINIMAL.replace("  regions: {name: regions.zip}\n", "")
        path = _write(tmp_path, body)
        with pytest.raises(ValueError, match="regions"):
            Catalog.load(path)

    def test_optional_region_names_not_required(self, tmp_path: Path) -> None:
        """The unused `region_names` file is optional — its absence still loads."""
        body = _MINIMAL.replace("  region_names: {name: names.csv}\n", "")
        assert Catalog.load(_write(tmp_path, body)).flood_types() == ["River"]

    def test_invalid_row_raises(self, tmp_path: Path) -> None:
        """A row that fails validation surfaces a clear error."""
        body = _MINIMAL.replace("  record: 999", "  record: not-an-int")
        path = _write(tmp_path, body)
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(path)
