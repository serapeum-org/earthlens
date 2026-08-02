"""Unit tests for the bundled Caravan catalog and its loader."""

from __future__ import annotations

import pytest

from earthlens.caravan import (
    CATALOG_PATH,
    ArchiveFile,
    Catalog,
    Variable,
    clear_catalog_cache,
)
from earthlens.caravan.catalog import _load_catalog_data

pytestmark = pytest.mark.caravan


class TestBundledCatalog:
    """The shipped YAML, as a reader of the plan would expect it."""

    def test_every_extension_is_present(self):
        """All seven wrapped extensions load."""
        assert sorted(Catalog().extensions) == [
            "base",
            "czechia",
            "denmark",
            "germany",
            "grdc",
            "israel",
            "spain",
        ]

    def test_grdc_pins_the_current_version_not_the_superseded_one(self):
        """The widely cited 14006282 record is v0.3 and must not be the pin."""
        release = Catalog().get_extension("grdc").resolve_version()

        assert release.doi == "10.5281/zenodo.15349031"
        assert release.file_for("csv").record == 15349031

    def test_grdc_catchment_count_is_the_measured_one(self):
        """v0.6 holds 5,356 catchments, not the 5,357 usually quoted."""
        release = Catalog().get_extension("grdc").resolve_version()

        assert release.n_catchments == 5356
        assert release.n_catchments_verified

    def test_every_extension_is_permissively_licensed(self):
        """Every wrapped row is CC-BY-4.0, which is what makes GRDC legal here."""
        assert {extension.license for extension in Catalog().extensions.values()} == {
            "CC-BY-4.0"
        }

    def test_base_carries_both_the_current_and_range_readable_release(self):
        """The cheap path is catalog data, not a branch in code."""
        base = Catalog().get_extension("base")

        assert base.default_version == "1.6"
        assert base.resolve_version("1.6").file_for("csv").archive_format == "tar.gz"
        assert base.resolve_version("1.2").file_for("csv").archive_format == "zip"

    def test_base_1_2_is_flagged_as_a_smaller_older_dataset(self):
        """The escape hatch must not look like a minor version difference."""
        base = Catalog().get_extension("base")

        assert base.resolve_version("1.2").n_catchments == 6830
        assert base.resolve_version("1.6").n_catchments > 6830
        assert base.resolve_version("1.2").data_period == (1981, 2020)

    def test_base_1_6_count_is_marked_unverified(self):
        """A tar.gz cannot be indexed without downloading it, so the count is arithmetic."""
        assert (
            not Catalog()
            .get_extension("base")
            .resolve_version("1.6")
            .n_catchments_verified
        )

    def test_source_datasets_are_not_top_level_rows(self):
        """camels / hysets / lamah live inside base, not beside it."""
        base = Catalog().get_extension("base")

        assert "hysets" in base.sources
        assert "hysets" not in Catalog().extensions
        assert len(base.source_names) == 7

    def test_grdc_root_prefix_differs_between_formats(self):
        """The csv and netcdf archives use different root directories."""
        release = Catalog().get_extension("grdc").resolve_version()

        assert release.file_for("csv").root_prefix == "GRDC_Caravan_extension_csv/"
        assert release.file_for("netcdf").root_prefix == "GRDC_Caravan_extension_nc/"

    def test_denmark_has_no_root_prefix(self):
        """Two archives put members at the top level, which templating would miss."""
        archive = Catalog().get_extension("denmark").resolve_version().file_for("csv")

        assert archive.root_prefix is None

    def test_base_splits_its_formats_across_two_records(self):
        """Since v1.6 the csv and netcdf timeseries are separate Zenodo records."""
        release = Catalog().get_extension("base").resolve_version("1.6")

        assert release.file_for("csv").record != release.file_for("netcdf").record


