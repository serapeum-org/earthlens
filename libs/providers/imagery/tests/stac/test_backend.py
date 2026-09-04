"""Unit + integration tests for `earthlens.stac.backend`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.stac.backend import (
    STAC,
    _acq_date,
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


class _BuildSignerSpy:
    """Records the `(signer_type, creds)` of each `build_signer` call."""

    def __init__(self):
        self.calls = []

    def __call__(self, signer_type, **creds):
        self.calls.append((signer_type, creds))
        return object()


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
        """The client is opened against the endpoint's URL (lazily, on first use)."""
        stac = _build_stac(tmp_path, endpoint="earth-search")
        assert not fake_pyramids.open_client_calls, (
            "construction must not open a client"
        )
        _ = stac.client
        assert fake_pyramids.open_client_calls[0]["url"].startswith(
            "https://earth-search"
        )

    def test_signer_selected_per_endpoint(self, fake_pyramids, tmp_path):
        """Each endpoint binds the signer its catalog row names."""
        s_es = _build_stac(tmp_path, endpoint="earth-search")
        s_pc = _build_stac(tmp_path, endpoint="planetary-computer")
        s_cdse = _build_stac(
            tmp_path,
            endpoint="cdse",
            variables={"sentinel-1-grd": ["vv"]},
            access_key="ak",
            secret_key="sk",
        )
        assert s_es._signer.name == "anonymous"
        assert s_pc._signer.name == "planetary-computer"
        assert s_cdse._signer.name == "cdse-s3"

    def test_endpoint_inferred_from_first_collection(self, fake_pyramids, tmp_path):
        """With no endpoint kwarg the home endpoint of the first collection is used."""
        stac = STAC(
            start="2024-01-01",
            end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )
        assert stac._endpoint == "planetary-computer"

    def test_mixed_endpoint_collections_raise(self, fake_pyramids, tmp_path):
        """A collection not served by the chosen endpoint is rejected, not mis-queried."""
        with pytest.raises(ValueError, match="not served by endpoint"):
            STAC(
                start="2024-01-01",
                end="2024-01-02",
                variables={"landsat-c2-l2": ["red"], "sentinel-1-grd": ["vv"]},
                lat_lim=[0.0, 1.0],
                lon_lim=[0.0, 1.0],
                path=str(tmp_path),
            )

    def test_empty_variables_raises(self, fake_pyramids, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="at least one collection"):
            STAC(
                start="2024-01-01",
                end="2024-01-02",
                variables={},
                lat_lim=[40.0, 41.0],
                lon_lim=[-4.0, -3.0],
                path=str(tmp_path),
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
            start="2024-01-01",
            end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0],
            lon_lim=[170.0, -170.0],
            path=str(tmp_path),
            endpoint="earth-search",
        )
        assert stac._bboxes() == [
            (170.0, 0.0, 180.0, 10.0),
            (-180.0, 0.0, -170.0, 10.0),
        ]

    def test_crossing_aoi_envelope_is_full_longitude(self, fake_pyramids, tmp_path):
        """The gross self.space envelope spans -180..180 for a crossing AOI."""
        stac = STAC(
            start="2024-01-01",
            end="2024-01-02",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0],
            lon_lim=[170.0, -170.0],
            path=str(tmp_path),
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
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": []}
        )
        products = stac._search()
        assert products[0].metadata["assets"] == ["B02", "B03", "B04", "B08"]


