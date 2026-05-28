"""Unit tests for `earthlens.ghsl.catalog`."""

from __future__ import annotations

import pytest

from earthlens.ghsl.catalog import (
    CATALOG_PATH,
    Availability,
    Catalog,
    Product,
    native_source_crs,
)


@pytest.mark.ghsl
class TestNativeSourceCrs:
    """Resolution → source-CRS derivation."""

    @pytest.mark.parametrize(
        "resolution, expected",
        [
            ("10m", "54009"),
            ("100m", "54009"),
            ("1km", "54009"),
            ("3ss", "4326"),
            ("30ss", "4326"),
        ],
    )
    def test_known_resolutions(self, resolution, expected):
        """Metric resolutions map to 54009, arc-second to 4326."""
        assert native_source_crs(resolution) == expected

    def test_unknown_resolution_raises(self):
        """An unknown resolution raises listing the known set."""
        with pytest.raises(ValueError, match="unknown GHSL resolution"):
            native_source_crs("250m")


@pytest.mark.ghsl
class TestAvailability:
    """The per-release availability block."""

    def test_defaults(self):
        """Version defaults to ('1', '0') and tiled falls back by resolution."""
        block = Availability(epochs=[2020], resolutions=["100m", "1km"])
        assert block.version == ("1", "0")
        assert block.tiled() == frozenset({"100m"})

    def test_explicit_tiled(self):
        """An explicit tiled_resolutions list is honoured verbatim."""
        block = Availability(
            epochs=[2020], resolutions=["100m", "1km"], tiled_resolutions=["1km"]
        )
        assert block.tiled() == frozenset({"1km"})

    def test_source_crs(self):
        """source_crs reflects the mix of metric + arc-second resolutions."""
        block = Availability(epochs=[2020], resolutions=["100m", "30ss"])
        assert block.source_crs() == frozenset({"54009", "4326"})

    def test_extra_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            Availability(epochs=[2020], resolutions=["100m"], bogus=1)


@pytest.mark.ghsl
class TestProduct:
    """The product row + its helpers."""

    def test_family_token_defaults_to_code(self):
        """family_token falls back to the code when no family is set."""
        prod = Product(code="GHS_POP")
        assert prod.family_token() == "GHS_POP"

    def test_family_token_override(self):
        """A sub-product's family_token is the family directory token."""
        prod = Product(code="GHS_BUILT_H_ANBH", family="GHS_BUILT_H")
        assert prod.family_token() == "GHS_BUILT_H"

    def test_release_epochs_and_resolutions_union(self):
        """release_epochs / release_resolutions union across blocks."""
        prod = Product(
            code="GHS_BUILT_S",
            releases={
                "R2023A": [
                    Availability(epochs=[2020], resolutions=["100m"]),
                    Availability(epochs=[2018], resolutions=["10m"]),
                ]
            },
        )
        assert prod.release_epochs("R2023A") == [2018, 2020]
        assert prod.release_resolutions("R2023A") == ["100m", "10m"]

    def test_block_for_matches_only_paired_combo(self):
        """block_for returns a block only when epoch+resolution co-occur."""
        prod = Product(
            code="GHS_BUILT_S",
            releases={
                "R2023A": [
                    Availability(epochs=[2020], resolutions=["100m"]),
                    Availability(epochs=[2018], resolutions=["10m"]),
                ]
            },
        )
        assert prod.block_for("R2023A", 2018, "10m") is not None
        assert prod.block_for("R2023A", 2018, "100m") is None
        assert prod.block_for("R2023A", 2020, "10m") is None

    def test_color_table_for_categorical(self):
        """color_table yields band/values/color/alpha rows for the legend."""
        prod = Product(
            code="X", categorical=True, legend={1: "a", 2: "b"}, colors={1: "#fff"}
        )
        table = prod.color_table()
        assert list(table.columns) == ["band", "values", "color", "alpha"]
        assert len(table) == 2
        assert table.iloc[0]["color"] == "#fff"
        assert table.iloc[1]["color"] == "#808080"

    def test_color_table_non_categorical_raises(self):
        """color_table on a non-categorical product raises."""
        with pytest.raises(ValueError, match="not a categorical product"):
            Product(code="GHS_POP").color_table()


@pytest.mark.ghsl
class TestCatalog:
    """The bundled-catalog loader + resolve/validate surface."""

    def test_loads_all_curated_products(self):
        """The bundled catalog exposes the full curated product set."""
        cat = Catalog()
        assert "GHS_POP" in cat.available_products()
        assert len(cat) >= 18

    def test_resolve_alias_and_canonical(self):
        """Both a friendly alias and a canonical key resolve to the code."""
        cat = Catalog()
        assert cat.resolve("population") == "GHS_POP"
        assert cat.resolve("GHS_POP") == "GHS_POP"

    def test_resolve_did_you_mean(self):
        """A typo'd key raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean"):
            Catalog().resolve("populaton")

    def test_validate_happy_path(self):
        """A valid (product, release, epoch, resolution) returns the tuple."""
        result = Catalog().validate("GHS_POP", "R2023A", 2020, "100m")
        assert result == ("GHS_POP", "R2023A", 2020, "100m")

    def test_validate_bad_release(self):
        """An unknown release is rejected listing the available ones."""
        with pytest.raises(ValueError, match="no release"):
            Catalog().validate("GHS_POP", "R9999Z", 2020, "100m")

    def test_validate_bad_epoch(self):
        """An out-of-range epoch is rejected listing the epochs."""
        with pytest.raises(ValueError, match="no epoch"):
            Catalog().validate("GHS_POP", "R2023A", 1973, "100m")

    def test_validate_bad_resolution(self):
        """An unavailable resolution is rejected listing the resolutions."""
        with pytest.raises(ValueError, match="no resolution"):
            Catalog().validate("GHS_SMOD", "R2023A", 2020, "100m")

    def test_validate_epoch_resolution_not_paired(self):
        """A valid epoch + valid resolution that never co-occur is rejected."""
        with pytest.raises(ValueError, match="not together"):
            Catalog().validate("GHS_BUILT_S", "R2023A", 2018, "100m")

    def test_get_did_you_mean(self):
        """get() on an unknown code raises via the AbstractCatalog hint."""
        with pytest.raises(ValueError):
            Catalog().get("GHS_NOPE")

    def test_load_from_explicit_path(self):
        """load() reads the bundled YAML when given its path."""
        cat = Catalog.load(CATALOG_PATH)
        assert "GHS_SMOD" in cat.datasets

    def test_load_missing_products_block(self, tmp_path):
        """A YAML without a products: block raises a clear error."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("other: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="products:"):
            Catalog.load(empty)

    def test_tabular_products_flagged(self):
        """DUC and the WUP statistics tables are kind='tabular'."""
        cat = Catalog()
        assert cat.get("GHS_DUC").kind == "tabular"
        assert cat.get("GHS_WUP_MTUC").kind == "tabular"