class TestVariables:
    """Friendly names, real columns, and the three column-set variants."""

    def test_precipitation_maps_to_its_real_column(self):
        """The archive column carries a `_sum` suffix the friendly name drops."""
        assert Catalog().get_variable("grdc", "total_precipitation").column == (
            "total_precipitation_sum"
        )

    def test_pet_resolves_per_column_set(self):
        """PET is the one variable renamed between the legacy and current eras."""
        pet = Catalog().get_variable("base", "potential_evaporation")

        assert pet.column_for("current") == "potential_evaporation_sum_ERA5_LAND"
        assert pet.column_for("legacy") == "potential_evaporation_sum"

    def test_a_real_column_name_is_accepted_directly(self):
        """The archive header is what most users have in front of them."""
        assert (
            Catalog().get_variable("grdc", "total_precipitation_sum").name
            == "total_precipitation"
        )

    def test_a_source_restricted_variable_is_refused_elsewhere(self):
        """Caravan-DE's extra columns do not exist in the other archives."""
        catalog = Catalog()

        with pytest.raises(ValueError, match="exists only in"):
            catalog.get_variable("grdc", "water_level")

    def test_a_source_restricted_variable_is_allowed_in_its_own_extension(self):
        """The same variable resolves for the extension that has it."""
        assert Catalog().get_variable("germany", "water_level").column == "water_level"

    def test_an_unknown_variable_lists_the_known_ones(self):
        """The error has to be actionable without opening the YAML."""
        catalog = Catalog()

        with pytest.raises(ValueError, match="is not a Caravan variable"):
            catalog.get_variable("grdc", "rainfall")


class TestLookupErrors:
    """The did-you-mean surface inherited from `AbstractCatalog`."""

    def test_an_unknown_extension_gets_a_hint(self):
        """A typo names the catalog and the valid keys."""
        catalog = Catalog()

        with pytest.raises(ValueError, match="Caravan catalog"):
            catalog.get_extension("grcd")

    def test_an_unknown_version_lists_the_valid_ones(self):
        """Version keys are not guessable, so the error enumerates them."""
        base = Catalog().get_extension("base")

        with pytest.raises(ValueError, match=r"Known versions: \['1.2', '1.6'\]"):
            base.resolve_version("9.9")

    def test_an_unpublished_format_is_refused(self):
        """Asking for a format a release does not ship names what it does."""
        release = Catalog().get_extension("grdc").resolve_version()
        with pytest.raises(ValueError, match="publishes no"):
            release.file_for("parquet")  # type: ignore[arg-type]


class TestArchiveFile:
    """The file descriptor that decides the transport."""

    def test_zip_is_range_readable_and_tar_is_not(self):
        """This single flag is what routes a request down the cheap path."""
        catalog = Catalog()

        assert (
            catalog.get_extension("grdc")
            .resolve_version()
            .file_for("csv")
            .is_range_readable
        )
        assert not (
            catalog.get_extension("base")
            .resolve_version("1.6")
            .file_for("csv")
            .is_range_readable
        )

    def test_the_url_is_composed_from_the_pinned_record(self):
        """A normal fetch needs no Zenodo metadata round trip."""
        archive = Catalog().get_extension("denmark").resolve_version().file_for("csv")

        assert archive.url == (
            "https://zenodo.org/api/records/15200118/files/"
            "Caravan_extension_DK.zip/content"
        )

    def test_checksums_are_bare_hex(self):
        """Zenodo reports `md5:<hex>`; the catalog stores the digest alone."""
        archive = Catalog().get_extension("grdc").resolve_version().file_for("csv")

        assert archive.md5 == "2689c2bff8807f53c3de127827a3cd16"