@pytest.mark.stac
class TestFetch:
    """_fetch mosaics per (collection, date) and writes one COG per group."""

    def test_single_tile_merges_each_band_and_writes_one_cog(
        self, fake_pyramids, tmp_path
    ):
        """One tile per date mosaics each band, stacks, and writes one COG."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a",
                "2024-01-05",
                {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"},
            )
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        paths = stac._fetch(stac._search())
        assert len(fake_pyramids.merge_calls) == 2  # one mosaic per band
        assert len(fake_pyramids.stack_calls) == 1
        assert len(fake_pyramids.write_calls) == 1
        assert len(paths) == 1 and paths[0].name == "sentinel-2-l2a_2024-01-05.tif"

    def test_multi_tile_calls_merge_per_band(self, fake_pyramids, tmp_path):
        """Two tiles on one date are mosaicked per band before stacking."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a",
                "2024-01-05",
                {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"},
            ),
            make_item(
                "b",
                "2024-01-05",
                {"B04": "https://h/b_b04.tif", "B08": "https://h/b_b08.tif"},
            ),
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        stac._fetch(stac._search())
        assert len(fake_pyramids.merge_calls) == 2

    def test_mixed_resolution_bands_use_aligned_stack_bands(
        self, fake_pyramids, tmp_path
    ):
        """Mixed-resolution bands stack via stack_bands(align=True) with a dtype-safe nodata."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a",
                "2024-01-05",
                {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"},
            )
        ]
        # the B08 band mosaic comes back at a coarser grid than B04
        fake_pyramids.dataset_shapes = {"_B08_": (1, 1, 1)}
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"sentinel-2-l2a": ["B04", "B08"]},
        )
        paths = stac._fetch(stac._search())
        assert len(paths) == 1
        assert len(fake_pyramids.stack_calls) == 1
        assert fake_pyramids.stack_calls[-1]["align"] is True
        assert fake_pyramids.stack_calls[-1]["no_data_value"] == 0
        assert not fake_pyramids.create_calls

    def test_cdse_rename_reaches_only_the_item_lookup(
        self, fake_pyramids, tmp_path, monkeypatch
    ):
        """CDSE's `B04_10m` addresses the item; the catalog's `B04` names the band.

        Test scenario:
            The item publishes CDSE's resolution-suffixed keys, so resolving the
            href at all proves the endpoint key was used (the stand-in raises
            KeyError otherwise). The written band names must still be the
            catalog's own keys, and the nodata must still resolve through them.
        """
        monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "ak")
        monkeypatch.setenv("CDSE_S3_SECRET_KEY", "sk")
        fake_pyramids.items_by_collection["sentinel-2-l2a"] = [
            make_item(
                "a",
                "2024-01-05",
                {"B04_10m": "https://h/a_b04.tif", "B08_10m": "https://h/a_b08.tif"},
            )
        ]
        stac = _build_stac(
            tmp_path,
            endpoint="cdse",
            variables={"sentinel-2-l2a": ["B04", "B08"]},
        )

        paths = stac._fetch(stac._search())

        assert len(paths) == 1, f"expected one written COG, got {paths}"
        names = fake_pyramids.stack_calls[-1]["band_names"]
        assert names == ["B04", "B08"], (
            f"the rename must not leak into the written band names; got {names}"
        )
        assert fake_pyramids.stack_calls[-1]["no_data_value"] == 0, (
            "nodata must still resolve through the catalog's own asset keys"
        )

    def test_same_resolution_bands_use_stack_bands(self, fake_pyramids, tmp_path):
        """Same-grid bands also stack via stack_bands(align=True), preserving band names."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a",
                "2024-01-05",
                {"B04": "https://h/a_b04.tif", "B08": "https://h/a_b08.tif"},
            )
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        stac._fetch(stac._search())
        assert len(fake_pyramids.stack_calls) == 1
        assert fake_pyramids.stack_calls[-1]["align"] is True
        assert not fake_pyramids.create_calls

    def test_crop_uses_wgs84_bbox_and_epsg(self, fake_pyramids, tmp_path):
        """The mosaic is cropped with the WGS84 AOI bbox declared as EPSG:4326."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif"})
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        cropped = fake_pyramids.write_data[-1]
        assert cropped.cropped_epsg == 4326, (
            "crop bbox must be declared in WGS84 (EPSG:4326)"
        )
        assert cropped.cropped_bbox == [-4.0, 40.0, -3.0, 41.0]

    def test_fetch_wraps_in_cloudconfig_with_gdal_env(self, fake_pyramids, tmp_path):
        """The merge/stack reads run inside CloudConfig(extra=signer.gdal_env())."""
        fake_pyramids.items_by_collection["sentinel-1-grd"] = [
            make_item("a", "2024-01-05", {"vv": "https://h/a_vv.tif"})
        ]
        stac = _build_stac(
            tmp_path,
            endpoint="cdse",
            variables={"sentinel-1-grd": ["vv"]},
            access_key="ak",
            secret_key="sk",
        )
        stac._fetch(stac._search())
        assert _FakeCloudConfig.active_extras[-1]["AWS_S3_ENDPOINT"]

    def test_signed_hrefs_reach_merge(self, fake_pyramids, tmp_path, monkeypatch):
        """MPC signs each tile href before it is handed to the mosaic step."""
        # The mpc-sas endpoint builds earthlens' own PlanetaryComputerSigner; patch
        # its sign_href to a deterministic transform (no network, no real PC host).
        monkeypatch.setattr(
            "earthlens.stac.signers.PlanetaryComputerSigner.sign_href",
            lambda self, href: href + "?sas=token",
        )
        fake_pyramids.items_by_collection["sentinel-2-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a_b04.tif"}),
            make_item("b", "2024-01-05", {"B04": "https://h/b_b04.tif"}),
        ]
        stac = _build_stac(
            tmp_path,
            endpoint="planetary-computer",
            variables={"sentinel-2-l2a": ["B04"]},
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
            start="2024-01-01",
            end="2024-01-31",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[0.0, 10.0],
            lon_lim=[170.0, -170.0],
            path=str(tmp_path),
            endpoint="earth-search",
        )
        names = sorted(p.name for p in stac._fetch(stac._search()))
        assert names == [
            "sentinel-2-l2a_2024-01-05_part0.tif",
            "sentinel-2-l2a_2024-01-05_part1.tif",
        ]

    def test_cross_crs_tiles_set_merge_dst_crs(self, fake_pyramids, tmp_path):
        """Tiles with differing proj:epsg make merge_rasters reproject via dst_crs."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a", "2024-01-05", {"B04": "https://h/a_b04.tif"}, proj_epsg=32630
            ),
            make_item(
                "b", "2024-01-05", {"B04": "https://h/b_b04.tif"}, proj_epsg=32631
            ),
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        merge_kwargs = fake_pyramids.merge_calls[0][2]
        assert merge_kwargs["dst_crs"] == 32630  # lowest of the differing EPSGs

    def test_explicit_epsg_sets_merge_dst_crs(self, fake_pyramids, tmp_path):
        """An explicit epsg= overrides item metadata as the merge dst_crs."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif"}, proj_epsg=32630)
        ]
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"sentinel-2-l2a": ["B04"]},
            epsg=3857,
        )
        stac._fetch(stac._search())
        assert fake_pyramids.merge_calls[0][2]["dst_crs"] == 3857

    def test_stack_uses_catalog_nodata_not_default(self, fake_pyramids, tmp_path):
        """stack_bands/merge get the catalog nodata (0 for S2 uint16), not -9999."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif"})
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        assert fake_pyramids.stack_calls[0]["no_data_value"] == 0
        assert fake_pyramids.merge_calls[0][2]["no_data_value"] == 0

    def test_nodata_for_reads_catalog_else_zero(self, fake_pyramids, tmp_path):
        """_nodata_for returns the catalog asset nodata, else 0 (dtype-safe)."""
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        assert stac._nodata_for("sentinel-2-l2a", ["B04"]) == 0
        assert stac._nodata_for("no-such-collection", ["x"]) == 0

    def test_api_composes_search_and_fetch(self, fake_pyramids, tmp_path):
        """_api() runs the search/fetch pipeline and returns the COG paths."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif"})
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        assert len(stac._api()) == 1

    def test_same_crs_tiles_leave_dst_crs_none(self, fake_pyramids, tmp_path):
        """Tiles sharing proj:epsg keep their native CRS (dst_crs=None)."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a", "2024-01-05", {"B04": "https://h/a_b04.tif"}, proj_epsg=32630
            ),
            make_item(
                "b", "2024-01-05", {"B04": "https://h/b_b04.tif"}, proj_epsg=32630
            ),
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        stac._fetch(stac._search())
        assert fake_pyramids.merge_calls[0][2]["dst_crs"] is None


