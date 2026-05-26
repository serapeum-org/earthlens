"""Unit + integration tests for `earthlens.openeo.backend` (graph + execution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.openeo.backend import OpenEO, _apply_step, _safe_name
from tests.openeo.conftest import FakeAuth, FakeConnection, FakeCube


def _make_backend(variables, output_dir, **kwargs) -> OpenEO:
    """Build an OpenEO backend over a small bbox + window (no network)."""
    return OpenEO(
        start="2023-01-01",
        end="2023-03-31",
        variables=variables,
        lat_lim=[40.0, 41.0],
        lon_lim=[3.0, 4.0],
        path=output_dir,
        **kwargs,
    )


def _bind_fake(backend: OpenEO) -> FakeConnection:
    """Replace the backend's auth with a recording fake connection."""
    conn = FakeConnection()
    backend._auth = FakeAuth(conn)
    return conn


@pytest.mark.openeo
class TestConstruction:
    """Constructor validation and request resolution."""

    def test_output_kind_is_raster(self, output_dir: Path):
        """The backend declares raster output."""
        backend = _make_backend({"sentinel-2-l2a": ["B04"]}, output_dir)
        assert backend.OUTPUT_KIND == "raster"

    def test_empty_variables_rejected(self, output_dir: Path):
        """An empty request is rejected at construction."""
        with pytest.raises(ValueError, match="at least one collection or recipe"):
            _make_backend({}, output_dir)

    def test_list_variables_rejected(self, output_dir: Path):
        """A list-shaped request is rejected with a clear TypeError, not a late crash."""
        with pytest.raises(TypeError, match="mapping"):
            _make_backend(["sentinel-2-l2a"], output_dir)

    def test_missing_dates_rejected(self, output_dir: Path):
        """A request with no start/end dates is rejected with a clear message."""
        with pytest.raises(ValueError, match="requires both start and end"):
            OpenEO(
                start=None,
                end=None,
                variables={"sentinel-2-l2a": ["B04"]},
                lat_lim=[40.0, 41.0],
                lon_lim=[3.0, 4.0],
                path=output_dir,
            )

    def test_bad_execute_rejected(self, output_dir: Path):
        """`execute` must be sync or batch."""
        with pytest.raises(ValueError, match="execute must be"):
            _make_backend({"sentinel-2-l2a": []}, output_dir, execute="turbo")

    def test_bad_output_format_rejected(self, output_dir: Path):
        """`output_format` must be a known openEO format."""
        with pytest.raises(ValueError, match="output_format must be"):
            _make_backend({"sentinel-2-l2a": []}, output_dir, output_format="jpeg")

    def test_unknown_key_rejected(self, output_dir: Path):
        """A request key absent from the catalog raises with a hint."""
        with pytest.raises(ValueError, match="not a known openEO"):
            _make_backend({"sentinel-99": []}, output_dir)

    def test_resolved_keyed_by_request(self, output_dir: Path):
        """`_initialize` resolves every request key into `_resolved`."""
        backend = _make_backend({"sentinel-2-l2a-ndvi-monthly": []}, output_dir)
        assert "sentinel-2-l2a-ndvi-monthly" in backend._resolved

    def test_process_override(self, output_dir: Path):
        """`process=` overrides the recipe inferred from the request key."""
        backend = _make_backend(
            {"sentinel-2-l2a": []}, output_dir, process="sentinel-2-l2a-ndvi-monthly"
        )
        resolved = backend._resolved["sentinel-2-l2a"]
        assert resolved.is_recipe and resolved.collection_id == "SENTINEL2_L2A"


@pytest.mark.openeo
class TestSearch:
    """`_search` is a cheap dry-run over the resolved request."""

    def test_one_product_per_key(self, output_dir: Path):
        """`_search` returns one product per requested key, no network."""
        backend = _make_backend(
            {"sentinel-2-l2a": ["B04"], "sentinel-1-grd": ["VV"]}, output_dir
        )
        products = backend._search()
        assert {p.id for p in products} == {"sentinel-2-l2a", "sentinel-1-grd"}
        assert all("resolved" in p.metadata for p in products)

    def test_api_composes_search_fetch(self, output_dir: Path):
        """`_api` composes `_search` + `_fetch` into the written paths."""
        backend = _make_backend({"sentinel-2-l2a": ["B04"]}, output_dir)
        _bind_fake(backend)
        paths = backend._api()
        assert len(paths) == 1 and paths[0].exists()


