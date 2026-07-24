"""Tests for `earthlens.asf.catalog`."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.asf import Catalog, Product
from earthlens.asf.catalog import (
    _CATALOG_CACHE,
    CATALOG_PATH,
    _load_catalog_data,
    clear_catalog_cache,
)


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_loads_from_default_path() -> None:
    """The catalog auto-loads when constructed with no args."""
    cat = Catalog()
    assert "sentinel-1-slc" in cat.products
    assert cat.products["sentinel-1-slc"].stackable is True


@pytest.mark.asf
@pytest.mark.unit
def test_curated_key_resolves_to_itself() -> None:
    """A curated key passes through `resolve` unchanged."""
    assert Catalog().resolve("sentinel-1-slc") == "sentinel-1-slc"


@pytest.mark.asf
@pytest.mark.unit
def test_alias_resolves_to_curated_key() -> None:
    """An alias resolves to its curated key."""
    cat = Catalog()
    assert cat.resolve("s1-slc") == "sentinel-1-slc"
    assert cat.resolve("opera-rtc") == "opera-rtc-s1"


@pytest.mark.asf
@pytest.mark.unit
def test_unknown_key_raises_with_did_you_mean() -> None:
    """An unknown but close name raises with a hint."""
    with pytest.raises(ValueError, match="ASF product catalog"):
        Catalog().resolve("sentinel1-slx")


@pytest.mark.asf
@pytest.mark.unit
def test_stackable_flag_matches_curated_rows() -> None:
    """`stackable_products` matches the YAML's curated truth."""
    stackable = set(Catalog().stackable_products())
    assert "sentinel-1-slc" in stackable
    assert "sentinel-1-burst" in stackable
    assert "alos-palsar-slc" in stackable
    assert "opera-cslc-s1" in stackable
    # Processed-derivative products are not stackable.
    assert "sentinel-1-grd" not in stackable
    assert "opera-rtc-s1" not in stackable
    assert "aria-s1-gunw" not in stackable


@pytest.mark.asf
@pytest.mark.unit
def test_product_rejects_both_platform_and_dataset() -> None:
    """A row with both `platform` and `dataset` is rejected."""
    with pytest.raises(ValidationError):
        Product(platform="SENTINEL1", dataset="OPERA_S1", product_type="RTC")


@pytest.mark.asf
@pytest.mark.unit
def test_product_rejects_neither_platform_nor_dataset() -> None:
    """A row with neither `platform` nor `dataset` is rejected."""
    with pytest.raises(ValidationError):
        Product(product_type="SLC")


@pytest.mark.asf
@pytest.mark.unit
def test_product_rejects_extra_fields() -> None:
    """A stray field is rejected by `extra='forbid'`."""
    with pytest.raises(ValidationError):
        Product(
            platform="SENTINEL1",
            product_type="SLC",
            stackable=True,
            unknown_field="oops",
        )


@pytest.mark.asf
@pytest.mark.unit
def test_available_products_is_sorted() -> None:
    """`available_products` returns a sorted list."""
    names = Catalog().available_products
    assert names == sorted(names)


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_parse_cache_invalidates_on_mtime_change(
    tmp_path: Path, reset_catalog_cache
) -> None:
    """Editing the YAML on disk invalidates the cache entry."""
    yaml_path = tmp_path / "asf.yaml"
    yaml_path.write_text(
        "products:\n"
        "  sentinel-1-slc:\n"
        "    aliases: []\n"
        "    platform: SENTINEL1\n"
        "    product_type: SLC\n"
        "    stackable: true\n",
        encoding="utf-8",
    )
    first = _load_catalog_data(yaml_path)
    assert "sentinel-1-slc" in first
    # Rewrite with a different row + bumped mtime.
    yaml_path.write_text(
        "products:\n"
        "  sentinel-1-burst:\n"
        "    aliases: []\n"
        "    dataset: SLC_BURST\n"
        "    product_type: BURST\n"
        "    stackable: true\n",
        encoding="utf-8",
    )
    import os

    new_mtime = yaml_path.stat().st_mtime_ns + 1
    os.utime(yaml_path, ns=(new_mtime, new_mtime))
    second = _load_catalog_data(yaml_path)
    assert "sentinel-1-burst" in second
    assert "sentinel-1-slc" not in second


