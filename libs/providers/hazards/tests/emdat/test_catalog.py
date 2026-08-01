"""Tests for the EM-DAT dataset catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.emdat import CATALOG_PATH, Catalog, Dataset, clear_catalog_cache
from earthlens.emdat import catalog as catalog_module

_MINIMAL_EVENTS = """
datasets:
  emdat:events:
    provider: dataverse
    output_kind: tabular
    long_name: Test archive
    dataverse_base: https://example.invalid
    doi: doi:10.0000/TEST
    file_pattern: "*_archive.xlsx"
    sheet: Sheet1
    year_column: Start Year
    licence: CC-BY-NC-ND-4.0
    hazard_vocabulary: gdis
hazard_vocabularies:
  gdis:
    - flood
"""


def _write_catalog(tmp_path: Path, body: str) -> Path:
    """Write a catalog YAML and return its path."""
    path = tmp_path / "catalog.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.emdat
class TestShippedCatalog:
    """The bundled catalog loads and describes the three shipped datasets."""

    def test_catalog_path_exists(self) -> None:
        """The bundled YAML ships next to the loader."""
        assert CATALOG_PATH.is_file()

    def test_available_ids(self) -> None:
        """All three dataset ids are present and sorted."""
        assert Catalog().available() == [
            "emdat:events",
            "gdis:points",
            "gdis:polygons",
        ]

    @pytest.mark.parametrize(
        ("dataset_id", "provider", "output_kind"),
        [
            ("emdat:events", "dataverse", "tabular"),
            ("gdis:points", "earthdata", "vector"),
            ("gdis:polygons", "earthdata", "vector"),
        ],
    )
    def test_provider_and_output_kind(
        self, dataset_id: str, provider: str, output_kind: str
    ) -> None:
        """Each row names its route and the shape it emits."""
        row = Catalog().get(dataset_id)
        assert (row.provider, row.output_kind) == (provider, output_kind)

    def test_events_is_restricted_use(self) -> None:
        """The EM-DAT archive is flagged restricted so a LicenseWarning fires."""
        assert Catalog().get("emdat:events").restricted_use is True

    @pytest.mark.parametrize("dataset_id", ["gdis:points", "gdis:polygons"])
    def test_gdis_is_not_restricted(self, dataset_id: str) -> None:
        """GDIS is CC-BY-4.0, so it carries no use restriction."""
        assert Catalog().get(dataset_id).restricted_use is False

    def test_points_carries_year_and_coordinates(self) -> None:
        """The CSV distribution has its own year and coordinate columns."""
        row = Catalog().get("gdis:points")
        assert (row.year_column, row.latitude_column, row.longitude_column) == (
            "year",
            "latitude",
            "longitude",
        )

    def test_polygons_derive_year_from_id(self) -> None:
        """The GeoPackage has no year column and falls back to the id prefix."""
        row = Catalog().get("gdis:polygons")
        assert row.year_column is None
        assert row.year_from_id_prefix is True

    def test_polygons_flag_a_large_download(self) -> None:
        """The GeoPackage records its size so the backend can warn."""
        assert Catalog().get("gdis:polygons").download_mb > 1000

    def test_dict_surface(self) -> None:
        """The catalog behaves as a mapping of dataset id to row."""
        catalog = Catalog()
        assert len(catalog) == 3
        assert "gdis:points" in catalog
        assert isinstance(catalog["gdis:points"], Dataset)

    def test_unknown_id_hints(self) -> None:
        """A near-miss id raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'gdis:points'"):
            Catalog().get("gdis:point")


