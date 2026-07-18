"""Unit tests for `earthlens.openaq.catalog` (parameter dispatch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.openaq import Catalog, Parameter
from earthlens.openaq.catalog import CATALOG_PATH


@pytest.mark.openaq
class TestParameter:
    """The Parameter row model and its defaults."""

    def test_minimal_construction(self):
        """A parameter needs name/ids; units, display_name, group default."""
        p = Parameter(name="pm25", ids=[2])
        assert p.ids == [2]
        assert p.units == []
        assert p.display_name == ""
        assert p.group == "other"

    def test_empty_ids_rejected(self):
        """A parameter must carry at least one id."""
        with pytest.raises(Exception):
            Parameter(name="pm25", ids=[])

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected (extra='forbid')."""
        with pytest.raises(Exception):
            Parameter(name="pm25", ids=[2], bogus="x")


@pytest.mark.openaq
class TestCatalogLoad:
    """Catalog loads and validates the bundled parameter YAML."""

    def test_bundled_catalog_path_exists(self):
        """The shipped catalog YAML is present as package data."""
        assert CATALOG_PATH.is_file(), f"missing bundled catalog at {CATALOG_PATH}"

    def test_known_parameters(self):
        """The bundled catalog lists the curated parameter set."""
        assert sorted(Catalog().parameters) == [
            "bc",
            "bc_370",
            "bc_375",
            "bc_470",
            "bc_528",
            "bc_625",
            "bc_880",
            "ch4",
            "co",
            "co2",
            "humidity",
            "no",
            "no2",
            "nox",
            "o3",
            "pm1",
            "pm10",
            "pm25",
            "pm4",
            "pressure",
            "relativehumidity",
            "so2",
            "temperature",
            "ufp",
            "um003",
            "um010",
            "um025",
            "um100",
            "wind_direction",
            "wind_speed",
        ]

    def test_pm25_ids(self):
        """pm25 resolves to its single id [2]."""
        assert Catalog().get_parameter("pm25").ids == [2]

    def test_no2_has_all_unit_variant_ids(self):
        """no2 carries every unit-variant id, not just one."""
        assert Catalog().get_parameter("no2").ids == [5, 7, 15]

    def test_no2_units_list_all_variants(self):
        """no2 records the reporting unit of each variant id."""
        assert Catalog().get_parameter("no2").units == ["ppb", "ppm", "µg/m³"]

    def test_ids_for_unions_all_variants_in_order(self):
        """ids_for returns the de-duplicated union across names, order-stable."""
        assert Catalog().ids_for(["pm25", "no2"]) == [2, 5, 7, 15]

    def test_ids_for_dedupes_repeated_name(self):
        """A name repeated across the request contributes its ids only once."""
        assert Catalog().ids_for(["no2", "no2"]) == [5, 7, 15]

    def test_get_catalog_returns_parameters(self):
        """get_catalog returns the same parameter map."""
        cat = Catalog()
        assert cat.get_catalog() is cat.parameters

    def test_get_parameter_unknown_raises_with_hint(self):
        """A close typo raises with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'pm25'"):
            Catalog().get_parameter("pm2.5")

    def test_get_parameter_unknown_lists_known(self):
        """An unknown name error names the catalog kind."""
        with pytest.raises(ValueError, match="OpenAQ parameter catalog"):
            Catalog().get_parameter("zzz")

    def test_ids_for_unknown_raises(self):
        """ids_for propagates the unknown-name error."""
        with pytest.raises(ValueError):
            Catalog().ids_for(["pm25", "nope"])


@pytest.mark.openaq
class TestCatalogLoadErrors:
    """Catalog.load fails loudly on a malformed catalog file."""

    def test_missing_parameters_block(self, tmp_path: Path):
        """A YAML with no parameters: block raises ValueError."""
        bad = tmp_path / "empty.yaml"
        bad.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty 'parameters:' block"):
            Catalog.load(bad)

    def test_invalid_row(self, tmp_path: Path):
        """A row missing required fields raises ValueError."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("parameters:\n  x:\n    name: x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="failed validation"):
            Catalog.load(bad)

    def test_explicit_parameters_skip_disk(self):
        """Passing parameters= skips the disk read (no auto-load)."""
        cat = Catalog(parameters={"x": Parameter(name="x", ids=[1])})
        assert sorted(cat.parameters) == ["x"]


@pytest.mark.openaq
class TestCatalogDictSurface:
    """The inherited AbstractCatalog dict surface backed by `datasets`."""

    def test_len_counts_parameters(self):
        """len(cat) equals the number of parameter rows."""
        cat = Catalog()
        assert len(cat) == len(cat.parameters)

    def test_contains_known_name(self):
        """A curated name is `in` the catalog."""
        assert "pm25" in Catalog()

    def test_contains_unknown_name(self):
        """An unknown name is not `in` the catalog."""
        assert "nope" not in Catalog()

    def test_getitem_returns_row(self):
        """cat[name] returns the Parameter row."""
        assert Catalog()["pm25"].ids == [2]

    def test_getitem_unknown_raises_keyerror(self):
        """cat[unknown] raises KeyError."""
        with pytest.raises(KeyError):
            Catalog()["nope"]

    def test_iter_yields_parameter_names(self):
        """Iterating the catalog yields its parameter names."""
        cat = Catalog()
        assert set(cat) == set(cat.parameters)


@pytest.mark.openaq
class TestParametersDatasetsAlias:
    """`parameters` aliases the base `datasets` field, both directions."""

    def test_parameters_is_datasets(self):
        """The parameters property returns the same object as datasets."""
        cat = Catalog()
        assert cat.parameters is cat.datasets

    def test_datasets_construction_skips_disk(self):
        """Constructing with datasets= populates the catalog directly."""
        cat = Catalog(datasets={"x": Parameter(name="x", ids=[1])})
        assert sorted(cat.parameters) == ["x"]
        assert cat["x"].ids == [1]

    def test_parameters_kwarg_routes_to_datasets(self):
        """The legacy parameters= kwarg lands in the datasets field."""
        cat = Catalog(parameters={"x": Parameter(name="x", ids=[1])})
        assert "x" in cat.datasets
