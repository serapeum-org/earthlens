"""Unit tests for the openEO catalog tooling (`tools/openeo/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "openeo"
sys.path.insert(0, str(_TOOLS_DIR))

import probe_openeo_collection as probe  # noqa: E402


class _FakeListing(list):
    """A list of `{'id': ...}` dicts standing in for an openEO listing."""


class _FakeConnection:
    """An openEO connection stand-in exposing the listing + describe calls."""

    def __init__(
        self,
        collections: list[str],
        processes: list[str],
        describe: dict | None = None,
    ) -> None:
        self._collections = collections
        self._processes = processes
        self._describe = describe or {}

    def list_collections(self) -> _FakeListing:
        """Return canned collection entries."""
        return _FakeListing({"id": c} for c in self._collections)

    def list_processes(self) -> _FakeListing:
        """Return canned process entries."""
        return _FakeListing({"id": p} for p in self._processes)

    def describe_collection(self, collection_id: str) -> dict:
        """Return canned collection metadata, or raise when unknown."""
        if collection_id not in self._describe:
            raise KeyError(collection_id)
        return self._describe[collection_id]


class _FakeOpeneoModule:
    """A stand-in for the `openeo` module exposing connect + __version__."""

    __version__ = "0.0-test"

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connect(self, url: str) -> _FakeConnection:
        """Return the canned connection regardless of URL."""
        return self._connection


@pytest.fixture
def fake_openeo(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `openeo` module covering the curated recipes' needs."""
    conn = _FakeConnection(
        collections=[
            "SENTINEL2_L2A",
            "SENTINEL_5P_L2",
            "SENTINEL1_GRD",
            "SENTINEL3_OLCI_L2_WATER",
        ],
        processes=[
            "mask_scl_dilation",
            "ndvi",
            "aggregate_temporal_period",
            "reduce_dimension",
            "sar_backscatter",
        ],
    )
    monkeypatch.setitem(sys.modules, "openeo", _FakeOpeneoModule(conn))
    return conn


@pytest.mark.openeo
class TestProbe:
    """`probe` describes one collection's live metadata."""

    def _module(self) -> "_FakeOpeneoModule":
        """A fake openeo whose describe_collection returns canned S2 metadata."""
        meta = {
            "title": "Sentinel-2 L2A",
            "summaries": {"eo:bands": [{"name": "B04"}, {"name": "B08"}], "gsd": [10]},
            "extent": {
                "spatial": {"bbox": [[-180, -90, 180, 90]]},
                "temporal": {"interval": [["2015-06-27T10:25:31Z", None]]},
            },
        }
        conn = _FakeConnection(
            collections=[], processes=[], describe={"SENTINEL2_L2A": meta}
        )
        return _FakeOpeneoModule(conn)

    def test_human_output(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """The human report prints bands, extent, and gsd."""
        monkeypatch.setitem(sys.modules, "openeo", self._module())
        assert probe.main(["SENTINEL2_L2A"]) == 0
        out = capsys.readouterr().out
        assert "B04" in out and "Sentinel-2 L2A" in out

    def test_yaml_output(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """The --yaml mode emits a paste-ready stanza."""
        monkeypatch.setitem(sys.modules, "openeo", self._module())
        assert probe.main(["SENTINEL2_L2A", "--yaml"]) == 0
        out = capsys.readouterr().out
        assert "collection_id: SENTINEL2_L2A" in out and "default_bands:" in out

    def test_unknown_collection_exit_1(self, monkeypatch: pytest.MonkeyPatch):
        """An undescribable collection exits non-zero."""
        monkeypatch.setitem(sys.modules, "openeo", self._module())
        assert probe.main(["NO_SUCH_COLLECTION"]) == 1
