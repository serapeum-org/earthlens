"""Tests for computed (derived) spectral-index bands (#1211).

Covers the catalog `DerivedBand` schema, the curated NDVI/NDWI/EVI rows, and
the server-side materialisation in the backend. `ee` is faked with a node
recorder so each test asserts the exact `normalizedDifference` / `divide` /
`expression` call and the rename, not the pixel values.
"""

from __future__ import annotations

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee.backend import (  # noqa: E402
    _AddDerivedBands,
    _materialise_derived_band,
)
from earthlens.gee.catalog import BandExpression, Catalog, DerivedBand  # noqa: E402


class _Node:
    """A fake ee.Image recording the band arithmetic applied to it."""

    def __init__(self, kind: str, args=None):
        self.kind = kind
        self.args = args
        self.name: str | None = None
        self.added: list[tuple] = []

    def normalizedDifference(self, bands):  # noqa: N802 - mirrors ee API
        return _Node("nd", tuple(bands))

    def select(self, band):
        return _Node("sel", band)

    def divide(self, other):
        return _Node("ratio", (self.args, other.args))

    def expression(self, formula, bindings):
        return _Node("expr", (formula, {k: v.args for k, v in bindings.items()}))

    def rename(self, name):
        self.name = name
        return self

    def addBands(self, other, overwrite=False):  # noqa: N802 - mirrors ee API
        self.added.append((other.name, other.kind, other.args, overwrite))
        return self


class TestDerivedBandModel:
    """`DerivedBand` accepts exactly one operation and reports its sources."""

    def test_normalized_difference(self):
        """A normalized-difference band carries its two source bands."""
        d = DerivedBand(normalized_difference=("B8", "B4"))
        assert d.source_bands() == ("B8", "B4")

    def test_ratio(self):
        """A ratio band carries its numerator and denominator."""
        assert DerivedBand(ratio=("B8", "B4")).source_bands() == ("B8", "B4")

    def test_expression_sources_are_the_band_bindings(self):
        """An expression band's sources are the bound band ids."""
        d = DerivedBand(
            expression=BandExpression(
                formula="NIR - RED", bands={"NIR": "B8", "RED": "B4"}
            )
        )
        assert set(d.source_bands()) == {"B8", "B4"}

    def test_no_operation_is_refused(self):
        """A derived band with no operation is ambiguous and refused."""
        with pytest.raises(ValidationError, match="exactly one of"):
            DerivedBand()

    def test_two_operations_are_refused(self):
        """Giving two operations is refused."""
        with pytest.raises(ValidationError, match="exactly one of"):
            DerivedBand(normalized_difference=("B8", "B4"), ratio=("B8", "B4"))

    def test_expression_needs_at_least_one_band(self):
        """An empty band binding map is refused."""
        with pytest.raises(ValidationError):
            BandExpression(formula="X", bands={})


class TestCuratedCatalogRows:
    """The curated NDVI / NDWI / EVI rows load with the right formulas."""

    def test_s2_ndvi_and_ndwi(self):
        """Sentinel-2 declares NDVI (B8/B4) and NDWI (B3/B8)."""
        s2 = Catalog().get_dataset("COPERNICUS/S2_SR_HARMONIZED")
        assert s2.get_band("NDVI").derived_from.normalized_difference == ("B8", "B4")
        assert s2.get_band("NDWI").derived_from.normalized_difference == ("B3", "B8")

    def test_s2_evi_is_an_expression(self):
        """EVI is an expression over NIR / RED / BLUE."""
        evi = Catalog().get_dataset("COPERNICUS/S2_SR_HARMONIZED").get_band("EVI")
        assert evi.derived_from.expression.bands == {
            "NIR": "B8",
            "RED": "B4",
            "BLUE": "B2",
        }

    def test_landsat_ndvi(self):
        """Landsat 9 C2-L2 declares NDVI from SR_B5 / SR_B4."""
        ls = Catalog().get_dataset("LANDSAT/LC09/C02/T1_L2")
        assert ls.get_band("NDVI").derived_from.normalized_difference == (
            "SR_B5",
            "SR_B4",
        )

    def test_a_raw_band_is_not_derived(self):
        """A native band carries no `derived_from`."""
        assert (
            Catalog()
            .get_dataset("COPERNICUS/S2_SR_HARMONIZED")
            .get_band("B4")
            .derived_from
            is None
        )


class TestMaterialise:
    """`_materialise_derived_band` builds the right server-side call."""

    def test_normalized_difference(self):
        """`normalized_difference` -> `image.normalizedDifference([a, b]).rename(name)`."""
        out = _materialise_derived_band(
            _Node("img"), "NDVI", DerivedBand(normalized_difference=("B8", "B4"))
        )
        assert (out.kind, out.args, out.name) == ("nd", ("B8", "B4"), "NDVI")

    def test_ratio(self):
        """`ratio` -> `image.select(a).divide(image.select(b)).rename(name)`."""
        out = _materialise_derived_band(
            _Node("img"), "SR", DerivedBand(ratio=("B8", "B4"))
        )
        assert (out.kind, out.args, out.name) == ("ratio", ("B8", "B4"), "SR")

    def test_expression(self):
        """`expression` -> `image.expression(formula, bindings).rename(name)`."""
        derived = DerivedBand(
            expression=BandExpression(
                formula="NIR - RED", bands={"NIR": "B8", "RED": "B4"}
            )
        )
        out = _materialise_derived_band(_Node("img"), "EVI", derived)
        assert out.kind == "expr" and out.name == "EVI"
        formula, sources = out.args
        assert formula == "NIR - RED" and sources == {"NIR": "B8", "RED": "B4"}


class TestAddDerivedBands:
    """`_AddDerivedBands` adds each index band with overwrite, keeping sources."""

    def test_adds_each_requested_index(self):
        """Every (name, formula) pair is `addBands`-ed with overwrite=True."""
        op = _AddDerivedBands(
            (
                ("NDVI", DerivedBand(normalized_difference=("B8", "B4"))),
                ("NDWI", DerivedBand(normalized_difference=("B3", "B8"))),
            )
        )
        out = op(_Node("img"))
        added_names = [(name, overwrite) for name, _k, _a, overwrite in out.added]
        assert added_names == [("NDVI", True), ("NDWI", True)]


class TestDerivedSourceValidation:
    """A derived band's source bands must exist among the dataset's native bands."""

    def test_a_derived_band_naming_a_missing_source_is_refused(self):
        """A source band that is not a native band fails catalog load, not at runtime."""
        from earthlens.gee.catalog import Band, Dataset, Extent

        with pytest.raises(ValidationError, match="not native bands"):
            Dataset(
                id="X/Y",
                title="t",
                extent=Extent(start_date="2020-01-01", end_date="2020-01-02"),
                bands={
                    "B4": Band(id="B4"),
                    "NDVI": Band(
                        id="NDVI",
                        derived_from=DerivedBand(normalized_difference=("B8", "B4")),
                    ),
                },
            )

    def test_a_derived_band_whose_sources_are_native_loads(self):
        """When every source band exists, the dataset builds without complaint."""
        from earthlens.gee.catalog import Band, Dataset, Extent

        row = Dataset(
            id="X/Y",
            title="t",
            extent=Extent(start_date="2020-01-01", end_date="2020-01-02"),
            bands={
                "B8": Band(id="B8"),
                "B4": Band(id="B4"),
                "NDVI": Band(
                    id="NDVI",
                    derived_from=DerivedBand(normalized_difference=("B8", "B4")),
                ),
            },
        )
        assert set(row.bands) == {"B8", "B4", "NDVI"}
