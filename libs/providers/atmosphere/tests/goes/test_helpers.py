"""Unit tests for the GOES S3 helper functions."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.goes._helpers import (
    BUCKET_REGION,
    download_key,
    list_prefix_keys,
    parse_scan_start,
    unsigned_s3_client,
)

from .conftest import FakeS3

pytestmark = pytest.mark.goes


class TestListPrefixKeys:
    """Tests for list_prefix_keys."""

    def test_single_page(self):
        """A single-page listing returns every key under the prefix."""
        fake = FakeS3(pages={"p/": ["p/a.nc", "p/b.nc"]})
        assert list_prefix_keys(fake, "b", "p/") == ["p/a.nc", "p/b.nc"], "both keys"

    def test_paginated(self):
        """A truncated listing follows the continuation token across pages."""
        fake = FakeS3(pages={"p/": [["p/a.nc"], ["p/b.nc"], ["p/c.nc"]]})
        keys = list_prefix_keys(fake, "b", "p/")
        assert keys == ["p/a.nc", "p/b.nc", "p/c.nc"], "all three pages concatenated"
        assert fake.listed[1][2] == "1", "second call passes the continuation token"

    def test_empty_prefix(self):
        """A prefix with no objects returns an empty list."""
        assert list_prefix_keys(FakeS3(), "b", "missing/") == [], "no keys"

    def test_truncated_without_next_token_stops(self):
        """A truncated response missing NextContinuationToken ends the loop."""

        class Weird:
            def list_objects_v2(self, **kw):
                return {"Contents": [{"Key": "p/a.nc"}], "IsTruncated": True}

        assert list_prefix_keys(Weird(), "b", "p/") == ["p/a.nc"], "loop terminates"


class TestParseScanStart:
    """Tests for parse_scan_start."""

    def test_parses_scan_start_with_tenths(self):
        """The _s token parses to a naive-UTC datetime including tenths of a second."""
        key = "OR_ABI-L2-MCMIPC-M6_G19_s20261841201185_e1_c1.nc"  # gitleaks:allow - GOES product filename, not a secret
        assert parse_scan_start(key) == dt.datetime(2026, 7, 3, 12, 1, 18, 500000), (
            "day-of-year 184 = 2026-07-03, tenths digit 5 -> 500000 us"
        )

    def test_no_token_returns_none(self):
        """A key without a scan-start token returns None."""
        assert parse_scan_start("no-scan-start.nc") is None, "unparseable -> None"


class TestDownloadKey:
    """Tests for download_key."""

    def test_streams_to_dest_atomically(self, tmp_path):
        """A known key streams to dest and leaves no .part file behind."""
        fake = FakeS3(pages={})
        dest = tmp_path / "g.nc"
        result = download_key(fake, "noaa-goes19", "p/g.nc", dest)
        assert result == dest, "returns the destination path"
        assert dest.read_bytes() == b"netcdf:p/g.nc", "streamed the fake body"
        assert not dest.with_name("g.nc.part").exists(), "no leftover .part file"

    def test_error_removes_part_and_reraises(self, tmp_path):
        """A failing get_object removes the .part file and re-raises."""
        fake = FakeS3(pages={}, missing={"p/x.nc"})
        dest = tmp_path / "x.nc"
        with pytest.raises(Exception):
            download_key(fake, "noaa-goes19", "p/x.nc", dest)
        assert not dest.with_name("x.nc.part").exists(), "partial file cleaned up"
        assert not dest.exists(), "no final file written"


class TestUnsignedClient:
    """Tests for unsigned_s3_client."""

    def test_builds_unsigned_client(self):
        """The helper builds an anonymous boto3 S3 client (UNSIGNED signature)."""
        from botocore import UNSIGNED

        client = unsigned_s3_client()
        assert client.meta.config.signature_version is UNSIGNED, "unsigned signer"
        assert client.meta.region_name == BUCKET_REGION, "default region applied"
