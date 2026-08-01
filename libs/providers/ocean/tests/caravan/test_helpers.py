"""Unit tests for the Caravan Zenodo and archive helpers (no network)."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest
import requests

from earthlens.base.http import HttpClient
from earthlens.caravan import _helpers
from earthlens.caravan.catalog import ArchiveFile

from .conftest import FakeRangeSession, build_tar, build_zip

pytestmark = pytest.mark.caravan


def _archive(blob: bytes, **kwargs: Any) -> _helpers.CaravanArchive:
    """Open a fixture ZIP through a fake ranged transport."""
    return _helpers.CaravanArchive.open_remote_zip(
        "https://example.org/a.zip",
        client=HttpClient(session=FakeRangeSession(blob)),
        size=len(blob),
        **kwargs,
    )


def _tar_file(path: Path, md5: str) -> ArchiveFile:
    """Build a catalog descriptor pointing at a local tarball."""
    return ArchiveFile(
        record=1,
        name=path.name,
        size=path.stat().st_size,
        md5=md5,
        archive_format="tar.gz",
    )


class TestCacheDir:
    """Where archives and indexes land."""

    def test_the_env_override_wins(self, monkeypatch, tmp_path):
        """`EARTHLENS_CACHE` is the override the other backends already honour."""
        monkeypatch.setenv("EARTHLENS_CACHE", str(tmp_path))

        assert _helpers.cache_dir() == tmp_path / "caravan"

    def test_it_falls_back_to_the_platform_cache(self, monkeypatch):
        """A hard-coded `~/.cache` would be wrong on Windows."""
        monkeypatch.delenv("EARTHLENS_CACHE", raising=False)

        assert _helpers.cache_dir().name == "caravan"
        assert ".cache/earthlens" not in _helpers.cache_dir().as_posix()


class TestResolveRecord:
    """Reading a Zenodo record's file listing."""

    def test_files_are_keyed_by_name_with_bare_checksums(self):
        """Zenodo reports `md5:<hex>`; callers want the digest alone."""
        payload = {
            "files": [
                {
                    "key": "a.zip",
                    "size": 10,
                    "checksum": "md5:abc123",
                    "links": {"self": "https://example.org/a.zip"},
                }
            ]
        }
        client = HttpClient(session=_JsonSession(payload))

        files = _helpers.resolve_record(1, client=client)

        assert files["a.zip"].md5 == "abc123"
        assert files["a.zip"].url == "https://example.org/a.zip"
        assert files["a.zip"].size == 10

    def test_a_record_without_files_is_empty_not_an_error(self):
        """An embargoed or metadata-only record is a legitimate answer."""
        client = HttpClient(session=_JsonSession({}))

        assert _helpers.resolve_record(1, client=client) == {}


class _JsonSession:
    """Returns one canned JSON body for any `GET`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = __import__("json").dumps(self.payload).encode()
        return response


class TestZipIndex:
    """Prefix-agnostic member resolution over a range-read ZIP."""

    @pytest.mark.parametrize(
        "prefix", ["", "Caravan_extension_DK/", "GRDC_Caravan_extension_csv/"]
    )
    def test_members_resolve_under_any_root(self, prefix):
        """Two shipped archives have no root directory and three do."""
        archive = _archive(build_zip(root_prefix=prefix))

        assert archive.timeseries_member("dk", "dk_1") == (
            f"{prefix}timeseries/csv/dk/dk_1.csv"
        )

    def test_sources_are_discovered_from_the_listing(self):
        """The source directories are read, never assumed."""
        assert _archive(build_zip()).sources == ["dk", "xx"]

    def test_gauge_ids_are_listed_per_source_and_format(self):
        """A source's catchments come from the member names."""
        archive = _archive(build_zip())

        assert archive.gauge_ids("dk") == ["dk_1", "dk_2"]
        assert archive.gauge_ids("dk", "netcdf") == ["dk_1"]

    def test_a_missing_catchment_is_none_not_an_error(self):
        """The caller decides whether an absent catchment is fatal."""
        assert _archive(build_zip()).timeseries_member("dk", "nope") is None

    def test_attribute_tables_are_found_by_kind(self):
        """`other` carries locations; `caravan` carries climate indices."""
        archive = _archive(build_zip())

        assert archive.attribute_member("dk", "other").endswith(
            "attributes_other_dk.csv"
        )
        assert archive.attribute_member("dk", "caravan").endswith(
            "attributes_caravan_dk.csv"
        )
        assert archive.attribute_member("dk", "hydroatlas") is None

    def test_every_shapefile_sidecar_is_returned(self):
        """GDAL cannot open a `.shp` without its `.shx` and `.dbf`."""
        parts = _archive(build_zip()).shapefile_members("dk")

        assert sorted(Path(p).suffix for p in parts) == [".dbf", ".shp", ".shx"]

    def test_directory_entries_are_excluded(self):
        """A directory member is not a file and must not look like one."""
        assert all(not name.endswith("/") for name in _archive(build_zip()).members)

    def test_the_index_costs_a_handful_of_requests(self):
        """Reading the directory must not walk the whole archive."""
        session = FakeRangeSession(build_zip())
        _helpers.CaravanArchive.open_remote_zip(
            "https://example.org/a.zip", client=HttpClient(session=session)
        )

        assert len(session.get_calls) <= 4

    def test_transfer_stats_are_reported(self):
        """The cost of a range-read session is observable."""
        archive = _archive(build_zip())
        archive.read(archive.timeseries_member("dk", "dk_1"))

        requests_made, megabytes = archive.transfer_stats

        assert requests_made > 0
        assert megabytes > 0