@pytest.mark.stac
class TestDownload:
    """download() runs the pipeline and forwards aggregate per the raster shape."""

    def test_download_returns_written_paths(self, fake_pyramids, tmp_path):
        """download() returns one COG path per (collection, date) group."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "a", "2024-01-05", {"B04": "https://h/a.tif", "B08": "https://h/b.tif"}
            )
        ]
        stac = _build_stac(tmp_path, endpoint="earth-search")
        assert len(stac.download()) == 1

    def test_download_aggregate_reduces_to_window_cogs(self, fake_pyramids, tmp_path):
        """aggregate= reduces the per-date COGs into one COG per time window."""
        from earthlens.aggregate import AggregationConfig

        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/a.tif"}),
            make_item("b", "2024-01-20", {"B04": "https://h/b.tif"}),
        ]
        stac = _build_stac(
            tmp_path, endpoint="earth-search", variables={"sentinel-2-l2a": ["B04"]}
        )
        out = stac.download(
            aggregate=AggregationConfig(freq="1MS", out_dir=tmp_path / "agg")
        )
        assert len(out) == 1, (
            f"two Jan dates should reduce to one monthly COG, got {out}"
        )
        assert out[0].name == "sentinel-2-l2a_mean_1MS_20240101.tif"
        # the intermediate per-date COGs are cleaned up (M3)
        assert not (tmp_path / "sentinel-2-l2a_2024-01-05.tif").exists()
        assert not (tmp_path / "sentinel-2-l2a_2024-01-20.tif").exists()


@pytest.mark.stac
class TestSignerOverride:
    """A collection can override its endpoint's signer (M3 — requester-pays)."""

    def test_override_resolves_to_requester_pays(self, fake_pyramids, tmp_path):
        """earth-search/landsat-c2-l2 reads with aws-requester-pays, not anonymous."""
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"earth-search/landsat-c2-l2": ["red"]},
        )
        assert (
            stac._signer_for("earth-search/landsat-c2-l2").name == "aws-requester-pays"
        )
        # a collection without an override keeps the endpoint (anonymous) signer
        assert stac._signer_for("sentinel-2-l2a").name == "anonymous"

    def test_override_signer_cached_by_type(self, fake_pyramids, tmp_path):
        """A repeated override lookup returns the same cached signer instance."""
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"earth-search/landsat-c2-l2": ["red"]},
        )
        first = stac._signer_for("earth-search/landsat-c2-l2")
        assert stac._signer_for("earth-search/landsat-c2-l2") is first

    def test_fetch_applies_requester_pays_env_and_vsis3(self, fake_pyramids, tmp_path):
        """The override's GDAL env is active and s3:// hrefs become /vsis3/ for the read."""
        fake_pyramids.items_by_collection["landsat-c2-l2"] = [
            make_item("a", "2024-01-05", {"red": "s3://usgs-landsat/x/red.tif"})
        ]
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"earth-search/landsat-c2-l2": ["red"]},
        )
        stac._fetch(stac._search())
        assert (
            _FakeCloudConfig.active_extras[-1].get("AWS_REQUEST_PAYER") == "requester"
        )
        assert fake_pyramids.merge_calls[0][0][0].startswith("/vsis3/usgs-landsat/")