@pytest.mark.emdat
class TestHazardVocabulary:
    """`normalize_hazard` is per dataset and absorbs inconsistent spellings."""

    def test_vocabularies_are_canonical(self) -> None:
        """Every shipped hazard name is lower-case and stripped."""
        catalog = Catalog()
        for names in catalog.hazard_vocabularies.values():
            assert all(name == name.strip().lower() for name in names)

    def test_gdis_vocabulary_is_the_eight_shipped_types(self) -> None:
        """GDIS validates against exactly the values its data carries."""
        gdis = Catalog().hazard_vocabularies["gdis"]
        assert len(gdis) == 8
        assert "extreme temperature" in gdis

    def test_emdat_vocabulary_covers_technological_types(self) -> None:
        """The archive's vocabulary includes the technological group."""
        emdat = Catalog().hazard_vocabularies["emdat"]
        assert {"wildfire", "epidemic", "oil spill", "road"} <= set(emdat)

    def test_neither_vocabulary_contains_the_other(self) -> None:
        """GDIS has landslide, which EM-DAT files under mass movement."""
        catalog = Catalog()
        gdis = set(catalog.hazard_vocabularies["gdis"])
        emdat = set(catalog.hazard_vocabularies["emdat"])
        assert gdis - emdat == {"landslide"}
        assert len(emdat) > len(gdis)

    @pytest.mark.parametrize("spelling", ["flood", "Flood", "  FLOOD  ", "FlOoD"])
    def test_case_and_whitespace_insensitive(self, spelling: str) -> None:
        """Any casing or padding resolves to the canonical name."""
        catalog = Catalog()
        row = catalog.get("gdis:points")
        assert catalog.normalize_hazard(spelling, row) == "flood"

    def test_trailing_space_spelling_resolves(self) -> None:
        """The GeoPackage's trailing-space spelling maps to the canonical one."""
        catalog = Catalog()
        row = catalog.get("gdis:polygons")
        assert (
            catalog.normalize_hazard("extreme temperature ", row)
            == "extreme temperature"
        )

    @pytest.mark.parametrize(
        "hazard",
        ["wildfire", "epidemic", "industrial accident (general)", "fog", "road"],
    )
    def test_archive_accepts_its_own_types(self, hazard: str) -> None:
        """A valid EM-DAT type is accepted on the archive, not rejected as GDIS."""
        catalog = Catalog()
        row = catalog.get("emdat:events")
        assert catalog.normalize_hazard(hazard, row) == hazard

    @pytest.mark.parametrize("hazard", ["wildfire", "epidemic", "oil spill"])
    def test_gdis_still_rejects_ungeocoded_types(self, hazard: str) -> None:
        """A type GDIS never geocoded is still refused on the GDIS rows."""
        catalog = Catalog()
        row = catalog.get("gdis:points")
        with pytest.raises(ValueError, match="is not a disaster type"):
            catalog.normalize_hazard(hazard, row)

    def test_error_names_the_dataset_not_gdis(self) -> None:
        """The message names the dataset queried, not an unrelated one."""
        catalog = Catalog()
        row = catalog.get("emdat:events")
        with pytest.raises(ValueError, match="'emdat:events'"):
            catalog.normalize_hazard("definitely not a hazard", row)

    def test_unknown_hazard_hints(self) -> None:
        """A near-miss hazard raises with a did-you-mean hint."""
        catalog = Catalog()
        row = catalog.get("gdis:points")
        with pytest.raises(ValueError, match="Did you mean 'flood'"):
            catalog.normalize_hazard("floods", row)

    def test_unknown_hazard_without_close_match(self) -> None:
        """An unrelated hazard still lists the vocabulary."""
        catalog = Catalog()
        row = catalog.get("gdis:points")
        with pytest.raises(ValueError, match="is not a disaster type"):
            catalog.normalize_hazard("zzzzzzzz", row)

    def test_vocabulary_for_returns_the_named_list(self) -> None:
        """`vocabulary_for` resolves a row to its vocabulary."""
        catalog = Catalog()
        assert (
            catalog.vocabulary_for(catalog.get("gdis:points"))
            == (catalog.hazard_vocabularies["gdis"])
        )


@pytest.mark.emdat
class TestFileMatching:
    """`matches_file` resolves the archive across release-date prefixes."""

    @pytest.mark.parametrize(
        "filename",
        ["260430_emdat_archive.xlsx", "990101_emdat_archive.xlsx"],
    )
    def test_matches_any_release_prefix(self, filename: str) -> None:
        """The pattern is prefix-agnostic, so a re-cut archive still resolves."""
        assert Catalog().get("emdat:events").matches_file(filename) is True

    @pytest.mark.parametrize(
        "filename",
        ["260430_emdat_columns.csv", "01_data_structure_and_content.pdf"],
    )
    def test_rejects_sibling_files(self, filename: str) -> None:
        """The archive's siblings in the same version do not match."""
        assert Catalog().get("emdat:events").matches_file(filename) is False

    def test_row_without_pattern_matches_nothing(self) -> None:
        """A GDIS row has no file pattern, so it never claims a file."""
        assert Catalog().get("gdis:points").matches_file("anything.csv") is False


