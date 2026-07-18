"""Unit tests for `earthlens.erddap.catalog`."""

from __future__ import annotations

import pytest

from earthlens.erddap import CATALOG_PATH, Catalog, Dataset
from earthlens.erddap.catalog import (
    _load_catalog_data,
    _yaml_files_for,
    clear_catalog_cache,
)

pytestmark = pytest.mark.erddap


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Reset the module-level parse cache so tmp-file rewrites work."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestBundledCatalog:
    """The bundled `catalog/` directory parses and is well-formed."""

    def test_catalog_path_is_a_directory_with_index(self):
        """`CATALOG_PATH` points at the shipped sharded directory."""
        assert CATALOG_PATH.is_dir()
        assert (CATALOG_PATH / "_index.yaml").is_file()

    def test_default_construction_loads_rows(self):
        """`Catalog()` with no args loads at least one dataset row."""
        assert len(Catalog().datasets) > 0

    def test_curated_is_subset_of_available(self):
        """Every curated key is listed in `available_datasets`."""
        cat = Catalog()
        assert set(cat.datasets) <= set(cat.available_datasets)

    @pytest.mark.parametrize("dataset_id", sorted(Catalog().datasets))
    def test_row_integrity(self, dataset_id):
        """Every row has a server_url, dataset_id, and a known protocol."""
        ds = Catalog().get(dataset_id)
        assert isinstance(ds, Dataset)
        assert ds.server_url.startswith("https://")
        assert ds.dataset_id == dataset_id
        assert ds.protocol in ("griddap", "tabledap")

    def test_griddap_row_has_dim_names(self):
        """The seeded CRW griddap row resolves with a time/lat/lon grid."""
        ds = Catalog().get("NOAA_DHW")
        assert ds.protocol == "griddap"
        assert ds.dim_names == ["time", "latitude", "longitude"]
        assert ds.variables  # a non-empty default variable set

    def test_tabledap_row(self):
        """The seeded NDBC buoy row is tabledap."""
        assert Catalog().get("cwwcNDBCMet").protocol == "tabledap"

    def test_unknown_id_raises_did_you_mean(self):
        """An unknown id raises ValueError naming a close match."""
        with pytest.raises(ValueError, match="ERDDAP catalog"):
            Catalog().get("NOAA_DHX")


class TestLoaderEdgeCases:
    """Loader behaviour on monkey-patched temp catalogs."""

    def test_single_yaml_file_loads(self, tmp_path):
        """A single `*.yaml` file (not a dir) is a valid catalog source."""
        f = tmp_path / "one.yaml"
        f.write_text(
            "datasets:\n"
            "  ds1:\n"
            "    server_url: https://x/erddap\n"
            "    dataset_id: ds1\n"
            "    protocol: tabledap\n"
        )
        _available, datasets = _load_catalog_data(f)
        assert datasets["ds1"].protocol == "tabledap"

    def test_duplicate_key_across_files_rejected(self, tmp_path):
        """The same dataset id in two files is a load error."""
        body = (
            "datasets:\n"
            "  dup:\n"
            "    server_url: https://x/erddap\n"
            "    dataset_id: dup\n"
            "    protocol: griddap\n"
        )
        (tmp_path / "a.yaml").write_text(body)
        (tmp_path / "b.yaml").write_text(body)
        with pytest.raises(ValueError, match="declared in two catalog files"):
            _load_catalog_data(tmp_path)

    def test_empty_datasets_block_rejected(self, tmp_path):
        """A catalog with no dataset rows fails loud."""
        (tmp_path / "empty.yaml").write_text("available_datasets: []\n")
        with pytest.raises(ValueError, match="empty 'datasets:' block"):
            _load_catalog_data(tmp_path)

    def test_curated_absent_from_available_rejected(self, tmp_path):
        """A curated key missing from `available_datasets` is an error."""
        (tmp_path / "c.yaml").write_text(
            "available_datasets: [other]\n"
            "datasets:\n"
            "  ds1:\n"
            "    server_url: https://x/erddap\n"
            "    dataset_id: ds1\n"
            "    protocol: tabledap\n"
        )
        with pytest.raises(ValueError, match="missing from"):
            _load_catalog_data(tmp_path)

    def test_missing_path_raises(self, tmp_path):
        """A non-existent catalog path fails with a clear message."""
        with pytest.raises(ValueError, match="does not exist"):
            _yaml_files_for(tmp_path / "nope")

    def test_vanished_file_stat_falls_back_to_default_cache_key(
        self, tmp_path, monkeypatch
    ):
        """A file that vanishes during the mtime build still loads (degraded key)."""
        from pathlib import Path

        (tmp_path / "c.yaml").write_text(
            "datasets:\n"
            "  ds1:\n"
            "    server_url: https://x/erddap\n"
            "    dataset_id: ds1\n"
            "    protocol: tabledap\n"
        )
        real_stat = Path.stat

        def _stat(self, *args, **kwargs):
            if self.suffix == ".yaml":
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _stat)
        _available, datasets = _load_catalog_data(tmp_path)
        assert datasets["ds1"].protocol == "tabledap"

    def test_invalid_protocol_rejected(self, tmp_path):
        """A row with an out-of-domain protocol fails validation."""
        (tmp_path / "bad.yaml").write_text(
            "datasets:\n"
            "  ds1:\n"
            "    server_url: https://x/erddap\n"
            "    dataset_id: ds1\n"
            "    protocol: wmsdap\n"
        )
        with pytest.raises(ValueError, match="failed validation"):
            _load_catalog_data(tmp_path)