@pytest.mark.stac
class TestSignerCredentials:
    """The backend forwards bearer + S3 credentials to build_signer (L1)."""

    def test_signer_credentials_drops_unset_kwargs(self, fake_pyramids, tmp_path):
        """_signer_credentials returns only the set creds; unset token/client_id are dropped."""
        stac = _build_stac(
            tmp_path, endpoint="earth-search", username="u", password="p"
        )
        creds = stac._signer_credentials()
        assert creds["username"] == "u"
        assert creds["password"] == "p"
        assert "token" not in creds
        assert "client_id" not in creds

    def test_initialize_forwards_credentials_to_build_signer(
        self, fake_pyramids, tmp_path, monkeypatch
    ):
        """_initialize forwards the set bearer creds to build_signer (client_id dropped)."""
        spy = _BuildSignerSpy()
        monkeypatch.setattr("earthlens.stac.signers.build_signer", spy)
        _build_stac(
            tmp_path, endpoint="earth-search", username="u", password="p", token="t"
        )
        creds = spy.calls[0][1]
        assert creds["username"] == "u"
        assert creds["password"] == "p"
        assert creds["token"] == "t"
        assert "client_id" not in creds

    def test_signer_for_forwards_credentials_to_build_signer(
        self, fake_pyramids, tmp_path, monkeypatch
    ):
        """_signer_for forwards the set bearer creds when building an override signer."""
        spy = _BuildSignerSpy()
        monkeypatch.setattr("earthlens.stac.signers.build_signer", spy)
        stac = _build_stac(
            tmp_path,
            endpoint="earth-search",
            variables={"earth-search/landsat-c2-l2": ["red"]},
            username="u",
            password="p",
        )
        stac._signer_for("earth-search/landsat-c2-l2")
        override_calls = [
            creds for kind, creds in spy.calls if kind == "aws-requester-pays"
        ]
        assert override_calls and override_calls[0]["username"] == "u"


