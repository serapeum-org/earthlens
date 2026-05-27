"""Unit tests for the NWM product + configuration catalog."""

from __future__ import annotations

import textwrap

import pytest

from earthlens.nwm import Catalog, NWMProduct, NWMVariable
from earthlens.nwm.catalog import NWMConfig, clear_catalog_cache

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


def test_products_listed(catalog):
    """The bundled catalog exposes the two MVP products."""
    assert catalog.products() == ["chrtout", "ldasout"]


def test_per_product_output_kind(catalog):
    """chrtout is tabular (feature_id) and ldasout is raster (gridded)."""
    assert catalog.get_product("chrtout").output_kind == "tabular"
    assert catalog.get_product("ldasout").output_kind == "raster"


def test_product_dims_and_token(catalog):
    """Each product carries its dimensions and S3 file-name token."""
    chrtout = catalog.get_product("chrtout")
    assert chrtout.dims == ["feature_id", "time"]
    assert chrtout.s3_token == "channel_rt"
    assert catalog.get_product("ldasout").s3_token == "land"


def test_variable_metadata(catalog):
    """Variable rows carry units and a long name."""
    streamflow = catalog.get_product("chrtout").variables["streamflow"]
    assert streamflow.units == "m3 s-1"
    assert "flow" in streamflow.long_name.lower()


def test_unknown_product_did_you_mean(catalog):
    """An unknown product key raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'chrtout'"):
        catalog.get_product("chrtou")


def test_configs_listed(catalog):
    """The three CONUS configurations are present."""
    assert sorted(catalog.configurations) == [
        "analysis_assim",
        "medium_range",
        "short_range",
    ]


def test_config_attributes(catalog):
    """short_range is an 18 h hourly forecast; analysis_assim uses tm steps."""
    sr = catalog.get_config("short_range")
    assert sr.horizon_h == 18 and sr.step_kind == "forecast"
    assert catalog.get_config("analysis_assim").step_kind == "analysis"
    assert catalog.get_config("medium_range").members == 6


def test_unknown_config_did_you_mean(catalog):
    """An unknown configuration key raises ValueError with a hint."""
    with pytest.raises(ValueError, match="Did you mean 'short_range'"):
        catalog.get_config("short_rang")


def test_dict_surface(catalog):
    """The catalog supports membership, indexing and length over products."""
    assert "chrtout" in catalog
    assert catalog["ldasout"].output_kind == "raster"
    assert len(catalog) == 2


def test_extra_field_rejected(tmp_path):
    """A product row with an unexpected field fails validation."""
    path = tmp_path / "bad.yaml"
    path.write_text(
        textwrap.dedent(
            """
            products:
              chrtout:
                output_kind: tabular
                s3_token: channel_rt
                bogus_field: 1
            """
        )
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(path)


def test_missing_products_block(tmp_path):
    """A catalog without a products block is rejected."""
    path = tmp_path / "empty.yaml"
    path.write_text("configurations: {}\n")
    clear_catalog_cache()
    with pytest.raises(ValueError, match="empty 'products:' block"):
        Catalog.load(path)


def test_duplicate_key_rejected(tmp_path):
    """A duplicate YAML key is rejected at parse time."""
    path = tmp_path / "dup.yaml"
    path.write_text(
        textwrap.dedent(
            """
            products:
              chrtout:
                output_kind: tabular
                s3_token: channel_rt
              chrtout:
                output_kind: raster
                s3_token: land
            """
        )
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="duplicate YAML key"):
        Catalog.load(path)


def test_parse_cache_returns_same_object(tmp_path):
    """Loading the same path twice hits the parse cache (identity)."""
    path = tmp_path / "ok.yaml"
    path.write_text(
        textwrap.dedent(
            """
            products:
              chrtout:
                output_kind: tabular
                s3_token: channel_rt
            """
        )
    )
    clear_catalog_cache()
    first = Catalog.load(path)
    second = Catalog.load(path)
    assert first is second


def test_injected_catalog_skips_disk(tmp_path):
    """Passing datasets= builds a catalog without reading the bundled YAML."""
    product = NWMProduct(
        product="chrtout",
        output_kind="tabular",
        s3_token="channel_rt",
        configurations=["short_range"],
        variables={"streamflow": NWMVariable(units="m3 s-1")},
    )
    cfg = {"short_range": Catalog().get_config("short_range")}
    cat = Catalog(datasets={"chrtout": product}, configurations=cfg)
    assert cat.products() == ["chrtout"]


def test_get_catalog_returns_datasets(catalog):
    """get_catalog returns the same map as datasets."""
    assert catalog.get_catalog() is catalog.datasets


def test_config_is_frozen():
    """Configuration rows are immutable."""
    cfg = NWMConfig(key="x", family="x")
    with pytest.raises(Exception):
        cfg.horizon_h = 5


def test_bad_configuration_row_rejected(tmp_path):
    """A configuration row with a bad field type fails validation."""
    path = tmp_path / "badcfg.yaml"
    path.write_text(
        textwrap.dedent(
            """
            products:
              chrtout:
                output_kind: tabular
                s3_token: channel_rt
            configurations:
              short_range:
                step_kind: not-a-valid-kind
            """
        )
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="configuration 'short_range' failed"):
        Catalog.load(path)


def test_unknown_config_without_close_match(catalog):
    """An unrelated configuration key raises without a did-you-mean hint."""
    with pytest.raises(ValueError, match="is not an NWM configuration"):
        catalog.get_config("zzzzzz")
