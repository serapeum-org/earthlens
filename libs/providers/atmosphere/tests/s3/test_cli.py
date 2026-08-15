"""Tests for the S3 catalog-tooling handlers (`earthlens.s3.cli`).

Moved out of core's CLI test suite when the S3 handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import importlib
import shutil
from typing import Any

import pytest

import earthlens.base.s3 as s3_auth
import earthlens.s3.cli as s3_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the s3 backend."""
    return next(b for b in list_backends() if b.provider == "s3")


class _FakeClient:
    """A boto3-like client returning a fixed listing."""

    def __init__(self, contents: list[dict[str, str]]):
        self._contents = contents

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        """Return the canned Contents payload."""
        return {"Contents": self._contents}


class _FakeAuth:
    """A stand-in `S3Auth` yielding a `_FakeClient`."""

    contents: list[dict[str, str]] = []

    def __init__(self, creds: Any):
        pass

    def client(self) -> _FakeClient:
        """Return the fake client."""
        return _FakeClient(type(self).contents)


class TestProber:
    """Tests for the S3 bucket prober (unsigned boto3)."""

    def test_lists_sample_keys(self, monkeypatch):
        """s3 probe lists a few object keys under the dataset's bucket."""
        monkeypatch.setattr(
            s3_cli, "_s3_sample_keys", lambda b, p, region: ["a/2020.tif"]
        )
        key = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), key)
        assert result.status == "ok", "s3 probe ran"
        assert "a/2020.tif" in result.assets, "object key listed"

    def test_lists_multiple_keys(self, monkeypatch):
        """A registered dataset lists a few object keys under its bucket."""
        monkeypatch.setattr(s3_cli, "_s3_sample_keys", lambda b, p, r: ["a", "b"])
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert "a" in result.assets, "keys listed"
        assert "b" in result.assets, "keys listed"

    def test_unknown_dataset_is_error(self):
        """An unregistered S3 dataset reports 'error'."""
        result = probe_dataset(_info(), "not-a-bucket")
        assert result.status == "error", "unknown dataset -> error"

    def test_sample_keys_helper_uses_unsigned_client(self, monkeypatch):
        """_s3_sample_keys returns the Contents keys from an unsigned client."""
        monkeypatch.setattr(_FakeAuth, "contents", [{"Key": "k1"}, {"Key": "k2"}])
        monkeypatch.setattr(s3_auth, "S3Auth", _FakeAuth)
        assert s3_cli._s3_sample_keys("b", "p", None) == ["k1", "k2"]


class TestWriter:
    """Tests for s3 refresh --write (regenerate available_datasets from curated)."""

    def test_write_regenerates_index_from_curated(self, tmp_path, monkeypatch):
        """s3 --write rewrites available_datasets to the sorted curated names."""
        info = _info()
        module = importlib.import_module(f"{info.module}.catalog")
        dst = tmp_path / module.CATALOG_PATH.name
        shutil.copy(module.CATALOG_PATH, dst)
        monkeypatch.setattr(module, "CATALOG_PATH", dst)
        module.clear_catalog_cache()
        before = sorted(load_catalog(info).datasets)
        outcome = refresh_one(info, write=True)
        assert outcome.status == "ok", "s3 refresh ran"
        assert outcome.written.endswith("s3_data_catalog.yaml"), "in-file index written"
        module.clear_catalog_cache()
        assert sorted(load_catalog(info).available_datasets) == before, "index==curated"


class TestLiveValidator:
    """Tests for the s3 live reachability validator (network mocked)."""

    def test_flags_empty_bucket(self, monkeypatch):
        """An S3 dataset whose bucket serves no object is flagged live."""
        monkeypatch.setattr(s3_cli, "_s3_live_keys", lambda b, p, r: [])
        result = validate_one(_info(), live=True)
        assert result.status == "ok", "empty bucket -> issue"
        assert result.issues, "empty bucket -> issue"

    def test_clean_when_objects_present(self, monkeypatch):
        """A reachable object clears the s3 live check."""
        monkeypatch.setattr(s3_cli, "_s3_live_keys", lambda b, p, r: ["k"])
        result = validate_one(_info(), live=True)
        assert result.issues == [], "objects present -> clean"

    def test_reports_bucket_error(self, monkeypatch):
        """A bucket whose listing raises is reported as drift, not raised."""

        def boom(b, p, r):
            raise RuntimeError("403")

        monkeypatch.setattr(s3_cli, "_s3_live_keys", boom)
        result = validate_one(_info(), live=True)
        assert any("bucket error" in i for i in result.issues), "error captured"

    def test_live_keys_lists_one(self, monkeypatch):
        """_s3_live_keys returns the object keys from an unsigned client."""
        monkeypatch.setattr(_FakeAuth, "contents", [{"Key": "k"}])
        monkeypatch.setattr(s3_auth, "S3Auth", _FakeAuth)
        assert s3_cli._s3_live_keys("b", "p", None) == ["k"], "object key returned"
