"""Tests for the WorldPop product / sub-alias catalog."""

from __future__ import annotations

import pytest

from earthlens.worldpop.catalog import Catalog, _years_set

pytestmark = pytest.mark.worldpop


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """Return the bundled WorldPop catalog (loaded once)."""
    return Catalog()


def test_years_set_single():
    """A single-year spec expands to one year."""
    assert _years_set("2020") == {2020}


def test_years_set_range():
    """A range spec expands inclusively."""
    assert _years_set("2018-2020") == {2018, 2019, 2020}


def test_resolve_canonical_and_friendly(catalog):
    """Both a canonical alias and a friendly alias resolve to the product."""
    assert catalog.resolve("pop") == "pop"
    assert catalog.resolve("population") == "pop"
    assert catalog.resolve("age_sex") == "age_structures"


def test_resolve_is_case_insensitive(catalog):
    """Alias resolution ignores case."""
    assert catalog.resolve("POPULATION") == "pop"


def test_resolve_unknown_raises_did_you_mean(catalog):
    """An unknown key raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="not a known WorldPop product"):
        catalog.resolve("populaton")


def test_available_products_sorted(catalog):
    """available_products returns the curated aliases, sorted."""
    products = catalog.available_products()
    assert products == sorted(products)
    assert "pop" in products and "age_structures" in products


def test_pick_subalias_default_is_wpgp(catalog):
    """The default selectors pick the classic unconstrained 100 m series."""
    assert catalog.pick_subalias("pop") == "wpgp"


def test_pick_subalias_unadjusted_false_is_un_adjusted(catalog):
    """unadjusted=False selects the UN-adjusted sub-alias."""
    assert catalog.pick_subalias("pop", unadjusted=False) == "wpgpunadj"


def test_pick_subalias_constrained_r2025a(catalog):
    """constrained + R2025A resolves to the Global-2 sub-alias."""
    got = catalog.pick_subalias("pop", constrained=True, generation="R2025A")
    assert got == "G2_CN_POP_R25A_100m"


def test_pick_subalias_global_mosaic(catalog):
    """scope=global at 1 km resolves to the global mosaic sub-alias."""
    assert catalog.pick_subalias("pop", resolution="1km", scope="global") == "wpgp1km"


def test_pick_subalias_single_product_ignores_selectors(catalog):
    """A single-sub-alias product returns its id regardless of selectors."""
    assert catalog.pick_subalias("births") == "bic"
    assert catalog.pick_subalias("future_pop", resolution="100m") == "FPP_v02"


def test_pick_subalias_pwd_level(catalog):
    """The pwd product disambiguates by national / subnational level."""
    assert catalog.pick_subalias("pwd", resolution="1km", level="national") == "pwd_national_1km"
    assert (
        catalog.pick_subalias("pwd", resolution="100m", level="subnational")
        == "pwd_subnational_100m"
    )


def test_pick_subalias_impossible_combo_raises(catalog):
    """An unavailable selector tuple raises listing the sub-aliases."""
    with pytest.raises(ValueError, match="has no variant"):
        catalog.pick_subalias(
            "pop", constrained=True, resolution="1km", generation="R2021", scope="countries"
        )


def test_demographic_flag(catalog):
    """age_structures is flagged demographic + mixed; pop is not."""
    assert catalog.get("age_structures").demographic is True
    assert catalog.get("age_structures").kind == "mixed"
    assert catalog.get("pop").demographic is False


def test_validate_returns_product_and_subalias(catalog):
    """validate returns the canonical (product, subalias) for a good year."""
    assert catalog.validate("pop", year=2020) == ("pop", "wpgp")


def test_validate_bad_year_raises(catalog):
    """A year outside the sub-alias range raises listing the available years."""
    with pytest.raises(ValueError, match="does not offer year 1990"):
        catalog.validate("pop", year=1990)


def test_extra_keys_forbidden():
    """The Product model forbids unknown keys."""
    from pydantic import ValidationError

    from earthlens.worldpop.catalog import Product

    with pytest.raises(ValidationError):
        Product(alias="x", bogus=1)


def test_load_rejects_empty_block(tmp_path):
    """Loading a catalog with no products block raises a clear error."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("products:\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'products:' block"):
        Catalog.load(bad)


def test_dict_surface(catalog):
    """The catalog exposes the dict-like membership + len surface."""
    assert "pop" in catalog
    assert len(catalog) == len(catalog.available_products())
