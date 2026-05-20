"""Unit tests for `earthlens.cmems.catalog`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.cmems import Catalog, Dataset, Variable
from earthlens.cmems.catalog import (
    CATALOG_PATH,
    TemporalCoverage,
    _load_catalog_data,
    _yaml_files_for,
    clear_catalog_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Reset the module-level parse cache so tmp-file rewrites work."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.cmems
class TestCatalogBundledYaml:
    """The bundled `catalog/` directory parses and is non-empty."""

    def test_bundled_catalog_path_exists(self):
        """`CATALOG_PATH` points at the real shipped catalog directory."""
        assert CATALOG_PATH.is_dir(), f"bundled catalog dir missing: {CATALOG_PATH}"
        assert (CATALOG_PATH / "_index.yaml").is_file(), "missing _index.yaml"

    def test_default_construction_loads_yaml(self):
        """`Catalog()` with no args loads the bundled catalog directory."""
        cat = Catalog()
        assert len(cat.datasets) > 0, "bundled catalog should have at least one dataset"

    def test_available_datasets_populated(self):
        """The informational `available_datasets` index round-trips."""
        cat = Catalog()
        assert isinstance(cat.available_datasets, list)
        assert any(
            "phy" in d or "sst" in d.lower() for d in cat.available_datasets
        ), (
            "expected at least one physics or SST dataset id, got "
            f"{cat.available_datasets[:5]!r}"
        )

    def test_curated_is_subset_of_available(self):
        """Every curated dataset id is a member of `available_datasets`."""
        cat = Catalog()
        missing = set(cat.datasets) - set(cat.available_datasets)
        assert not missing, (
            f"curated datasets absent from available_datasets: {sorted(missing)[:5]}"
        )

    def test_curated_glorys_present(self):
        """The curated GLORYS12 dataset row resolves end-to-end."""
        cat = Catalog()
        ds = cat.get_dataset("cmems_mod_glo_phy_my_0.083deg_P1D-m")
        assert ds.cadence == "daily", f"GLORYS12 cadence should be daily, got {ds.cadence!r}"
        assert ds.domain == "global", f"GLORYS12 domain should be global, got {ds.domain!r}"
        assert "thetao" in ds.variables, (
            f"GLORYS12 should expose thetao; got {list(ds.variables)}"
        )

    def test_get_variable_known_pair(self):
        """`get_variable` resolves a `(dataset, variable)` pair."""
        v = Catalog().get_variable(
            "cmems_mod_glo_phy_my_0.083deg_P1D-m", "thetao"
        )
        assert v.units == "degrees_C", f"thetao units should be degrees_C, got {v.units!r}"
        assert v.long_name.startswith("Sea water potential temperature"), (
            f"long_name not preserved: {v.long_name!r}"
        )

    def test_get_variable_unknown_dataset(self):
        """Unknown dataset id raises KeyError."""
        with pytest.raises(KeyError):
            Catalog().get_variable("not-a-real-dataset", "thetao")

    def test_get_variable_unknown_variable(self):
        """Unknown variable under a known dataset raises KeyError."""
        with pytest.raises(KeyError):
            Catalog().get_variable(
                "cmems_mod_glo_phy_my_0.083deg_P1D-m", "not-a-real-var"
            )


@pytest.mark.cmems
class TestTemporalCoverageDateCoercion:
    """`TemporalCoverage` accepts both ISO strings and `datetime.date`."""

    def test_string_passthrough(self):
        """String dates round-trip as strings."""
        tc = TemporalCoverage(start="1993-01-01", end="2024-12-31")
        assert tc.start == "1993-01-01"
        assert tc.end == "2024-12-31"

    def test_datetime_date_coerced_to_iso(self):
        """PyYAML-style `datetime.date` is coerced to ISO."""
        import datetime as dt

        tc = TemporalCoverage(start=dt.date(2007, 1, 1), end=None)
        assert tc.start == "2007-01-01", f"date not coerced to ISO; got {tc.start!r}"
        assert tc.end is None, "end=None must round-trip as None"


@pytest.mark.cmems
class TestYamlFilesFor:
    """`_yaml_files_for` resolves a catalog path to its contributing files."""

    def test_directory_globs_yaml_sorted(self, tmp_path: Path):
        """A directory returns its `*.yaml` siblings, sorted."""
        (tmp_path / "b.yaml").write_text("datasets: {}\n")
        (tmp_path / "a.yaml").write_text("datasets: {}\n")
        (tmp_path / "notes.txt").write_text("ignore me\n")
        files = _yaml_files_for(tmp_path)
        assert [f.name for f in files] == ["a.yaml", "b.yaml"], (
            f"expected sorted *.yaml only, got {[f.name for f in files]}"
        )

    def test_single_file_returns_itself(self, tmp_path: Path):
        """An existing single file returns just that file."""
        target = tmp_path / "one.yaml"
        target.write_text("datasets: {}\n")
        assert _yaml_files_for(target) == [target], (
            f"single file should return [itself], got {_yaml_files_for(target)!r}"
        )

    def test_missing_path_raises_valueerror(self, tmp_path: Path):
        """A path that is neither a dir nor an existing file fails loud."""
        missing = tmp_path / "nope" / "missing.yaml"
        with pytest.raises(ValueError, match="does not exist") as exc:
            _yaml_files_for(missing)
        assert "missing.yaml" in str(exc.value), (
            f"error should name the bad path, got {exc.value}"
        )


@pytest.mark.cmems
class TestCatalogLoaderEdgeCases:
    """Loader validates YAML structure before yielding."""

    def test_missing_datasets_block(self, tmp_path: Path):
        """A YAML with no `datasets:` block fails loud."""
        bad = tmp_path / "no_datasets.yaml"
        bad.write_text("available_products: []\n")
        with pytest.raises(ValueError, match="datasets"):
            _load_catalog_data(bad)

    def test_missing_catalog_path_raises(self, tmp_path: Path):
        """`_load_catalog_data` on a nonexistent path raises a clear error."""
        with pytest.raises(ValueError, match="does not exist"):
            _load_catalog_data(tmp_path / "absent-dir")

    def test_dataset_without_variables(self, tmp_path: Path):
        """A dataset entry without any `variables:` fails loud."""
        bad = tmp_path / "empty_vars.yaml"
        bad.write_text(
            "datasets:\n"
            "  some-dataset:\n"
            "    product: P\n"
            "    title: T\n"
        )
        with pytest.raises(ValueError, match="variables"):
            _load_catalog_data(bad)

    def test_variable_with_invalid_field(self, tmp_path: Path):
        """Pydantic rejects extra fields on the variable schema."""
        bad = tmp_path / "bad_var.yaml"
        bad.write_text(
            "datasets:\n"
            "  ds-1:\n"
            "    variables:\n"
            "      v:\n"
            "        units: m\n"
            "        long_name: ok\n"
            "        bogus_field: 1\n"
        )
        with pytest.raises(ValueError, match="bogus_field|extra"):
            _load_catalog_data(bad)

    def test_caching_re_reads_on_mtime_change(self, tmp_path: Path):
        """Mutating the YAML invalidates the per-mtime cache entry."""
        target = tmp_path / "cmems.yaml"
        target.write_text(
            "datasets:\n"
            "  ds-1:\n"
            "    variables:\n"
            "      v:\n"
            "        units: m\n"
        )
        first = _load_catalog_data(target)
        assert "ds-1" in first[1]

        import time

        time.sleep(0.01)
        target.write_text(
            "datasets:\n"
            "  ds-1:\n"
            "    variables:\n"
            "      v:\n"
            "        units: K\n"
        )
        second = _load_catalog_data(target)
        assert second[1]["ds-1"].variables["v"].units == "K", (
            "second read should pick up the rewritten unit"
        )


@pytest.mark.cmems
class TestVariableProperties:
    """`Variable` exposes `is_flux` from the typed marker."""

    def test_state_default(self):
        """`types` defaults to None and `is_flux` reports False."""
        v = Variable(units="K")
        assert v.is_flux is False, "default Variable should report is_flux == False"

    def test_flux_marker_routes(self):
        """Explicit `types='flux'` flips `is_flux` to True."""
        v = Variable(units="kg m-2 s-1", types="flux")
        assert v.is_flux is True, "Variable(types='flux') should report is_flux == True"