class TestLoader:
    """Parsing, caching and validation of the YAML itself."""

    def test_the_bundled_path_exists(self):
        """The catalog ships as package data."""
        assert CATALOG_PATH.is_file()

    def test_rows_are_frozen(self):
        """A catalog row is a value object shared through a parse cache."""
        archive = ArchiveFile(record=1, name="a", size=1, md5="b", archive_format="zip")
        with pytest.raises(Exception, match="frozen|Instance is frozen"):
            archive.name = "c"  # type: ignore[misc]

    def test_the_parse_cache_is_reused(self):
        """A second load returns the same parsed objects."""
        first = _load_catalog_data(CATALOG_PATH)
        second = _load_catalog_data(CATALOG_PATH)

        assert first["datasets"] is second["datasets"]

    def test_clearing_the_cache_forces_a_reparse(self):
        """The escape hatch for a catalog rewritten on disk."""
        first = _load_catalog_data(CATALOG_PATH)
        clear_catalog_cache()

        assert _load_catalog_data(CATALOG_PATH)["datasets"] is not first["datasets"]

    def test_a_missing_extensions_block_raises(self, tmp_path):
        """An empty catalog is a packaging bug, not an empty result."""
        path = tmp_path / "empty.yaml"
        path.write_text("variables: {}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="extensions"):
            Catalog.load(path)

    def test_a_missing_variables_block_raises(self, tmp_path):
        """Without variables nothing can be selected from a member."""
        path = tmp_path / "novars.yaml"
        path.write_text("extensions:\n  a:\n    license: X\n", encoding="utf-8")

        with pytest.raises(ValueError, match="variables"):
            Catalog.load(path)

    def test_a_duplicate_key_is_rejected(self, tmp_path):
        """The strict loader refuses a YAML that silently drops a row."""
        path = tmp_path / "dupe.yaml"
        path.write_text(
            "variables:\n  a:\n    column: a\n  a:\n    column: b\n"
            "extensions:\n  x:\n    license: Y\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            Catalog.load(path)

    def test_a_bad_row_names_the_offender(self, tmp_path):
        """A validation failure has to say which row failed."""
        path = tmp_path / "bad.yaml"
        path.write_text(
            "variables:\n  a:\n    column: a\n"
            "extensions:\n  x:\n    license: Y\n    nonsense: 1\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="extension 'x' failed validation"):
            Catalog.load(path)

    def test_a_variable_row_is_self_describing(self):
        """The friendly name is stored on the row, not only as its key."""
        assert Variable(name="x", column="y").name == "x"


class TestCommunityExtensions:
    """The two extensions published under their source dataset's name."""

    def test_czechia_pins_the_caravan_shaped_file(self):
        """The record also ships native CAMELS-CZ zips, which are not Caravan."""
        archive = Catalog().get_extension("czechia").resolve_version().file_for("csv")

        assert archive.name == "Caravan-Extension-CZ.zip"
        assert archive.root_prefix == "Caravan-Extension-CZ/"

    def test_czechia_carries_the_most_recent_series(self):
        """Its data runs to 2025 where GRDC stops in 2023."""
        assert Catalog().get_extension("czechia").resolve_version().data_period == (
            1950,
            2025,
        )

    def test_spain_declares_its_own_column_set(self):
        """Caravan-ES adds four EFAS/EMO-1 columns to the current set."""
        assert (
            Catalog().get_extension("spain").resolve_version().column_set == "camelses"
        )

    def test_the_spanish_extras_are_source_restricted(self):
        """They exist only in Caravan-ES, so other extensions must refuse them."""
        catalog = Catalog()

        assert catalog.get_variable("spain", "discharge_efas").column == "dis_efas5"
        with pytest.raises(ValueError, match="exists only in"):
            catalog.get_variable("grdc", "discharge_efas")

    def test_neither_uses_a_caravan_shaped_root_prefix(self):
        """Templating a root from the extension name would miss both."""
        catalog = Catalog()

        assert (
            catalog.get_extension("spain").resolve_version().file_for("csv").root_prefix
            == "v110/"
        )
        assert (
            catalog.get_extension("czechia")
            .resolve_version()
            .file_for("csv")
            .root_prefix
            == "Caravan-Extension-CZ/"
        )

    def test_czechia_ships_a_singular_license_directory(self):
        """Every other archive uses `licenses/`; this one does not."""
        assert Catalog().get_extension("czechia").license_file.startswith("license/")
