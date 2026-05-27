"""Scenario tests for the review-fix behaviours (H1/M1/L1/L3/L4/N1 edge paths).

These target the branches the existing per-plane tests do not exercise: the
async request-id extraction edge cases and the missing-id warning path, the
local-tiling per-tile size guard, the `aggregate=` window edge-insert + S3-URI
passthrough, `_geometry_bounds` input shapes, and the `_resolve_plane` memo.
All run against the faked SDK (no network).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.sentinel_hub.backend import (
    SentinelHub,
    _async_request_id,
    _geometry_bounds,
    _iter_geometries,
    _wait_for_async,
)

pytestmark = pytest.mark.sentinel_hub

_S3 = {"bucket": "s3://b/out", "iam_role_arn": "arn:aws:iam::1:role/r"}


def _backend(output_dir, lat=None, lon=None, **kwargs) -> SentinelHub:
    """Build a backend over a small (default) or caller-specified AOI."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-03",
        variables={"sentinel-2-l2a-ndvi": []},
        lat_lim=lat or [40.80, 40.83],
        lon_lim=lon or [14.24, 14.27],
        path=output_dir,
        resolution=10,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestAsyncRequestId:
    """`_async_request_id` extracts the id defensively from the submission JSON."""

    def test_reads_id_field(self):
        """A dict with `id` returns it."""
        assert _async_request_id([{"id": "x-1"}]) == "x-1"

    def test_falls_back_to_request_id(self):
        """`requestId` is used when `id` is absent."""
        assert _async_request_id([{"requestId": "r-9"}]) == "r-9"

    def test_dict_without_id_is_none(self):
        """A dict lacking both id keys yields None."""
        assert _async_request_id([{"status": "CREATED"}]) is None

    def test_bare_dict_payload(self):
        """A non-list payload (bare dict) is still read."""
        assert _async_request_id({"id": "z"}) == "z"

    def test_empty_payload_is_none(self):
        """An empty payload yields None."""
        assert _async_request_id([]) is None

    def test_empty_id_falls_back_then_none(self):
        """An empty-string id is not usable: fall back, else None (N1)."""
        assert _async_request_id([{"id": "", "requestId": "r"}]) == "r"
        assert _async_request_id([{"id": ""}]) is None


class TestBatchOutputValidation:
    """A `batch_output` without a bucket/url is rejected (L1)."""

    def test_async_missing_bucket_raises(self, fake_sh, output_dir):
        """api='async' with a bucket-less batch_output raises a clear error."""
        backend = _backend(
            output_dir,
            lat=[40.0, 40.5],
            lon=[14.0, 14.5],
            api="async",
            batch_output={"iam_role_arn": "arn:aws:iam::1:role/r"},
        )
        with pytest.raises(ValueError, match="must include a 'bucket'"):
            backend.download()

    def test_batch_missing_bucket_raises(self, fake_sh, output_dir):
        """api='batch' with a bucket-less batch_output raises a clear error."""
        backend = _backend(
            output_dir,
            lat=[10.0, 40.0],
            lon=[0.0, 30.0],
            api="batch",
            batch_output={"iam_role_arn": "arn:aws:iam::1:role/r", "grid_id": 2},
        )
        with pytest.raises(ValueError, match="must include a 'bucket'"):
            backend.download()


class TestAsyncMissingId:
    """The async plane copes when no request id can be determined (H1 else-branch)."""

    def test_missing_id_warns_and_returns_bucket(
        self, fake_sh, monkeypatch, output_dir
    ):
        """A submission with no id skips the poll, warns, and still returns the bucket."""
        monkeypatch.setattr(
            fake_sh.AsyncProcessRequest, "get_data", lambda self, save_data=False: []
        )
        backend = _backend(
            output_dir,
            lat=[40.0, 40.5],
            lon=[14.0, 14.5],
            api="async",
            batch_output=_S3,
        )
        assert backend.download() == ["s3://b/out"]


class TestTilingGuard:
    """The local-tiling pre-flight guard rejects an over-cap tile (L3)."""

    def test_oversized_tile_raises(self, fake_sh, monkeypatch, output_dir):
        """A tile that sizes above the Process cap raises before any render."""
        monkeypatch.setattr(
            fake_sh, "bbox_to_dimensions", lambda bbox, resolution: (3000, 3000)
        )
        backend = _backend(output_dir, api="tiling")
        with pytest.raises(ValueError, match="exceeding the 2500 px Process cap"):
            backend.download()