class TestAttributeIndex:
    """The table a bbox or country filter is resolved against."""

    def test_it_reads_the_other_table_not_the_caravan_one(self):
        """`attributes_caravan_*` holds climate indices, not locations."""
        index = _helpers.attribute_index(_archive(build_zip()), "dk")

        assert list(index.columns) == [
            "area",
            "country",
            "gauge_lat",
            "gauge_lon",
            "gauge_name",
        ]
        assert index.loc["dk_1", "country"] == "Denmark"

    def test_a_source_without_the_table_raises(self):
        """Without centroids, a spatial request cannot be honoured."""
        with pytest.raises(ValueError, match="cannot be resolved by bounding box"):
            _helpers.attribute_index(_archive(build_zip()), "nope")

    def test_merge_attributes_joins_the_tables(self):
        """`with_attributes` needs the locations and the indices together."""
        merged = _helpers.merge_attributes(_archive(build_zip()), "dk")

        assert "country" in merged.columns
        assert "p_mean" in merged.columns

    def test_merge_attributes_is_empty_for_an_unknown_source(self):
        """A source with no tables yields an empty frame, not a crash."""
        assert _helpers.merge_attributes(_archive(build_zip()), "nope").empty


class TestTarArchive:
    """The download-and-scan fallback for a non-seekable archive."""

    def test_members_resolve_the_same_way(self, tmp_path):
        """One index surface regardless of transport."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())

        archive = _helpers.CaravanArchive.open_local_tar(tarball)

        assert archive.sources == ["dk", "xx"]
        assert archive.timeseries_member("dk", "dk_1") == (
            "Caravan/timeseries/csv/dk/dk_1.csv"
        )

    def test_the_listing_is_cached_beside_the_archive(self, tmp_path):
        """Listing a gzip stream decompresses it, so it happens once."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())
        _helpers.CaravanArchive.open_local_tar(tarball)

        assert (tmp_path / "a.tar.gz.index.json").is_file()

    def test_reading_many_members_takes_one_pass(self, tmp_path):
        """Re-scanning a 29 GB stream per catchment would be pathological."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())
        archive = _helpers.CaravanArchive.open_local_tar(tarball)
        members = [
            archive.timeseries_member("dk", "dk_1"),
            archive.timeseries_member("dk", "dk_2"),
        ]

        blobs = archive.read_many(members)

        assert set(blobs) == set(members)

    def test_only_the_named_members_are_extracted(self, tmp_path):
        """`extractall` on a multi-gigabyte archive would explode the cache."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())
        dest = tmp_path / "out"

        _helpers.extract_tar_members(
            tarball, {"Caravan/timeseries/csv/dk/dk_1.csv"}, dest
        )

        assert [p.name for p in dest.rglob("*.csv")] == ["dk_1.csv"]

    def test_an_absent_member_is_simply_missing(self, tmp_path):
        """The caller decides whether a missing catchment is an error."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())

        assert _helpers.extract_tar_members(tarball, {"nope"}, tmp_path / "o") == {}

    def test_no_members_wanted_does_no_work(self, tmp_path):
        """An empty selection must not open the archive at all."""
        assert (
            _helpers.extract_tar_members(tmp_path / "missing.tar.gz", set(), tmp_path)
            == {}
        )

    def test_an_escaping_member_is_refused(self, tmp_path):
        """A malicious member name must never be written outside the target."""
        tarball = tmp_path / "evil.tar.gz"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = b"pwned"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        tarball.write_bytes(buffer.getvalue())

        with pytest.raises(ValueError, match="unsafe path"):
            _helpers.extract_tar_members(tarball, {"../escape.txt"}, tmp_path / "out")

    def test_a_tar_archive_reports_no_transfer(self, tmp_path):
        """A local archive transfers nothing at read time."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())

        assert _helpers.CaravanArchive.open_local_tar(tarball).transfer_stats == (
            0,
            0.0,
        )

    def test_an_archive_without_a_backing_store_raises(self):
        """A hand-built archive object cannot read anything."""
        archive = _helpers.CaravanArchive(members=("a",), label="x")

        with pytest.raises(RuntimeError, match="no backing store"):
            archive.read_many(["a"])