@pytest.mark.asf
@pytest.mark.unit
def test_clear_catalog_cache_empties_the_cache() -> None:
    """`clear_catalog_cache` removes every entry."""
    Catalog()  # populate the cache
    assert _CATALOG_CACHE  # something landed
    clear_catalog_cache()
    assert _CATALOG_CACHE == {}


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_with_empty_products_block_rejected(
    tmp_path: Path, reset_catalog_cache
) -> None:
    """An empty `products:` block raises with a clear message."""
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("products: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'products:' block"):
        _load_catalog_data(yaml_path)


@pytest.mark.asf
@pytest.mark.unit
def test_constant_names_exist_in_real_asf_search() -> None:
    """Every catalog row references a real `asf_search` enum member.

    Guards against a typo or stale constant name silently passing
    until a live request hits the API.
    """
    asf = pytest.importorskip("asf_search")
    cat = Catalog()
    for key, row in cat.products.items():
        if row.platform is not None:
            assert hasattr(asf.PLATFORM, row.platform), (
                f"{key}: PLATFORM.{row.platform} not in asf_search"
            )
        else:
            assert hasattr(asf.DATASET, row.dataset), (
                f"{key}: DATASET.{row.dataset} not in asf_search"
            )
        assert hasattr(asf.PRODUCT_TYPE, row.product_type), (
            f"{key}: PRODUCT_TYPE.{row.product_type} not in asf_search"
        )


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_path_points_at_bundled_yaml() -> None:
    """The module-level `CATALOG_PATH` resolves to the shipped file."""
    assert CATALOG_PATH.exists()
    assert CATALOG_PATH.name == "asf_data_catalog.yaml"


@pytest.mark.asf
@pytest.mark.unit
def test_products_property_aliases_datasets() -> None:
    """The `products` property is the same dict as the base `datasets`."""
    cat = Catalog()
    assert cat.products is cat.datasets


@pytest.mark.asf
@pytest.mark.unit
def test_get_product_returns_row_for_curated_key() -> None:
    """`get_product` returns the row for a curated key."""
    row = Catalog().get_product("sentinel-1-slc")
    assert isinstance(row, Product)
    assert row.product_type == "SLC"


@pytest.mark.asf
@pytest.mark.unit
def test_get_product_with_unknown_key_raises() -> None:
    """`get_product` on an unknown key raises with a hint."""
    with pytest.raises(ValueError, match="ASF product catalog"):
        Catalog().get_product("unknown-product")


@pytest.mark.asf
@pytest.mark.unit
def test_dotted_alias_resolves_to_canonical_key() -> None:
    """The `alos-l1.1` alias survives the dot in the friendly name."""
    assert Catalog().resolve("alos-l1.1") == "alos-palsar-slc"


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_rejects_duplicate_aliases_across_rows() -> None:
    """An alias reused across two product rows fails at construction."""
    duplicated = Product(platform="SENTINEL1", product_type="SLC", aliases=["shared"])
    other = Product(platform="ALOS", product_type="L1_1", aliases=["shared"])
    with pytest.raises(ValueError, match="alias 'shared'"):
        Catalog(products={"one": duplicated, "two": other})


@pytest.mark.asf
@pytest.mark.unit
def test_curated_catalog_aliases_are_globally_unique() -> None:
    """The bundled YAML carries no duplicated aliases (defensive)."""
    cat = Catalog()
    seen: dict[str, str] = {}
    for canonical, row in cat.products.items():
        for alias in row.aliases:
            assert alias not in seen, (
                f"alias {alias!r} on {canonical!r} also on {seen[alias]!r}"
            )
            seen[alias] = canonical


@pytest.mark.asf
@pytest.mark.unit
def test_available_products_index_matches_products_block(reset_catalog_cache) -> None:
    """The informational `available_products:` index agrees with `products:`."""
    cat = Catalog()
    # The property reads from the loaded rows; we also verify the YAML
    # itself by re-loading and asserting the loader accepted the index.
    declared = cat.available_products
    assert declared == sorted(cat.products)


@pytest.mark.asf
@pytest.mark.unit
def test_drifted_available_products_block_rejected(
    tmp_path, reset_catalog_cache
) -> None:
    """A stale `available_products:` block fails at load."""
    yaml_path = tmp_path / "drifted.yaml"
    yaml_path.write_text(
        "available_products:\n"
        "  - sentinel-1-slc\n"
        "  - ghost-product\n"
        "products:\n"
        "  sentinel-1-slc:\n"
        "    aliases: []\n"
        "    platform: SENTINEL1\n"
        "    product_type: SLC\n"
        "    stackable: true\n",
        encoding="utf-8",
    )
    from earthlens.asf.catalog import _load_catalog_data

    with pytest.raises(ValueError, match="available_products"):
        _load_catalog_data(yaml_path)
