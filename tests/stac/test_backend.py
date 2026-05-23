"""Unit + integration tests for `earthlens.stac.backend`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.stac.backend import (
    STAC,
    _acq_date,
    _asset_href,
    _group_products,
)

from .conftest import _FakeCloudConfig, make_item


def _build_stac(tmp_path, endpoint="earth-search", variables=None, **kwargs):
    """Construct a STAC backend bound to `endpoint` over a small AOI."""
    return STAC(
        start="2024-01-01",
        end="2024-01-31",
        variables=variables or {"sentinel-2-l2a": ["B04", "B08"]},
        lat_lim=[40.0, 41.0],
        lon_lim=[-4.0, -3.0],
        path=str(tmp_path),
        endpoint=endpoint,
        **kwargs,
    )


@pytest.mark.stac
class TestOutputKind:
    """The backend declares the raster output shape."""

    def test_output_kind_is_raster(self):
        """OUTPUT_KIND is 'raster' so the facade forwards aggregate=."""
        assert STAC.OUTPUT_KIND == "raster"


@pytest.mark.stac
class TestInitialize:
    """_initialize resolves the endpoint, builds the signer, opens the client."""

    def test_opens_client_with_endpoint_url(self, fake_pyramids, tmp_path):
        """The client is opened against the endpoint's URL."""
        _build_stac(tmp_path, endpoint="earth-search")
        assert fake_pyramids.open_client_calls[0]["url"].startswith("https://earth-search")

    def test_signer_selected_per_endpoint(self, fake_pyramids, fake_pc, tmp_path):
        """Each endpoint binds the signer its catalog row names."""
        s_es = _build_stac(tmp_path, endpoint="earth-search")
        s_pc = _build_stac(tmp_path, endpoint="planetary-computer")
        s_cdse = _build_stac(
            tmp_path, endpoint="cdse", variables={"sentinel-1-grd": ["vv"]},
            access_key="ak", secret_key="sk",
        )
        assert s_es._signer.name == "anonymous"
        assert s_pc._signer.name == "mpc-sas"
        assert s_cdse._signer.name == "cdse-s3"

    def test_endpoint_inferred_from_first_collection(self, fake_pyramids, fake_pc, tmp_path):
        """With no endpoint kwarg the home endpoint of the first collection is used."""
        stac = STAC(
            start="2024-01-01", end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0], path=str(tmp_path),
        )
        assert stac._endpoint == "planetary-computer"

    def test_empty_variables_raises(self, fake_pyramids, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="at least one collection"):
            STAC(
                start="2024-01-01", end="2024-01-02", variables={},
                lat_lim=[40.0, 41.0], lon_lim=[-4.0, -3.0], path=str(tmp_path),
                endpoint="earth-search",
            )


@pytest.mark.stac
class TestGridAndDates:
    """_create_grid and _check_input_dates capture the request envelope."""

    def test_create_grid_wraps_bbox(self, fake_pyramids, tmp_path):
        """The lat/lon limits land on self.space."""
        stac = _build_stac(tmp_path)
        assert (stac.space.west, stac.space.east) == (-4.0, -3.0)

    def test_check_input_dates_builds_window(self, fake_pyramids, tmp_path):
        """The start/end window is parsed onto self.time."""
        stac = _build_stac(tmp_path)
        assert stac.time.start_date.strftime("%Y-%m-%d") == "2024-01-01"
        assert stac.time.end_date.strftime("%Y-%m-%d") == "2024-01-31"

    def test_bboxes_single_for_normal_extent(self, fake_pyramids, tmp_path):
        """A non-antimeridian extent yields exactly one search bbox."""
        stac = _build_stac(tmp_path)
        assert stac._bboxes() == [(-4.0, 40.0, -3.0, 41.0)]

    def test_crossing_aoi_splits_into_two_bboxes(self, fake_pyramids, tmp_path):
        """A west>east lon_lim is split into an eastern + western half."""
        stac = STAC(
            start="2024-01-01", end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0], lon_lim=[170.0, -170.0], path=str(tmp_path),
            endpoint="earth-search",
        )
        assert stac._bboxes() == [
            (170.0, 0.0, 180.0, 10.0),
            (-180.0, 0.0, -170.0, 10.0),
        ]

    def test_crossing_aoi_envelope_is_full_longitude(self, fake_pyramids, tmp_path):
        """The gross self.space envelope spans -180..180 for a crossing AOI."""
        stac = STAC(
            start="2024-01-01", end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0], lon_lim=[170.0, -170.0], path=str(tmp_path),
            endpoint="earth-search",
        )
        assert (stac.space.west, stac.space.east) == (-180.0, 180.0)