@pytest.mark.openeo
class TestBuildCube:
    """`_build_cube` constructs the right load + steps + aggregate node."""

    def test_collection_load_args(self, output_dir: Path):
        """A plain collection loads with bbox, window, and requested bands."""
        backend = _make_backend({"sentinel-2-l2a": ["B04", "B08"]}, output_dir)
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        _, cid, bands, kwargs, spatial, temporal = load
        assert cid == "SENTINEL2_L2A" and bands == ["B04", "B08"]
        assert spatial == {"west": 3.0, "south": 40.0, "east": 4.0, "north": 41.0}
        # openEO temporal_extent is right-open: the inclusive end (03-31) is
        # forwarded as the exclusive 04-01 so the last day is included.
        assert temporal == ["2023-01-01", "2023-04-01"]

    def test_inclusive_end_forwarded_as_exclusive(self, output_dir: Path):
        """The request's inclusive end is advanced one day for openEO's open bound."""
        backend = OpenEO(
            start="2023-06-01",
            end="2023-06-30",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[3.0, 4.0],
            path=output_dir,
        )
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        assert load[5] == ["2023-06-01", "2023-07-01"]

    def test_single_day_window_is_non_empty(self, output_dir: Path):
        """A single-day request maps to a non-empty `[d, d+1)` openEO interval."""
        backend = OpenEO(
            start="2023-06-15",
            end="2023-06-15",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[3.0, 4.0],
            path=output_dir,
        )
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        assert load[5] == ["2023-06-15", "2023-06-16"]
        assert load[5][0] != load[5][1]

    def test_default_bands_used_when_none_requested(self, output_dir: Path):
        """An empty band list falls back to the collection defaults."""
        backend = _make_backend({"sentinel-1-grd": []}, output_dir)
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        assert load[2] == ["VV", "VH"]

    def test_recipe_steps_applied_in_order(self, output_dir: Path):
        """Recipe graph steps are applied after load, in order."""
        backend = _make_backend({"sentinel-2-l2a-ndvi-monthly": []}, output_dir)
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        step_names = [e for e in conn.log if e[0] != "load_collection"]
        assert step_names[0] == ("process", "mask_scl_dilation", ["data"])
        assert ("ndvi", "B08", "B04") in conn.log
        assert ("aggregate_temporal_period", "month", "mean") in conn.log

    def test_max_cloud_cover_forwarded(self, output_dir: Path):
        """`max_cloud_cover` is forwarded for an optical collection."""
        backend = _make_backend(
            {"sentinel-2-l2a": ["B04"]}, output_dir, max_cloud_cover=15
        )
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        assert load[3] == {"max_cloud_cover": 15}

    def test_max_cloud_cover_rejected_for_non_optical(self, output_dir: Path):
        """`max_cloud_cover` on a SAR collection raises a clear ValueError."""
        backend = _make_backend(
            {"sentinel-1-grd": ["VV"]}, output_dir, max_cloud_cover=15
        )
        _bind_fake(backend)
        with pytest.raises(ValueError, match="only supported for optical"):
            backend._fetch(backend._search())

    def test_max_cloud_cover_allowed_for_optical_recipe(self, output_dir: Path):
        """A Sentinel-2 recipe inherits cloud-cover support from its base."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi-monthly": []}, output_dir, max_cloud_cover=30
        )
        conn = _bind_fake(backend)
        backend._fetch(backend._search())
        load = next(e for e in conn.log if e[0] == "load_collection")
        assert load[3] == {"max_cloud_cover": 30}

    def test_aggregate_adds_temporal_period_node(self, output_dir: Path):
        """`aggregate=` appends a server-side aggregate_temporal_period node."""
        backend = _make_backend({"sentinel-2-l2a": ["B04"]}, output_dir)
        conn = _bind_fake(backend)
        backend.download(aggregate=AggregationConfig(freq="1MS", op="std"))
        assert ("aggregate_temporal_period", "month", "sd") in conn.log

    def test_aggregate_not_rejected_for_raster(self, output_dir: Path):
        """`aggregate=` is honoured (raster), not rejected."""
        backend = _make_backend({"sentinel-2-l2a": ["B04"]}, output_dir)
        _bind_fake(backend)
        paths = backend.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        assert len(paths) == 1


@pytest.mark.openeo
class TestFetchExecution:
    """`_fetch` writes one file per product, sync or batch."""

    def test_sync_download_writes_file(self, output_dir: Path):
        """Sync execution downloads one file per key with the right suffix."""
        backend = _make_backend({"sentinel-2-l2a": ["B04"]}, output_dir)
        conn = _bind_fake(backend)
        paths = backend.download()
        assert paths == [output_dir / "sentinel-2-l2a.tif"]
        assert paths[0].exists()
        assert any(e[0] == "download" for e in conn.log)

    def test_recipe_output_format_overrides_backend(self, output_dir: Path):
        """A recipe's output_format (netCDF) wins over the backend default."""
        backend = _make_backend({"sentinel-2-l2a-ndvi-monthly": []}, output_dir)
        _bind_fake(backend)
        paths = backend.download()
        assert paths[0].suffix == ".nc"

    def test_batch_execution_takes_job_path(self, output_dir: Path):
        """`execute='batch'` creates a job and downloads its result."""
        backend = _make_backend(
            {"sentinel-2-l2a": ["B04"]}, output_dir, execute="batch"
        )
        conn = _bind_fake(backend)
        paths = backend.download()
        assert any(e[0] == "create_job" for e in conn.log)
        assert any(e[0] == "job_download_file" for e in conn.log)
        assert paths[0].exists()

    def test_multiple_keys_one_file_each(self, output_dir: Path):
        """A multi-key request writes one file per key."""
        backend = _make_backend(
            {"sentinel-2-l2a": ["B04"], "sentinel-1-grd": ["VV"]}, output_dir
        )
        _bind_fake(backend)
        paths = backend.download()
        assert {p.stem for p in paths} == {"sentinel-2-l2a", "sentinel-1-grd"}


@pytest.mark.openeo
class TestModuleHelpers:
    """`_apply_step` dispatch and `_safe_name` flattening."""

    def test_apply_step_dispatches_to_method(self):
        """A DataCube method step is dispatched to the method."""
        log: list = []
        cube = FakeCube(log)
        _apply_step(cube, {"ndvi": {"nir": "B08", "red": "B04"}})
        assert ("ndvi", "B08", "B04") in log

    def test_apply_step_falls_back_to_process(self):
        """A backend-only step is applied via process with data bound."""
        log: list = []
        cube = FakeCube(log)
        _apply_step(cube, {"mask_scl_dilation": {}})
        assert ("process", "mask_scl_dilation", ["data"]) in log

    def test_safe_name_flattens_separators(self):
        """Path separators in a key are flattened for filenames."""
        assert _safe_name("a/b\\c") == "a_b_c"
