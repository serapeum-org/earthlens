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
    # dug has one sub-alias at 100 m (matching the default), so no warning.
    assert catalog.pick_subalias("dug", resolution="100m") == "dug_g2_v1"


def test_pick_subalias_single_product_resolution_mismatch_warns(catalog):
    """Requesting a resolution the sole sub-alias doesn't offer warns."""
    with pytest.warns(UserWarning, match="resolution='1km' is ignored"):
        assert catalog.pick_subalias("urban_change", resolution="1km") == "ucic"


def test_pick_subalias_pwd_level(catalog):
    """The pwd product disambiguates by national / subnational level."""
    assert (
        catalog.pick_subalias("pwd", resolution="1km", level="national")
        == "pwd_national_1km"
    )
    assert (
        catalog.pick_subalias("pwd", resolution="100m", level="subnational")
        == "pwd_subnational_100m"
    )


def test_pick_subalias_impossible_combo_raises(catalog):
    """An unavailable selector tuple raises listing the sub-aliases."""
    with pytest.raises(ValueError, match="has no variant"):
        catalog.pick_subalias(
            "pop",
            constrained=True,
            resolution="1km",
            generation="R2021",
            scope="countries",
        )


def test_demographic_flag(catalog):
    """age_structures is flagged demographic + mixed; pop is not."""
    assert catalog.get("age_structures").demographic is True
    assert catalog.get("age_structures").kind == "mixed"
    assert catalog.get("pop").demographic is False


def test_subalias_returns_row(catalog):
    """subalias() returns the SubAlias row for a product + id."""
    sub = catalog.subalias("pop", "wpgp")
    assert sub.id == "wpgp" and sub.scope == "countries"


def test_subalias_unknown_id_raises(catalog):
    """An id not belonging to the product raises listing the valid ids."""
    with pytest.raises(ValueError, match="has no sub-alias"):
        catalog.subalias("pop", "not_a_subalias")


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


def test_get_catalog_returns_datasets(catalog):
    """get_catalog() returns the same product map as datasets."""
    assert catalog.get_catalog() is catalog.datasets


def test_product_selectors_lists_tuples(catalog):
    """Product.selectors() returns one selector tuple per sub-alias."""
    pop = catalog.get("pop")
    selectors = pop.selectors()
    assert len(selectors) == len(pop.subaliases)
    assert all(len(sel) == 6 for sel in selectors)


def test_load_rejects_malformed_row(tmp_path):
    """A product row with an unknown key fails load with a clear error."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("products:\n  pop:\n    bogus_key: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)


def test_parse_cache_reuses_on_unchanged_file():
    """Two Catalog() builds reuse the cached parse for the unchanged file."""
    from earthlens.worldpop import catalog as catalog_mod
    from earthlens.worldpop import clear_catalog_cache

    clear_catalog_cache()
    Catalog()
    assert len(catalog_mod._CATALOG_CACHE) == 1
    Catalog()
    assert len(catalog_mod._CATALOG_CACHE) == 1
    clear_catalog_cache()
    assert len(catalog_mod._CATALOG_CACHE) == 0


def test_health_clean_on_bundled_catalog(catalog):
    """The bundled catalog reports no hygiene problems."""
    report = catalog.health()
    assert report == {
        "product_without_subaliases": [],
        "demographic_not_mixed": [],
        "subalias_unknown_generation": [],
        "subalias_bad_years": [],
    }


def test_health_flags_problems():
    """health() flags an empty product and a demographic non-mixed product."""
    from earthlens.worldpop.catalog import Product, SubAlias

    cat = Catalog(
        datasets={
            "empty": Product(alias="empty"),
            "demo": Product(
                alias="demo",
                demographic=True,
                kind="raster",
                subaliases=[SubAlias(id="x")],
            ),
            "weird": Product(
                alias="weird",
                subaliases=[SubAlias(id="y", generation="BOGUS", years="not-a-year")],
            ),
        }
    )
    report = cat.health()
    assert "empty" in report["product_without_subaliases"]
    assert "demo" in report["demographic_not_mixed"]
    assert "weird:y" in report["subalias_unknown_generation"]
    assert "weird:y" in report["subalias_bad_years"]


def test_describe_returns_record(catalog):
    """describe() returns the product metadata + its sub-alias rows."""
    info = catalog.describe("population")
    assert info["product"] == "pop"
    assert info["kind"] == "raster"
    assert info["subaliases"][0]["id"] == "wpgp"
    assert {"scope", "resolution", "years"} <= set(info["subaliases"][0])


def test_covariates_curated(catalog):
    """All 54 covariate layers are curated as products routing to 'covariates'."""
    covs = [
        p
        for p in catalog.available_products()
        if catalog.get(p).rest_alias == "covariates"
    ]
    assert len(covs) == 54
    assert catalog.get("cviirs").endpoint() == "covariates"
    assert catalog.get("pop").endpoint() == "pop"  # non-covariate uses its key


def test_covariate_describe_has_description(catalog):
    """A covariate's describe() carries the hub title."""
    info = catalog.describe("cviirs")
    assert info["product"] == "cviirs"