@pytest.mark.stac
class TestSearch:
    """_search builds the right query and one product per item."""

    def test_search_uses_resolved_id_bbox_window(self, fake_pyramids, tmp_path):
        """The search runs against the alias-resolved collection id + bbox + window."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif"})
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        products = stac._search()
        call = fake_pyramids.client.search_calls[0]
        assert call["collections"] == ["sentinel-2-c1-l2a"]
        assert call["bbox"] == [-4.0, 40.0, -3.0, 41.0]
        assert call["datetime"] == "2024-01-01/2024-01-31"
        assert len(products) == 1 and products[0].metadata["date"] == "2024-01-05"

    def test_search_falls_back_to_default_assets(self, fake_pyramids, tmp_path):
        """An empty asset list falls back to the collection default_assets."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {})
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": []})
        products = stac._search()
        assert products[0].metadata["assets"] == ["B02", "B03", "B04", "B08"]


@pytest.mark.stac
class TestFetch:
    """_fetch mosaics per (collection, date) and writes one COG per group."""

    def test_single_tile_skips_merge_and_writes_one_cog(self, fake_pyramids, tmp_path):
        """One tile per date stacks + writes a COG without calling merge_rasters."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"})
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        paths = stac._fetch(stac._search())
        assert fake_pyramids.merge_calls == []
        assert len(fake_pyramids.stack_calls) == 1
        assert len(fake_pyramids.write_calls) == 1
        assert len(paths) == 1 and paths[0].name == "sentinel-2-l2a_2024-01-05.tif"

    def test_multi_tile_calls_merge_per_band(self, fake_pyramids, tmp_path):
        """Two tiles on one date are mosaicked per band before stacking."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"}),
            make_item("b", "2024-01-05", {"B04": "https://h/b_b04.tif", "B08": "https://h/b_b08.tif"}),
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        stac._fetch(stac._search())
        assert len(fake_pyramids.merge_calls) == 2

    def test_fetch_wraps_in_cloudconfig_with_gdal_env(self, fake_pyramids, tmp_path):
        """The merge/stack reads run inside CloudConfig(extra=signer.gdal_env())."""
        fake_pyramids.items_by_collection["sentinel-1-grd"] = [
            make_item("a", "2024-01-05", {"vv": "https://h/a_vv.tif"})
        ]
        stac = _build_stac(
            tmp_path, endpoint="cdse", variables={"sentinel-1-grd": ["vv"]},
            access_key="ak", secret_key="sk",
        )
        stac._fetch(stac._search())
        assert _FakeCloudConfig.active_extras[-1]["AWS_S3_ENDPOINT"]

    def test_signed_hrefs_reach_merge(self, fake_pyramids, fake_pc, tmp_path):
        """MPC signs each tile href before it is handed to the mosaic step."""
        fake_pyramids.items_by_collection["sentinel-2-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif"}),
            make_item("b", "2024-01-05", {"B04": "https://h/b_b04.tif"}),
        ]
        stac = _build_stac(
            tmp_path, endpoint="planetary-computer", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        merged_src = fake_pyramids.merge_calls[0][0]
        assert all(s.endswith("?sas=token") for s in merged_src)

    def test_crossing_aoi_writes_one_cog_per_half(self, fake_pyramids, tmp_path):
        """A crossing AOI emits one COG per half, suffixed _part0 / _part1."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("east", "2024-01-05", {"B04": "https://h/east.tif"}),
            make_item("west", "2024-01-05", {"B04": "https://h/west.tif"}),
        ]
        stac = STAC(
            start="2024-01-01", end="2024-01-31",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0], lon_lim=[170.0, -170.0], path=str(tmp_path),
            endpoint="earth-search",
        )
        names = sorted(p.name for p in stac._fetch(stac._search()))
        assert names == ["sentinel-2-l2a_2024-01-05_part0.tif", "sentinel-2-l2a_2024-01-05_part1.tif"]

    def test_cross_crs_tiles_reprojected_before_merge(self, fake_pyramids, tmp_path):
        """Tiles in different CRSs are reprojected to a common CRS before merging."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif"}),
            make_item("b", "2024-01-05", {"B04": "https://h/b_b04.tif"}),
        ]
        fake_pyramids.dataset_epsgs = {
            "https://h/a_b04.tif": 32630,
            "https://h/b_b04.tif": 32631,
        }
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        merged_src = fake_pyramids.merge_calls[0][0]
        assert any("reproj" in s for s in merged_src)