@pytest.mark.ghsl
class TestMultiFileCatalog:
    """The directory layout + `(path, mtime_ns)` parse cache."""

    def _write(self, directory, name, text):
        """Write a catalog YAML fragment and return its path."""
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_yaml_files_for_directory(self, tmp_path):
        """A directory yields its sorted *.yaml siblings, skipping non-YAML."""
        from earthlens.ghsl.catalog import _yaml_files_for

        self._write(tmp_path, "b.yaml", "products: {}\n")
        self._write(tmp_path, "a.yaml", "products: {}\n")
        self._write(tmp_path, "notes.txt", "ignore\n")
        assert [p.name for p in _yaml_files_for(tmp_path)] == ["a.yaml", "b.yaml"]

    def test_yaml_files_for_single_file(self, tmp_path):
        """A single existing file returns just that file."""
        from earthlens.ghsl.catalog import _yaml_files_for

        f = self._write(tmp_path, "one.yaml", "products: {}\n")
        assert _yaml_files_for(f) == [f]

    def test_yaml_files_for_missing_raises(self, tmp_path):
        """A non-existent path fails loud."""
        from earthlens.ghsl.catalog import _yaml_files_for

        with pytest.raises(ValueError, match="does not exist"):
            _yaml_files_for(tmp_path / "nope")

    def test_merges_products_across_files(self, tmp_path):
        """Products from several files merge into one catalog."""
        self._write(
            tmp_path,
            "_index.yaml",
            "available_datasets: [GHS_POP, GHS_SMOD]\n",
        )
        self._write(
            tmp_path,
            "pop.yaml",
            "products:\n  GHS_POP:\n    default_resolution: '100m'\n"
            "    releases:\n      R2023A:\n        - epochs: [2020]\n"
            "          resolutions: ['100m']\n",
        )
        self._write(
            tmp_path,
            "smod.yaml",
            "products:\n  GHS_SMOD:\n    categorical: true\n"
            "    default_resolution: '1km'\n    legend: {30: Urban Centre}\n"
            "    releases:\n      R2023A:\n        - epochs: [2020]\n"
            "          resolutions: ['1km']\n",
        )
        cat = Catalog.load(tmp_path)
        assert set(cat.datasets) == {"GHS_POP", "GHS_SMOD"}
        assert sorted(cat.available_datasets) == ["GHS_POP", "GHS_SMOD"]

    def test_duplicate_code_across_files_raises(self, tmp_path):
        """The same product code in two files is rejected."""
        body = (
            "products:\n  GHS_POP:\n    default_resolution: '100m'\n"
            "    releases:\n      R2023A:\n        - epochs: [2020]\n"
            "          resolutions: ['100m']\n"
        )
        self._write(tmp_path, "a.yaml", body)
        self._write(tmp_path, "b.yaml", body)
        with pytest.raises(ValueError, match="declared in two catalog files"):
            Catalog.load(tmp_path)

    def test_curated_code_missing_from_index_raises(self, tmp_path):
        """A curated code absent from available_datasets is rejected."""
        self._write(tmp_path, "_index.yaml", "available_datasets: [GHS_OTHER]\n")
        self._write(
            tmp_path,
            "pop.yaml",
            "products:\n  GHS_POP:\n    default_resolution: '100m'\n"
            "    releases:\n      R2023A:\n        - epochs: [2020]\n"
            "          resolutions: ['100m']\n",
        )
        with pytest.raises(ValueError, match="missing from 'available_datasets:'"):
            Catalog.load(tmp_path)

    def test_parse_cache_hits_and_clears(self, tmp_path):
        """A second load on an unchanged tree hits the cache; clear empties it."""
        from earthlens.ghsl.catalog import (
            _CATALOG_CACHE,
            _load_catalog_data,
            clear_catalog_cache,
        )

        self._write(
            tmp_path,
            "pop.yaml",
            "products:\n  GHS_POP:\n    default_resolution: '100m'\n"
            "    releases:\n      R2023A:\n        - epochs: [2020]\n"
            "          resolutions: ['100m']\n",
        )
        clear_catalog_cache()
        first = _load_catalog_data(tmp_path)
        second = _load_catalog_data(tmp_path)
        assert second is first, "an unchanged tree must return the cached tuple"
        clear_catalog_cache()
        assert len(_CATALOG_CACHE) == 0
