"""Unit tests for `earthlens.s3.layouts` (per-dataset key resolvers)."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.s3.catalog import Catalog
from earthlens.s3.layouts import plan_products

pytestmark = [pytest.mark.s3]


@pytest.fixture
def catalog():
    """The registry catalog."""
    return Catalog()


def test_copernicus_dem_single_tile(catalog):
    """A bbox inside one 1-degree tile yields that tile's COG key."""
    dem = catalog.resolve("copernicus-dem")
    products = plan_products(dem, dem.resolve_variables(None), (6.4, 0.4, 6.6, 0.6), [], None)
    assert [p.href for p in products] == [
        "Copernicus_DSM_COG_10_N00_00_E006_00_DEM/"
        "Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif"
    ]


def test_copernicus_dem_spans_multiple_tiles(catalog):
    """A 2-degree-wide bbox yields one product per covered 1-degree tile."""
    dem = catalog.resolve("copernicus-dem")
    products = plan_products(dem, dem.resolve_variables(None), (5.5, 0.5, 7.5, 0.5), [], None)
    assert [p.metadata["tile"] for p in products] == ["N00E005", "N00E006", "N00E007"]


def test_copernicus_dem_southern_western_hemisphere(catalog):
    """Negative lat/lon format as S/W with correct zero-padding."""
    dem = catalog.resolve("copernicus-dem")
    products = plan_products(dem, dem.resolve_variables(None), (-74.2, -1.2, -74.1, -1.1), [], None)
    assert products[0].metadata["tile"] == "S02W075"


def test_esa_worldcover_three_degree_tile(catalog):
    """WorldCover keys carry the version/epoch and a 3-degree tile id."""
    wc = catalog.resolve("esa-worldcover")
    products = plan_products(wc, wc.resolve_variables(None), (6.4, 0.4, 6.6, 0.6), [], None)
    assert products[0].href == (
        "v200/2021/map/ESA_WorldCover_10m_2021_v200_N00E006_Map.tif"
    )


def test_era5_matches_variable_token_in_listing(catalog, fake_client_factory):
    """ERA5 plans one monthly NetCDF per (variable, month) from the listing."""
    era5 = catalog.resolve("era5")
    client = fake_client_factory(
        listing={
            "nsf-ncar-era5": [
                "e5.oper.an.sfc/202406/e5.oper.an.sfc.128_167_2t.ll025sc.x.nc",
                "e5.oper.an.sfc/202406/e5.oper.an.sfc.128_165_10u.ll025sc.x.nc",
            ]
        }
    )
    products = plan_products(
        era5, [era5.resolve_variable("t2m")], (0, 0, 1, 1),
        pd.date_range("2024-06-01", "2024-06-02"), client,
    )
    assert len(products) == 1 and "128_167_2t" in products[0].href


def test_goes_matches_channel_at_first_hour(catalog, fake_client_factory):
    """GOES plans one frame per (channel, day) at the first available hour."""
    goes = catalog.resolve("goes")
    client = fake_client_factory(
        listing={
            "noaa-goes16": [
                "ABI-L2-CMIPF/2024/180/00/OR_ABI-L2-CMIPF-M6C02_G16_s.nc",
                "ABI-L2-CMIPF/2024/180/00/OR_ABI-L2-CMIPF-M6C13_G16_s.nc",
            ]
        }
    )
    products = plan_products(
        goes, [goes.resolve_variable("C02")], (-100, 30, -99, 31),
        pd.to_datetime(["2024-06-28"]), client,
    )
    assert len(products) == 1 and "M6C02_G16" in products[0].href


def test_sentinel2_lists_scenes_over_mgrs_tiles(catalog, fake_client_factory):
    """Sentinel-2 plans one band COG per scene under the bbox's MGRS tile."""
    s2 = catalog.resolve("sentinel-2-l2a")
    client = fake_client_factory(
        listing={
            "sentinel-cogs": [
                "sentinel-s2-l2a-cogs/31/U/DQ/2024/6/0/B04.tif",
                "sentinel-s2-l2a-cogs/31/U/DQ/2024/6/1/B04.tif",
            ]
        }
    )
    products = plan_products(
        s2, [s2.resolve_variable("red")], (2.2, 48.8, 2.5, 48.9),
        pd.date_range("2024-06-01", "2024-06-15"), client,
    )
    assert len(products) == 2 and all(p.href.endswith("B04.tif") for p in products)