@pytest.mark.stac
class TestDownload:
    """download() runs the pipeline and forwards aggregate per the raster shape."""

    def test_download_returns_written_paths(self, fake_pyramids, tmp_path):
        """download() returns one COG path per (collection, date) group."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif", "B08": "https://h/b.tif"})
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        assert len(stac.download()) == 1

    def test_download_aggregate_raises_not_implemented(self, fake_pyramids, tmp_path):
        """aggregate= is accepted (raster) but COG reduction is not yet wired (D6)."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = []
        stac = _build_stac(tmp_path, endpoint="earth-search")
        with pytest.raises(NotImplementedError, match="COG"):
            stac.download(aggregate=object())


@pytest.mark.stac
class TestModuleHelpers:
    """The module-level helpers behave on pystac-shaped and dict-shaped items."""

    def test_acq_date_from_datetime(self):
        """_acq_date reads the item datetime as YYYY-MM-DD."""
        assert _acq_date(make_item("a", "2024-03-09", {})) == "2024-03-09"

    def test_acq_date_unknown_when_absent(self):
        """A datetime-less item yields the 'unknown' bucket."""
        assert _acq_date(object()) == "unknown"

    def test_asset_href_resolves_key(self):
        """_asset_href returns the href of the named asset."""
        item = make_item("a", "2024-01-01", {"B04": "https://h/a.tif"})
        assert _asset_href(item, "B04") == "https://h/a.tif"

    def test_asset_href_missing_raises(self):
        """A missing asset key raises KeyError."""
        item = make_item("a", "2024-01-01", {"B04": "https://h/a.tif"})
        with pytest.raises(KeyError):
            _asset_href(item, "B99")

    def test_acq_date_from_dict_properties(self):
        """_acq_date reads properties['datetime'] on a raw STAC dict."""
        item = {"properties": {"datetime": "2024-02-03T10:00:00Z"}}
        assert _acq_date(item) == "2024-02-03"

    def test_asset_href_from_dict_item(self):
        """_asset_href resolves a raw dict item + dict asset."""
        item = {"assets": {"B04": {"href": "https://h/a.tif"}}}
        assert _asset_href(item, "B04") == "https://h/a.tif"

    def test_asset_href_dict_asset_without_href_raises(self):
        """A dict asset lacking an href raises KeyError."""
        item = {"assets": {"B04": {}}}
        with pytest.raises(KeyError, match="no 'href'"):
            _asset_href(item, "B04")

    def test_copy_single_writes_file(self, fake_pyramids, tmp_path):
        """_copy_single materialises a single tile href to a local GeoTIFF."""
        from earthlens.stac.backend import _copy_single

        target = tmp_path / "one.tif"
        _copy_single("https://h/a.tif", target)
        assert target.is_file()

    def test_cleanup_ignores_missing(self, tmp_path):
        """_cleanup tolerates absent paths."""
        from earthlens.stac.backend import _cleanup

        _cleanup([tmp_path / "absent.tif"])

    def test_to_common_crs_single_href_passthrough(self, fake_pyramids, tmp_path):
        """A single tile needs no reprojection and is returned unchanged."""
        stac = _build_stac(tmp_path, endpoint="earth-search")
        assert stac._to_common_crs(["https://h/a.tif"]) == ["https://h/a.tif"]

    def test_to_common_crs_same_epsg_passthrough(self, fake_pyramids, tmp_path):
        """Tiles sharing a CRS are returned unchanged (no reprojection)."""
        fake_pyramids.dataset_epsgs = {"https://h/a.tif": 32630, "https://h/b.tif": 32630}
        stac = _build_stac(tmp_path, endpoint="earth-search")
        out = stac._to_common_crs(["https://h/a.tif", "https://h/b.tif"])
        assert out == ["https://h/a.tif", "https://h/b.tif"]

    def test_group_products_buckets_by_collection_date_bbox(self):
        """Products are grouped by (collection_key, date, source bbox)."""
        from earthlens.base import RemoteProduct

        bx = (0.0, 0.0, 1.0, 1.0)
        products = [
            RemoteProduct(id="a", metadata={"collection_key": "c", "date": "2024-01-05", "bbox": bx}),
            RemoteProduct(id="b", metadata={"collection_key": "c", "date": "2024-01-05", "bbox": bx}),
            RemoteProduct(id="d", metadata={"collection_key": "c", "date": "2024-01-06", "bbox": bx}),
        ]
        groups = dict(_group_products(products))
        assert len(groups[("c", "2024-01-05", bx)]) == 2
        assert len(groups[("c", "2024-01-06", bx)]) == 1