@pytest.mark.stac
class TestModuleHelpers:
    """The module-level helpers behave on pystac-shaped and dict-shaped items."""

    def test_acq_date_from_datetime(self):
        """_acq_date reads the item datetime as YYYY-MM-DD."""
        assert _acq_date(make_item("a", "2024-03-09", {})) == "2024-03-09"

    def test_acq_date_unknown_when_absent(self):
        """A datetime-less item yields the 'unknown' bucket."""
        assert _acq_date(object()) == "unknown"

    def test_acq_date_from_dict_properties(self):
        """_acq_date reads properties['datetime'] on a raw STAC dict."""
        item = {"properties": {"datetime": "2024-02-03T10:00:00Z"}}
        assert _acq_date(item) == "2024-02-03"

    def test_window_labels_skips_empty_buckets(self):
        """A monthly grouping over dates with a gap skips the empty window."""
        from earthlens.base import window_labels

        # Jan + Mar dates: the February bucket is empty and is skipped.
        labels = window_labels(["2024-01-10", "2024-03-20"], "MS")
        assert labels == ["20240101", "20240301"]

    def test_cleanup_ignores_missing(self, tmp_path):
        """_cleanup tolerates absent paths."""
        from earthlens.stac.backend import _cleanup

        _cleanup([tmp_path / "absent.tif"])

    def test_cleanup_swallows_oserror(self, tmp_path, monkeypatch):
        """_cleanup swallows an OSError raised while unlinking."""
        from earthlens.stac.backend import _cleanup

        def _raise(self, missing_ok=False):
            raise OSError("locked")

        monkeypatch.setattr(Path, "unlink", _raise)
        _cleanup([tmp_path / "x.tif"])  # must not raise

    def test_to_vsi_rewrites_s3(self):
        """_to_vsi turns s3:// into the GDAL /vsis3/ path; leaves others alone."""
        from earthlens.stac.backend import _to_vsi

        assert _to_vsi("s3://usgs-landsat/x/B4.TIF") == "/vsis3/usgs-landsat/x/B4.TIF"
        assert _to_vsi("https://h/a.tif") == "https://h/a.tif"
        assert _to_vsi("/vsis3/eodata/a.tif") == "/vsis3/eodata/a.tif"

    def test_item_epsg_reads_proj_metadata(self, fake_pyramids):
        """_item_epsg reads proj:epsg via pyramids' extension reader (None when absent)."""
        from earthlens.stac.backend import _item_epsg

        assert _item_epsg(make_item("a", "2024-01-05", {}, proj_epsg=32631)) == 32631
        assert _item_epsg(make_item("a", "2024-01-05", {})) is None

    def test_item_epsg_reads_dict_item(self, fake_pyramids):
        """_item_epsg reads proj:epsg from a raw STAC item dict."""
        from earthlens.stac.backend import _item_epsg

        assert _item_epsg({"properties": {"proj:epsg": 32633}, "assets": {}}) == 32633
        assert _item_epsg({"properties": {}, "assets": {}}) is None

    def test_group_products_buckets_by_collection_date_bbox(self):
        """Products are grouped by (collection_key, date, source bbox)."""
        from earthlens.base import RemoteProduct

        bx = (0.0, 0.0, 1.0, 1.0)
        products = [
            RemoteProduct(
                id="a",
                metadata={"collection_key": "c", "date": "2024-01-05", "bbox": bx},
            ),
            RemoteProduct(
                id="b",
                metadata={"collection_key": "c", "date": "2024-01-05", "bbox": bx},
            ),
            RemoteProduct(
                id="d",
                metadata={"collection_key": "c", "date": "2024-01-06", "bbox": bx},
            ),
        ]
        groups = dict(_group_products(products))
        assert len(groups[("c", "2024-01-05", bx)]) == 2
        assert len(groups[("c", "2024-01-06", bx)]) == 1
