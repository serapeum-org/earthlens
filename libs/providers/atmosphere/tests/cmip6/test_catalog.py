"""Unit tests for the CMIP6 config + curated-vocabulary catalog."""

from __future__ import annotations

import textwrap

import pytest

from earthlens.cmip6 import Catalog, Cmip6Variable, Experiment, Source, Table
from earthlens.cmip6.catalog import CATALOG_PATH, clear_catalog_cache

pytestmark = [pytest.mark.cmip6, pytest.mark.unit]


def test_config_fields(catalog):
    """The catalog exposes the CSV URL, bucket, facet columns, and defaults."""
    assert catalog.csv_url.endswith("pangeo-cmip6.csv")
    assert catalog.bucket == "cmip6"
    assert catalog.facet_columns[0] == "activity_id"
    assert "source_id" in catalog.facet_columns
    assert "variable_id" in catalog.facet_columns
    assert catalog.default_member_id == "r1i1p1f1"
    assert catalog.default_version == "latest"


def test_curated_blocks_typed(catalog):
    """Each curated block loads into its typed pydantic row."""
    assert isinstance(catalog.get_dataset("tas"), Cmip6Variable)
    assert isinstance(catalog.get_experiment("ssp585"), Experiment)
    assert isinstance(catalog.get_table("Amon"), Table)
    assert isinstance(catalog.get_source("CanESM5"), Source)


def test_variable_metadata(catalog):
    """A curated variable carries its units, long name, and realm."""
    tas = catalog.get_dataset("tas")
    assert tas.units == "K"
    assert "temperature" in tas.long_name.lower()
    assert tas.realm == "atmos"


def test_experiment_and_table_metadata(catalog):
    """Experiments carry their activity and tables their cadence."""
    assert catalog.get_experiment("ssp585").activity_id == "ScenarioMIP"
    assert catalog.get_experiment("historical").activity_id == "CMIP"
    assert catalog.get_table("Amon").cadence == "monthly"
    assert catalog.get_table("day").cadence == "daily"


def test_available_datasets_sorted(catalog):
    """The available-datasets index is the sorted curated variable keys."""
    assert catalog.available_datasets == sorted(catalog.datasets)
    assert "tas" in catalog.available_datasets


def test_dict_surface(catalog):
    """The catalog behaves like a dict over its curated variables."""
    assert "tas" in catalog
    assert catalog["tas"].units == "K"
    assert len(catalog) == len(catalog.datasets)
    assert "Catalog" in repr(catalog)


@pytest.mark.parametrize(
    "getter, key, needle",
    [
        ("get_dataset", "rainfall", "CMIP6 catalog"),
        ("get_experiment", "ssp58", "experiment"),
        ("get_table", "Amonn", "table"),
        ("get_source", "CanESM", "source"),
    ],
)
def test_unknown_key_raises_with_hint(catalog, getter, key, needle):
    """An unknown key raises a ValueError naming the catalog / entry kind."""
    with pytest.raises(ValueError, match=needle):
        getattr(catalog, getter)(key)


def test_did_you_mean_suggests_close_key(catalog):
    """A near-miss experiment id gets a did-you-mean suggestion."""
    with pytest.raises(ValueError, match="Did you mean 'ssp585'"):
        catalog.get_experiment("ssp586")


def test_terms_note_curated_then_default(catalog):
    """A curated source returns its own note; an uncurated one the default."""
    assert "CanESM5" in catalog.terms_note("CanESM5")
    assert catalog.terms_note("NO-SUCH-MODEL") == catalog.default_terms_note
    assert catalog.default_terms_note


def test_injected_datasets_skip_disk():
    """Passing datasets= builds a catalog without reading the bundled YAML."""
    cat = Catalog(csv_url="http://x/y.csv", datasets={"tas": Cmip6Variable(units="K")})
    assert cat.get_dataset("tas").units == "K"
    assert cat.csv_url == "http://x/y.csv"


def test_get_catalog_returns_variable_map(catalog):
    """get_catalog returns the same object as the datasets map."""
    assert catalog.get_catalog() is catalog.datasets


def test_load_is_cached():
    """Loading the same catalog path twice returns the cached instance."""
    clear_catalog_cache()
    first = Catalog.load()
    second = Catalog.load()
    assert first is second
    clear_catalog_cache()
    assert Catalog.load() is not first


def test_load_missing_csv_url_raises(tmp_path):
    """A catalog YAML with no csv_url raises a clear ValueError."""
    path = tmp_path / "bad.yaml"
    path.write_text("bucket: cmip6\n", encoding="utf-8")
    with pytest.raises(ValueError, match="csv_url"):
        Catalog.load(path)


def test_load_malformed_row_raises(tmp_path):
    """A curated row with an unknown field fails validation loudly."""
    path = tmp_path / "bad.yaml"
    path.write_text(
        textwrap.dedent(
            """
            csv_url: http://x/y.csv
            variables:
              tas:
                bogus_field: 1
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Cmip6Variable 'tas' failed validation"):
        Catalog.load(path)


def test_load_missing_file_raises_naming_the_path(tmp_path):
    """A missing catalog path raises the shared loader's error."""
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(tmp_path / "does-not-exist.yaml")


def test_bundled_catalog_path_exists():
    """The bundled catalog ships at the module CATALOG_PATH."""
    assert CATALOG_PATH.exists()
    assert CATALOG_PATH.name == "cmip6_data_catalog.yaml"
