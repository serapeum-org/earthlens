"""Unit + integration tests for `earthlens.sentinel_hub.backend` (construction + dispatch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub


def _make_backend(variables, output_dir, **kwargs) -> SentinelHub:
    """Build a SentinelHub backend over a small bbox + window (no network)."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables=variables,
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        **kwargs,
    )


class TestConstruction:
    """Constructor validation and request resolution."""

    def test_output_kind_is_mixed(self, output_dir: Path):
        """The backend declares mixed output (fixed class attribute)."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir)
        assert backend.OUTPUT_KIND == "mixed"
        assert SentinelHub.OUTPUT_KIND == "mixed"

    def test_resolves_keys(self, output_dir: Path):
        """Each requested key is resolved against the catalog."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir)
        assert list(backend._resolved) == ["sentinel-2-l2a-ndvi"]

    def test_unknown_api_rejected(self, output_dir: Path):
        """An unknown `api=` is rejected at construction."""
        with pytest.raises(ValueError, match="unknown api"):
            _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir, api="render")

    def test_bad_mosaicking_order_rejected(self, output_dir: Path):
        """An invalid `mosaicking_order` is rejected."""
        with pytest.raises(ValueError, match="mosaicking_order"):
            _make_backend(
                {"sentinel-2-l2a-ndvi": []}, output_dir, mosaicking_order="newest"
            )

    def test_empty_variables_rejected(self, output_dir: Path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="at least one"):
            _make_backend({}, output_dir)

    def test_non_dict_variables_rejected(self, output_dir: Path):
        """A non-mapping variables value is rejected."""
        with pytest.raises(TypeError, match="mapping"):
            _make_backend(["sentinel-2-l2a-ndvi"], output_dir)

    def test_unknown_key_did_you_mean(self, output_dir: Path):
        """An unknown collection/recipe key raises with a hint."""
        with pytest.raises(ValueError, match="not a known"):
            _make_backend({"sentinel-2-l2a-ndv": []}, output_dir)


class TestSearch:
    """`_search` is a cheap dry-run."""

    def test_one_product_per_key(self, output_dir: Path):
        """`_search` returns one product per requested key, no network."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": [], "sentinel-2-l2a-ndwi": []}, output_dir
        )
        products = backend._search()
        assert sorted(p.id for p in products) == [
            "sentinel-2-l2a-ndvi",
            "sentinel-2-l2a-ndwi",
        ]
        assert products[0].metadata["resolved"].sh_collection == "SENTINEL2_L2A"


class TestPlaneRouting:
    """`_resolve_plane` auto-routes by size + geometry."""

    def test_small_bbox_routes_to_process(self, fake_sh, output_dir: Path):
        """A tiny bbox auto-routes to the Process plane."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir, resolution=10)
        assert backend._resolve_plane() == "process"

    def test_geometry_routes_to_statistical(self, fake_sh, output_dir: Path):
        """A `geometry=` request auto-routes to the Statistical plane."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi-stats": []},
            output_dir,
            geometry={"type": "Polygon", "coordinates": []},
        )
        assert backend._resolve_plane() == "statistical"

    def test_oversized_bbox_without_s3_routes_to_tiling(self, fake_sh, output_dir: Path):
        """A wide bbox with no S3 bucket auto-routes to local tiling."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[10.0, 40.0],
            lon_lim=[0.0, 30.0],
            path=output_dir,
            resolution=10,
        )
        assert backend._resolve_plane() == "tiling"

    def test_explicit_api_honoured(self, fake_sh, output_dir: Path):
        """An explicit `api=` overrides size-based routing."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []}, output_dir, api="statistical"
        )
        assert backend._resolve_plane() == "statistical"


class TestProcessFetch:
    """Process-plane render via the faked SDK (C3)."""

    def test_download_writes_geotiff(self, fake_sh, output_dir: Path):
        """A Process render builds a request and writes a GeoTIFF."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []},
            output_dir,
            client_id="a",
            client_secret="b",
        )
        paths = backend.download()
        assert len(paths) == 1
        assert paths[0].exists()
        assert paths[0].name == "response.tiff"

    def test_request_shape(self, fake_sh, output_dir: Path):
        """The built request carries the recipe evalscript, window, and TIFF output."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []}, output_dir, client_id="a", client_secret="b"
        )
        backend.download()
        req = fake_sh.SentinelHubRequest.instances[-1]
        assert "//VERSION=3" in req.evalscript
        assert req.input_data[0]["time_interval"] == ("2020-06-01", "2020-06-02")
        assert req.input_data[0]["mosaicking_order"] == "mostRecent"
        assert req.responses[0]["format"] == "image/tiff"

    def test_cdse_binding_applied(self, fake_sh, output_dir: Path):
        """The collection is rebound to the CDSE service URL."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []}, output_dir, client_id="a", client_secret="b"
        )
        backend.download()
        req = fake_sh.SentinelHubRequest.instances[-1]
        collection = req.input_data[0]["data_collection"]
        assert collection.service_url == "https://sh.dataspace.copernicus.eu"

    def test_maxcc_forwarded(self, fake_sh, output_dir: Path):
        """`maxcc=` is forwarded to the input_data block when set."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []},
            output_dir,
            client_id="a",
            client_secret="b",
            maxcc=0.2,
        )
        backend.download()
        req = fake_sh.SentinelHubRequest.instances[-1]
        assert req.input_data[0]["maxcc"] == 0.2

    def test_custom_inline_evalscript(self, fake_sh, output_dir: Path):
        """An inline `evalscript=` over a plain collection bypasses the recipe."""
        inline = "//VERSION=3\nfunction setup(){return {};}"
        backend = _make_backend(
            {"sentinel-2-l2a": []},
            output_dir,
            client_id="a",
            client_secret="b",
            evalscript=inline,
            api="process",
        )
        backend.download()
        req = fake_sh.SentinelHubRequest.instances[-1]
        assert req.evalscript == inline

    def test_plain_collection_without_evalscript_errors(self, fake_sh, output_dir: Path):
        """A plain collection with no `evalscript=` raises a clear error."""
        backend = _make_backend(
            {"sentinel-2-l2a": []},
            output_dir,
            client_id="a",
            client_secret="b",
            api="process",
        )
        with pytest.raises(ValueError, match="plain collection"):
            backend.download()

    def test_forced_process_on_oversized_raises(self, fake_sh, output_dir: Path):
        """Forcing `api='process'` on an oversized render raises the size guard."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[10.0, 40.0],
            lon_lim=[0.0, 30.0],
            path=output_dir,
            resolution=10,
            api="process",
            client_id="a",
            client_secret="b",
        )
        with pytest.raises(ValueError, match="Process API caps"):
            backend.download()