@pytest.mark.emdat
class TestCatalogLoading:
    """Loading, caching and validation of a catalog file."""

    def test_load_from_explicit_path(self, tmp_path: Path) -> None:
        """An explicit path overrides the bundled catalog."""
        path = _write_catalog(tmp_path, _MINIMAL_EVENTS)
        catalog = Catalog.load(path)
        assert catalog.available() == ["emdat:events"]

    def test_parse_cache_is_reused(self, tmp_path: Path) -> None:
        """A second load of an unchanged file hits the cache."""
        path = _write_catalog(tmp_path, _MINIMAL_EVENTS)
        Catalog.load(path)
        key = next(iter(catalog_module._CATALOG_CACHE))
        Catalog.load(path)
        assert key in catalog_module._CATALOG_CACHE

    def test_clear_catalog_cache_empties_it(self, tmp_path: Path) -> None:
        """`clear_catalog_cache` drops every parsed entry."""
        Catalog.load(_write_catalog(tmp_path, _MINIMAL_EVENTS))
        clear_catalog_cache()
        assert len(catalog_module._CATALOG_CACHE) == 0

    def test_empty_datasets_block_rejected(self, tmp_path: Path) -> None:
        """A catalog with no datasets is an error, not an empty catalog."""
        path = _write_catalog(tmp_path, "datasets:\nhazard_types: []\n")
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            Catalog.load(path)

    def test_duplicate_keys_rejected(self, tmp_path: Path) -> None:
        """The strict loader refuses a dataset id declared twice."""
        body = """
datasets:
  emdat:events:
    provider: dataverse
    output_kind: tabular
    long_name: First
    dataverse_base: https://example.invalid
    doi: doi:10.0000/TEST
    file_pattern: "*_archive.xlsx"
    sheet: Sheet1
    year_column: Start Year
    licence: CC-BY-NC-ND-4.0
  emdat:events:
    provider: dataverse
    output_kind: tabular
    long_name: Second
    dataverse_base: https://example.invalid
    doi: doi:10.0000/TEST
    file_pattern: "*_archive.xlsx"
    sheet: Sheet1
    year_column: Start Year
    licence: CC-BY-NC-ND-4.0
"""
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            Catalog.load(path)

    def test_get_catalog_returns_rows(self) -> None:
        """`get_catalog` satisfies the abstract contract with the row map."""
        catalog = Catalog()
        assert catalog.get_catalog() is catalog.datasets


@pytest.mark.emdat
class TestRowValidation:
    """A malformed row fails loudly at load time, naming the problem."""

    def test_dataverse_row_needs_its_fields(self, tmp_path: Path) -> None:
        """A dataverse row without a doi is rejected."""
        body = _MINIMAL_EVENTS.replace("    doi: doi:10.0000/TEST\n", "")
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="missing required field"):
            Catalog.load(path)

    def test_earthdata_row_needs_its_fields(self, tmp_path: Path) -> None:
        """An earthdata row without a granule is rejected."""
        body = """
datasets:
  gdis:points:
    provider: earthdata
    output_kind: vector
    long_name: Test points
    short_name: TEST
    member: inner.csv
    format: csv
    year_column: year
    licence: CC-BY-4.0
"""
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="missing required field"):
            Catalog.load(path)

    def test_gpkg_row_needs_a_layer(self, tmp_path: Path) -> None:
        """A GeoPackage row must name the layer to read."""
        body = """
datasets:
  gdis:polygons:
    provider: earthdata
    output_kind: vector
    long_name: Test polygons
    short_name: TEST
    granule: g.zip
    member: inner.gpkg
    format: gpkg
    year_from_id_prefix: true
    id_column: disasterno
    licence: CC-BY-4.0
"""
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="must name its `layer:`"):
            Catalog.load(path)

    def test_row_needs_a_year_source(self, tmp_path: Path) -> None:
        """A row with no year column and no id fallback cannot be windowed."""
        body = _MINIMAL_EVENTS.replace("    year_column: Start Year\n", "")
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="year_column"):
            Catalog.load(path)

    def test_year_from_id_prefix_needs_an_id_column(self, tmp_path: Path) -> None:
        """Deriving the year from the id requires naming the id column."""
        body = """
datasets:
  gdis:polygons:
    provider: earthdata
    output_kind: vector
    long_name: Test polygons
    short_name: TEST
    granule: g.zip
    member: inner.gpkg
    format: gpkg
    layer: GPKG
    year_from_id_prefix: true
    hazard_vocabulary: gdis
    licence: CC-BY-4.0
hazard_vocabularies:
  gdis:
    - flood
"""
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="no `id_column:`"):
            Catalog.load(path)

    def test_unknown_vocabulary_rejected(self, tmp_path: Path) -> None:
        """A row naming a vocabulary that does not exist is refused."""
        body = _MINIMAL_EVENTS.replace(
            "hazard_vocabulary: gdis", "hazard_vocabulary: nope"
        )
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="not in the"):
            Catalog.load(path)

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        """An unrecognised key is a typo, so the row is refused."""
        body = _MINIMAL_EVENTS.replace(
            "    licence: CC-BY-NC-ND-4.0\n",
            "    licence: CC-BY-NC-ND-4.0\n    nonsense: 1\n",
        )
        path = _write_catalog(tmp_path, body)
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(path)
