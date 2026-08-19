"""Unit tests for the NEXRAD radar backend (no network)."""

from __future__ import annotations

import datetime as dt
import sys

import pytest

from earthlens.base import SpatialExtent
from earthlens.radar import Radar
from earthlens.radar.backend import _volume_start

pytestmark = [pytest.mark.radar, pytest.mark.unit]


def _make(tmp_path, **kwargs):
    """Build a Radar over KTLX with a wide same-day window."""
    params = dict(
        start="2024-06-01T00:00:00",
        end="2024-06-01T23:59:59",
        variables={"KTLX": ["reflectivity"]},
        lat_lim=[33, 37],
        lon_lim=[-100, -95],
        path=str(tmp_path),
    )
    params.update(kwargs)
    return Radar(**params)


class TestConstruction:
    """Tests for Radar.__init__ and the hooks."""

    def test_output_kind_vector(self, tmp_path):
        """Radar declares vector output (aggregate rejected by the facade)."""
        assert _make(tmp_path).OUTPUT_KIND == "vector"

    def test_empty_variables_raises(self, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            _make(tmp_path, variables={})

    def test_create_grid(self, tmp_path):
        """_create_grid wraps the bbox into a SpatialExtent."""
        assert isinstance(_make(tmp_path).space, SpatialExtent)

    def test_inverted_dates_raise(self, tmp_path):
        """A start later than end is rejected."""
        with pytest.raises(ValueError, match="inverted"):
            _make(tmp_path, start="2024-06-02T00:00:00", end="2024-06-01T00:00:00")

    def test_unknown_station_still_constructs(self, tmp_path):
        """A site absent from the catalog is still usable (no geometry)."""
        b = _make(tmp_path, variables={"KZZZ": []})
        assert b._stations == [("KZZZ", None)]


class TestVolumeStart:
    """Tests for the chunk-key timestamp parser."""

    def test_parses_scan_time(self):
        """_volume_start reads the scan start from a chunk key."""
        assert _volume_start("KTLX/871/20260524-005505-001-S") == dt.datetime(
            2026, 5, 24, 0, 55, 5
        )


class TestSearch:
    """Tests for the volume-listing search."""

    def test_lists_both_volumes_in_window(self, tmp_path, fake_s3):
        """A wide window finds both KTLX volumes with ordered chunk keys."""
        products = _make(tmp_path)._search()
        assert {p.metadata["volume"] for p in products} == {"100", "101"}
        v100 = next(p for p in products if p.metadata["volume"] == "100")
        assert v100.id == "KTLX.100"
        assert v100.metadata["chunk_keys"][0].endswith("-001-S")
        assert v100.metadata["chunk_keys"][-1].endswith("-003-E")
        assert v100.metadata["start_time"] == dt.datetime(2024, 6, 1, 12, 0, 0)

    def test_window_filters_volumes(self, tmp_path, fake_s3):
        """A narrow window keeps only the volumes whose scan time falls in it."""
        b = _make(tmp_path, start="2024-06-01T12:30:00", end="2024-06-01T14:00:00")
        products = b._search()
        assert {p.metadata["volume"] for p in products} == {"101"}

    def test_list_keys_paginates(self, tmp_path, fake_s3, monkeypatch):
        """_list_keys follows NextContinuationToken across pages."""
        pages = [
            {
                "Contents": [{"Key": "KTLX/100/a"}],
                "IsTruncated": True,
                "NextContinuationToken": "t1",
            },
            {"Contents": [{"Key": "KTLX/100/b"}], "IsTruncated": False},
        ]
        calls = {"n": 0}

        def paginated(**kwargs):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        fake_s3.list_objects_v2 = paginated
        keys = Radar._list_keys(fake_s3, "KTLX/100/")
        assert keys == ["KTLX/100/a", "KTLX/100/b"] and calls["n"] == 2

    def test_list_prefixes_paginates(self, fake_s3):
        """_list_prefixes follows NextContinuationToken across pages."""
        pages = [
            {
                "CommonPrefixes": [{"Prefix": "KTLX/100/"}],
                "IsTruncated": True,
                "NextContinuationToken": "t1",
            },
            {"CommonPrefixes": [{"Prefix": "KTLX/101/"}], "IsTruncated": False},
        ]
        calls = {"n": 0}

        def paginated(**kwargs):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        fake_s3.list_objects_v2 = paginated
        assert Radar._list_prefixes(fake_s3, "KTLX/") == ["KTLX/100/", "KTLX/101/"]

    def test_first_key_returns_earliest(self, fake_s3):
        """_first_key returns the lexicographically first chunk of a volume."""
        assert (
            Radar._first_key(fake_s3, "KTLX/100/") == "KTLX/100/20240601-120000-001-S"
        )

    def test_first_key_empty_prefix_is_none(self, fake_s3):
        """_first_key returns None when no objects live under the prefix."""
        assert Radar._first_key(fake_s3, "ZZZZ/") is None

    def test_search_skips_volume_without_first_key(
        self, tmp_path, fake_s3, monkeypatch
    ):
        """A listed volume prefix with no first chunk is skipped (no product)."""
        b = _make(tmp_path)
        monkeypatch.setattr(b, "_list_prefixes", lambda client, prefix: ["KTLX/999/"])
        monkeypatch.setattr(b, "_first_key", lambda client, prefix: None)
        assert b._search() == []

    def test_search_skips_volume_without_chunks(self, tmp_path, fake_s3, monkeypatch):
        """An in-window volume whose full chunk list is empty is skipped."""
        b = _make(tmp_path)
        monkeypatch.setattr(b, "_list_prefixes", lambda client, prefix: ["KTLX/999/"])
        monkeypatch.setattr(
            b, "_first_key", lambda client, prefix: "KTLX/999/20240601-120000-001-S"
        )
        monkeypatch.setattr(b, "_list_keys", lambda client, prefix: [])
        assert b._search() == []


class TestWindow:
    """Tests for the scan-time window helper."""

    def test_window_extends_a_date_only_end_to_day_end(self, tmp_path):
        """A date-only end is extended to 23:59:59 of that day."""
        b = _make(tmp_path, start="2024-06-01", end="2024-06-01")
        _, end = b._window()
        assert (end.hour, end.minute, end.second) == (23, 59, 59)

    def test_window_keeps_an_explicit_midnight_end(self, tmp_path):
        """An end typed as an explicit midnight instant means that instant."""
        b = _make(tmp_path, start="2024-06-01T00:00:00", end="2024-06-02T00:00:00")
        _, end = b._window()
        assert (end.hour, end.minute, end.second) == (0, 0, 0), (
            f"an explicit midnight must not be widened, got {end}"
        )

    def test_window_keeps_explicit_end_time(self, tmp_path):
        """A non-midnight end time is returned unchanged."""
        b = _make(tmp_path, start="2024-06-01T00:00:00", end="2024-06-01T18:30:00")
        _, end = b._window()
        assert (end.hour, end.minute, end.second) == (18, 30, 0)


def _boom_on_101(Bucket, Key):  # noqa: N803 - mirrors the boto3 keyword name
    """Fail the `/101/` chunk, serve one byte for every other key.

    Args:
        Bucket: The bucket name (ignored).
        Key: The object key being fetched.

    Returns:
        dict: A minimal `get_object` response.

    Raises:
        RuntimeError: For any key under the `101` volume.
    """
    if "/101/" in Key:
        raise RuntimeError("s3 down")
    return {"Body": type("B", (), {"read": lambda self: b"x"})()}


class TestApi:
    """Tests for the `_api` search/fetch composition."""

    def test_api_returns_assembled_paths(self, tmp_path, fake_s3):
        """_api composes search + fetch and returns the assembled .ar2v paths."""
        paths = _make(tmp_path)._api()
        assert len(paths) == 2
        assert all(p.suffix == ".ar2v" for p in paths)


class TestFetch:
    """Tests for the chunk assembly."""

    def test_assembles_volume(self, tmp_path, fake_s3):
        """_fetch concatenates a volume's chunks (S+I+E) into one .ar2v."""
        b = _make(tmp_path)
        paths = b._fetch(b._search())
        assert len(paths) == 2
        v100 = next(p for p in paths if "20240601_120000" in p.name)
        assert v100.name == "KTLX_20240601_120000.ar2v"
        assert v100.read_bytes() == b"AR2V0006.100<S><I2><E>"

    def test_fetch_skips_failed_volume(self, tmp_path, fake_s3):
        """A volume whose chunk download raises is skipped, not fatal."""
        b = _make(tmp_path)
        products = b._search()

        def boom(Bucket, Key):
            if "/101/" in Key:
                raise RuntimeError("s3 down")
            return {"Body": type("B", (), {"read": lambda self: b"x"})()}

        fake_s3.get_object = boom
        paths = b._fetch(products)
        assert len(paths) == 1  # the 100 volume survives, 101 skipped

    def test_errors_raise_propagates_the_failed_volume(self, tmp_path, fake_s3):
        """download(errors="raise") surfaces the assembly error instead of skipping."""
        b = _make(tmp_path)
        fake_s3.get_object = _boom_on_101
        with pytest.raises(RuntimeError, match="s3 down"):
            b.download(errors="raise")

    def test_errors_rejects_an_unknown_policy(self, tmp_path, fake_s3):
        """An unrecognised errors= value is refused before any request."""
        b = _make(tmp_path)
        with pytest.raises(ValueError, match="errors"):
            b.download(errors="explode")


class TestDownload:
    """Tests for the GeoDataFrame inventory."""

    def test_inventory_has_rows_and_geometry(self, tmp_path, fake_s3):
        """download() returns one GeoDataFrame row per volume with station geometry."""
        gdf = _make(tmp_path).download()
        assert len(gdf) == 2
        assert set(gdf["station_id"]) == {"KTLX"}
        assert list(gdf.columns) == [
            "station_id",
            "volume",
            "scan_time",
            "n_chunks",
            "path",
            "geometry",
        ]
        assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
        assert gdf.geometry.iloc[0] is not None  # KTLX is in the catalog

    def test_aggregate_rejected(self, tmp_path, fake_s3):
        """download(aggregate=...) is not supported for raw radar volumes."""
        with pytest.raises(NotImplementedError, match="not griddable"):
            _make(tmp_path).download(aggregate=object())


class TestS3Client:
    """Tests for the unsigned-S3 client construction."""

    def test_missing_boto3_raises_friendly(self, monkeypatch):
        """A missing boto3 surfaces an earthlens[radar] ImportError."""
        from earthlens.radar.backend import _s3_client

        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(ImportError, match=r"earthlens\[radar\]"):
            _s3_client("us-east-1")