def test_sentinel2_max_scenes_caps_and_dedupes_ids(catalog, fake_client_factory):
    """max_scenes keeps the most-recent scenes; each scene gets a distinct product id (M4/N1)."""
    s2 = catalog.resolve("sentinel-2-l2a")
    s2 = s2.model_copy(update={"params": {**s2.params, "max_scenes": 1}})
    client = fake_client_factory(
        listing={
            "sentinel-cogs": [
                "sentinel-s2-l2a-cogs/31/U/DQ/2024/6/0/B04.tif",
                "sentinel-s2-l2a-cogs/31/U/DQ/2024/6/1/B04.tif",
                "sentinel-s2-l2a-cogs/31/U/DQ/2024/6/2/B04.tif",
            ]
        }
    )
    products = plan_products(
        s2, [s2.resolve_variable("red")], (2.2, 48.8, 2.5, 48.9),
        pd.date_range("2024-06-01", "2024-06-15"), client,
    )
    assert len(products) == 1  # capped from 3 scenes
    assert products[0].metadata["scene"].endswith("/2/")  # kept the most recent
    assert "_2_" in products[0].id  # scene sequence is part of the id (no collisions)


def test_passthrough_template_formats_per_date(catalog):
    """A passthrough key_template is formatted per variable and date."""
    ds = catalog.resolve(
        {
            "bucket": "b",
            "format": "cog",
            "layout": "deterministic_tiles",
            "params": {"key_template": "{year}/{variable}.tif"},
        }
    )
    s2 = catalog.resolve("sentinel-2-l2a")
    products = plan_products(ds, [s2.resolve_variable("red")], (0, 0, 1, 1), pd.to_datetime(["2024-06-01"]), None)
    assert products[0].href == "2024/B04.tif"


def test_passthrough_without_template_raises(catalog):
    """A passthrough lacking key_template fails clearly."""
    ds = catalog.resolve({"bucket": "b", "format": "cog", "layout": "deterministic_tiles"})
    with pytest.raises(ValueError, match="key_template"):
        plan_products(ds, [], (0, 0, 1, 1), [], None)


def test_unknown_builder_raises(catalog):
    """A bogus builder token is reported with the known set."""
    ds = catalog.resolve(
        {"bucket": "b", "format": "cog", "layout": "deterministic_tiles",
         "params": {"builder": "bogus"}}
    )
    with pytest.raises(ValueError, match="no S3 key resolver"):
        plan_products(ds, [], (0, 0, 1, 1), [], None)


def test_landsat_builds_keys_from_a_scene_id(catalog):
    """Landsat parses sensor/path/row/year from the scene id; one key per band."""
    ls = catalog.resolve("usgs-landsat")
    ls = ls.model_copy(
        update={"params": {**ls.params, "scene": "LC08_L2SP_039037_20210901_20210910_02_T1"}}
    )
    products = plan_products(ls, ls.resolve_variables(["red", "nir"]), (0, 0, 1, 1), [], None)
    assert [p.href for p in products] == [
        "collection02/level-2/standard/oli-tirs/2021/039/037/"
        "LC08_L2SP_039037_20210901_20210910_02_T1/"
        "LC08_L2SP_039037_20210901_20210910_02_T1_SR_B4.TIF",
        "collection02/level-2/standard/oli-tirs/2021/039/037/"
        "LC08_L2SP_039037_20210901_20210910_02_T1/"
        "LC08_L2SP_039037_20210901_20210910_02_T1_SR_B5.TIF",
    ]


def test_landsat_without_scene_raises(catalog):
    """Landsat needs an explicit scene id (no bbox->WRS-2 discovery)."""
    ls = catalog.resolve("usgs-landsat")
    with pytest.raises(ValueError, match="needs scene="):
        plan_products(ls, ls.resolve_variables(["red"]), (0, 0, 1, 1), [], None)


def test_landsat_unknown_sensor_raises(catalog):
    """An unrecognised Landsat sensor prefix is reported clearly."""
    ls = catalog.resolve("usgs-landsat")
    ls = ls.model_copy(update={"params": {**ls.params, "scene": "ZZ99_L2SP_039037_2021_x_02_T1"}})
    with pytest.raises(ValueError, match="unrecognised Landsat sensor"):
        plan_products(ls, ls.resolve_variables(["red"]), (0, 0, 1, 1), [], None)


def test_naip_builds_key_from_a_tile(catalog):
    """NAIP resolves the quad object path supplied via tile=."""
    naip = catalog.resolve("naip-source")
    tile = "al/2021/100cm/rgbir_cog/30086/m_3008601_ne_16_060_20211004"
    naip = naip.model_copy(update={"params": {**naip.params, "tile": tile}})
    products = plan_products(naip, naip.resolve_variables(None), (0, 0, 1, 1), [], None)
    assert products[0].href == f"{tile}.tif"


def test_goes_skips_a_day_with_no_frames(catalog, fake_client_factory):
    """A day whose hour prefixes are empty contributes no products."""
    goes = catalog.resolve("goes")
    client = fake_client_factory(listing={"noaa-goes16": []})
    products = plan_products(
        goes, [goes.resolve_variable("C02")], (-100, 30, -99, 31),
        pd.to_datetime(["2024-06-28"]), client,
    )
    assert products == []
