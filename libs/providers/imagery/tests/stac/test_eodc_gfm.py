"""Unit tests for the EODC endpoint + Copernicus GFM collection (Route C)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.base import safe_filename
from earthlens.earthlens import EarthLens
from earthlens.stac import STAC, Catalog

from .conftest import make_item

_GFM_ASSETS = {
    "ensemble_flood_extent",
    "ensemble_water_extent",
    "ensemble_likelihood",
    "reference_water_mask",
    "exclusion_mask",
    "advisory_flags",
    "dlr_flood_extent",
    "tuw_flood_extent",
    "list_flood_extent",
    "dlr_likelihood",
    "tuw_likelihood",
    "list_likelihood",
}


def _build_gfm(tmp_path, variables=None, **kwargs):
    """Construct a STAC backend bound to the EODC endpoint over a small AOI."""
    return STAC(
        start="2022-09-01",
        end="2022-09-30",
        variables=variables or {"eodc/gfm": ["ensemble_flood_extent"]},
        lat_lim=[26.0, 28.0],
        lon_lim=[67.0, 69.0],
        path=str(tmp_path),
        endpoint="eodc",
        **kwargs,
    )


@pytest.mark.stac
class TestEodcCatalog:
    """The bundled catalog exposes the EODC endpoint and the GFM collection."""

    def test_eodc_endpoint_is_anonymous(self):
        """The EODC endpoint is the public STAC root with an anonymous signer."""
        endpoint = Catalog().get_endpoint("eodc")
        assert endpoint.url == "https://stac.eodc.eu/api/v1"
        assert endpoint.signer == "anonymous"

    def test_gfm_collection_resolves_to_upstream_id(self):
        """The logical key eodc/gfm resolves to the upstream collection id GFM."""
        assert Catalog().resolve("eodc", "eodc/gfm") == "GFM"

    def test_gfm_has_all_twelve_layers(self):
        """GFM exposes the twelve GFM product layers."""
        assert set(Catalog().get_collection("eodc/gfm").assets) == _GFM_ASSETS

    def test_gfm_default_asset_is_ensemble_flood_extent(self):
        """The default asset is the final ensemble flood extent."""
        assert Catalog().get_collection("eodc/gfm").default_assets == [
            "ensemble_flood_extent"
        ]

    def test_gfm_layers_are_uint8_nodata_255(self):
        """Every GFM layer is a uint8 COG with nodata 255."""
        assets = Catalog().get_collection("eodc/gfm").assets
        assert all(a.dtype == "uint8" and a.nodata == 255 for a in assets.values())

    def test_gfm_advertised_in_available_collections(self):
        """GFM appears in the EODC available_collections index."""
        assert "GFM" in Catalog().available_collections["eodc"]


@pytest.mark.stac
@pytest.mark.integration
class TestEodcRouting:
    """The facade registers the eodc alias and binds the endpoint."""

    def test_eodc_key_resolves_to_stac(self):
        """The eodc data-source key resolves to the STAC backend class."""
        assert EarthLens.DataSources["eodc"] is STAC

    def test_eodc_alias_prebinds_endpoint(self, fake_pyramids, tmp_path):
        """data_source='eodc' binds the eodc endpoint on the STAC backend."""
        el = EarthLens(
            data_source="eodc",
            start="2022-09-01",
            end="2022-09-30",
            variables={"eodc/gfm": ["ensemble_flood_extent"]},
            lat_lim=[26.0, 28.0],
            lon_lim=[67.0, 69.0],
            path=str(tmp_path),
        )
        assert el.datasource._endpoint == "eodc"


@pytest.mark.stac
class TestEodcSearchFetch:
    """Searching and fetching GFM through the generic STAC machinery."""

    def test_search_uses_gfm_id_and_selected_layer(self, fake_pyramids, tmp_path):
        """The search runs against the resolved id GFM with the requested layer."""
        fake_pyramids.items_by_collection["GFM"] = [
            make_item("s", "2022-09-28", {"ensemble_flood_extent": "https://h/s.tif"})
        ]
        stac = _build_gfm(tmp_path)
        products = stac._search()
        call = fake_pyramids.client.search_calls[0]
        assert call["collections"] == ["GFM"]
        assert call["bbox"] == [67.0, 26.0, 69.0, 28.0]
        assert products[0].metadata["assets"] == ["ensemble_flood_extent"]

    def test_empty_asset_list_falls_back_to_default(self, fake_pyramids, tmp_path):
        """An empty asset list falls back to the GFM default asset."""
        fake_pyramids.items_by_collection["GFM"] = [make_item("s", "2022-09-28", {})]
        stac = _build_gfm(tmp_path, variables={"eodc/gfm": []})
        assert stac._search()[0].metadata["assets"] == ["ensemble_flood_extent"]

    def test_single_tile_writes_one_flattened_cog(self, fake_pyramids, tmp_path):
        """One GFM tile writes a single COG whose name flattens the slash key."""
        fake_pyramids.items_by_collection["GFM"] = [
            make_item("s", "2022-09-28", {"ensemble_flood_extent": "https://h/s.tif"})
        ]
        stac = _build_gfm(tmp_path)
        paths = stac._fetch(stac._search())
        assert len(paths) == 1
        assert paths[0].name == f"{safe_filename('eodc/gfm')}_2022-09-28.tif"

    def test_nodata_is_catalog_uint8_fill(self, fake_pyramids, tmp_path):
        """The mosaic no-data value comes from the catalog (255), not pyramids' -9999."""
        fake_pyramids.items_by_collection["GFM"] = [
            make_item("s", "2022-09-28", {"ensemble_flood_extent": "https://h/s.tif"})
        ]
        stac = _build_gfm(tmp_path)
        stac._fetch(stac._search())
        assert fake_pyramids.merge_calls[0][2]["no_data_value"] == 255


@pytest.mark.stac
def test_stac_backend_never_imports_xarray():
    """The GFM raster path stays on pyramids — the backend never imports xarray."""
    import earthlens.stac.backend as backend_mod

    source = Path(backend_mod.__file__).read_text(encoding="utf-8")
    assert "import xarray" not in source