class TestEnsureArchive:
    """Fetching and verifying the non-seekable archive."""

    def test_a_matching_cached_copy_is_reused(self, tmp_path):
        """The 29 GB base download must happen at most once."""
        blob = build_tar()
        cached = tmp_path / "1" / "a.tar.gz"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(blob)
        descriptor = _tar_file(cached, hashlib.md5(blob).hexdigest())
        client = HttpClient(session=_ExplodingSession())

        assert (
            _helpers.ensure_archive(descriptor, cache_root=tmp_path, client=client)
            == cached
        )

    def test_a_stale_cached_copy_is_refetched(self, tmp_path):
        """A truncated or superseded file must not be trusted."""
        blob = build_tar()
        cached = tmp_path / "1" / "a.tar.gz"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"truncated")
        descriptor = _tar_file(cached, hashlib.md5(blob).hexdigest())

        result = _helpers.ensure_archive(
            descriptor,
            cache_root=tmp_path,
            client=HttpClient(session=_BlobSession(blob)),
            progress=False,
        )

        assert result.read_bytes() == blob

    def test_a_checksum_mismatch_raises(self, tmp_path):
        """A corrupt download is not silently handed to the parser."""
        descriptor = ArchiveFile(
            record=1, name="a.tar.gz", size=1, md5="deadbeef", archive_format="tar.gz"
        )

        with pytest.raises(ValueError, match="failed its checksum"):
            _helpers.ensure_archive(
                descriptor,
                cache_root=tmp_path,
                client=HttpClient(session=_BlobSession(b"wrong")),
                progress=False,
            )


class _ExplodingSession:
    """Fails any request — proves the cached path issues none."""

    def get(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("a cached archive must not be re-downloaded")


class _BlobSession:
    """Streams a fixed body, mimicking a file download."""

    def __init__(self, blob: bytes) -> None:
        self.blob = blob

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = self.blob
        response.headers["Content-Length"] = str(len(self.blob))
        response.raw = io.BytesIO(self.blob)
        return response


class TestTarScanCost:
    """The tar fallback must decompress the archive once, not once per read."""

    def _counting_tarball(self, tmp_path, monkeypatch):
        """Write a fixture tarball and count how often the stream is opened."""
        tarball = tmp_path / "a.tar.gz"
        tarball.write_bytes(build_tar())
        opens: list[str] = []
        original = tarfile.open

        def _counted(*args: Any, **kwargs: Any) -> Any:
            opens.append(str(args[0] if args else kwargs.get("name")))
            return original(*args, **kwargs)

        monkeypatch.setattr(tarfile, "open", _counted)
        return tarball, opens

    def test_indexing_extracts_the_metadata_in_the_same_pass(
        self, tmp_path, monkeypatch
    ):
        """Attribute reads must not each trigger another full decompression."""
        tarball, opens = self._counting_tarball(tmp_path, monkeypatch)

        archive = _helpers.CaravanArchive.open_local_tar(
            tarball, extract_dir=tmp_path / "members"
        )
        during_index = len(opens)
        _helpers.attribute_index(archive, "dk")
        _helpers.merge_attributes(archive, "dk")

        assert during_index == 1, "indexing should scan once"
        assert len(opens) == during_index, (
            "attribute reads re-scanned the archive; they must come from the "
            "members extracted during indexing"
        )

    def test_a_repeat_member_read_does_not_rescan(self, tmp_path, monkeypatch):
        """An already-extracted member is read from disk."""
        tarball, opens = self._counting_tarball(tmp_path, monkeypatch)
        archive = _helpers.CaravanArchive.open_local_tar(
            tarball, extract_dir=tmp_path / "members"
        )
        member = archive.timeseries_member("dk", "dk_1")

        archive.read(member)
        after_first = len(opens)
        archive.read(member)

        assert len(opens) == after_first

    def test_several_missing_members_share_one_scan(self, tmp_path, monkeypatch):
        """Two uncached catchments cost one pass between them, not two."""
        tarball, opens = self._counting_tarball(tmp_path, monkeypatch)
        archive = _helpers.CaravanArchive.open_local_tar(
            tarball, extract_dir=tmp_path / "members"
        )
        before = len(opens)

        archive.read_many(
            [
                archive.timeseries_member("dk", "dk_1"),
                archive.timeseries_member("dk", "dk_2"),
            ]
        )

        assert len(opens) - before == 1

    def test_a_partial_extraction_is_not_mistaken_for_a_member(
        self, tmp_path, monkeypatch
    ):
        """Members are staged as `.part` and renamed, so a crash leaves no stub."""
        tarball, _ = self._counting_tarball(tmp_path, monkeypatch)
        dest = tmp_path / "members"
        _helpers.CaravanArchive.open_local_tar(tarball, extract_dir=dest)

        assert not list(dest.rglob("*.part"))

    def test_the_cached_index_is_written_atomically(self, tmp_path, monkeypatch):
        """A half-written index would silently hide members from every lookup."""
        tarball, _ = self._counting_tarball(tmp_path, monkeypatch)
        _helpers.CaravanArchive.open_local_tar(tarball, extract_dir=tmp_path / "m")

        assert (tmp_path / "a.tar.gz.index.json").is_file()
        assert not list(tmp_path.glob("*.part"))