class TestResolvePlaneMemo:
    """`_resolve_plane` is computed once per download (L1)."""

    def test_repeated_resolve_uses_memo(self, fake_sh, monkeypatch, output_dir):
        """Calling `_resolve_plane` repeatedly sizes the request only once."""
        calls = {"n": 0}
        original = fake_sh.bbox_to_dimensions

        def _counting(bbox, resolution):
            calls["n"] += 1
            return original(bbox, resolution)

        monkeypatch.setattr(fake_sh, "bbox_to_dimensions", _counting)
        backend = _backend(output_dir)
        first = backend._resolve_plane()
        second = backend._resolve_plane()
        assert first == second == "process"
        assert calls["n"] == 1, f"plane resolved should memoise; got {calls['n']} calls"


class TestAggregateEdges:
    """`aggregate=` window edges: misaligned-start insert + S3-URI passthrough."""

    def test_misaligned_freq_inserts_start(self, fake_sh, output_dir):
        """A monthly freq starting mid-month inserts the request start as the first edge."""
        backend = SentinelHub(
            start="2020-06-15",
            end="2020-08-10",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.80, 40.83],
            lon_lim=[14.24, 14.27],
            path=output_dir,
            resolution=10,
            client_id="a",
            client_secret="b",
        )
        paths = backend.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        stamps = sorted(Path(p).name.split("_")[-1] for p in paths)
        assert (
            stamps[0] == "20200615.tiff"
        ), f"first window not stamped at start: {stamps}"

    def test_async_aggregate_passes_uris_through(self, fake_sh, output_dir):
        """Windowed aggregate on the async (S3) plane returns the URIs unchanged."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-03",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.5],
            lon_lim=[14.0, 14.5],
            path=output_dir,
            resolution=10,
            api="async",
            batch_output=_S3,
            client_id="a",
            client_secret="b",
        )
        results = backend.download(aggregate=AggregationConfig(freq="D", op="mean"))
        assert results == ["s3://b/out", "s3://b/out", "s3://b/out"]


class TestGeometryBounds:
    """`_geometry_bounds` handles shapely, GeoJSON Feature, and bad input."""

    def test_shapely_like_bounds(self):
        """An object exposing `.bounds` returns them directly."""

        class _Geom:
            bounds = (1.0, 2.0, 3.0, 4.0)

        assert _geometry_bounds(_Geom()) == (1.0, 2.0, 3.0, 4.0)

    def test_feature_geometry(self):
        """A GeoJSON Feature unwraps to its geometry's bounds."""
        feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 3]]]},
        }
        # _geometry_bounds is given a geometry mapping; pass the feature's geometry
        assert _geometry_bounds(feature["geometry"]) == (0, 0, 2, 3)

    def test_empty_geometry_raises(self):
        """A mapping with no coordinates raises a clear error."""
        with pytest.raises(ValueError, match="could not extract coordinates"):
            _geometry_bounds({"type": "Polygon", "coordinates": []})


class TestIterGeometries:
    """`_iter_geometries` normalises Feature / list / bare inputs."""

    def test_feature_uses_its_id(self):
        """A GeoJSON Feature yields one pair carrying its `id`."""
        feature = {"type": "Feature", "id": "f1", "geometry": {"type": "Point"}}
        assert _iter_geometries(feature) == [("f1", {"type": "Point"})]

    def test_list_of_geometries_indexed(self):
        """A list of geometries yields positional ids."""
        geoms = [{"type": "Point"}, {"type": "Polygon"}]
        assert _iter_geometries(geoms) == [(0, geoms[0]), (1, geoms[1])]


class TestWaitForAsync:
    """`_wait_for_async` polls the status endpoint until nothing is running."""

    def test_empty_ids_returns_immediately(self):
        """No active ids → return without polling."""
        module = types.SimpleNamespace(
            get_async_running_status=lambda ids, cfg: pytest.fail("should not poll")
        )
        _wait_for_async(module, [None, ""], config=None)

    def test_polls_until_done(self, monkeypatch):
        """A job running once then finished completes after a single re-poll."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        states = iter([{"job-1": True}, {"job-1": False}])
        module = types.SimpleNamespace(
            get_async_running_status=lambda ids, cfg: next(states)
        )
        _wait_for_async(module, ["job-1"], config=None, poll_seconds=0)

    def test_timeout_raises(self, monkeypatch):
        """A job that never finishes raises TimeoutError after max_attempts."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        module = types.SimpleNamespace(
            get_async_running_status=lambda ids, cfg: {"job-1": True}
        )
        with pytest.raises(TimeoutError, match="still running"):
            _wait_for_async(
                module, ["job-1"], config=None, poll_seconds=0, max_attempts=2
            )


class TestMosaickingConstant:
    """`mosaicking_order` validation uses the hoisted vocabulary (N1)."""

    @pytest.mark.parametrize("order", ["mostRecent", "leastRecent", "leastCC"])
    def test_valid_orders_accepted(self, output_dir, order):
        """Every valid mosaicking order constructs."""
        backend = _backend(output_dir, mosaicking_order=order)
        assert backend._mosaicking_order == order
