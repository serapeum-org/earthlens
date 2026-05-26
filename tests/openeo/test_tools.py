"""Unit tests for the openEO catalog tooling (`tools/openeo/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "openeo"
sys.path.insert(0, str(_TOOLS_DIR))

import audit_openeo_datasets as audit  # noqa: E402
import probe_openeo_collection as probe  # noqa: E402
import refresh_openeo_catalog as refresh  # noqa: E402


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
class TestRefresh:
    """`refresh` lists collections + processes and renders the index."""

    def test_dry_run_prints_index(self, fake_openeo, capsys):
        """A dry-run prints a parseable index without writing."""
        import yaml

        code = refresh.main(["refresh", "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        parsed = yaml.safe_load(out)
        assert "SENTINEL2_L2A" in parsed["available_collections"]
        assert "ndvi" in parsed["available_processes"]

    def test_writes_index_file(self, fake_openeo, tmp_path: Path, monkeypatch):
        """A non-dry run writes the index and reloads it to validate."""
        import yaml

        index = tmp_path / "_index.yaml"
        index.write_text("available_collections: []\n", encoding="utf-8")
        # Reload uses the real Catalog; point it at a dir with curated rows too.
        (tmp_path / "collections.yaml").write_text(
            "collections:\n  k:\n    collection_id: SENTINEL2_L2A\n", encoding="utf-8"
        )
        code = refresh.main(["refresh", "--catalog-index", str(index)])
        assert code == 0
        parsed = yaml.safe_load(index.read_text(encoding="utf-8"))
        assert "SENTINEL2_L2A" in parsed["available_collections"]

    def test_connect_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch):
        """A connection failure exits non-zero."""

        class _BoomModule:
            __version__ = "x"

            def connect(self, url):
                raise RuntimeError("network down")

        monkeypatch.setitem(sys.modules, "openeo", _BoomModule())
        assert refresh.main(["refresh", "--dry-run"]) == 1


@pytest.mark.openeo
class TestValidateRecipe:
    """`validate-recipe` checks a recipe against the live catalog."""

    def test_consistent_recipe_passes(self, fake_openeo):
        """A recipe whose collection + processes exist live validates clean."""
        assert refresh.main(["validate-recipe", "sentinel-2-l2a-ndvi-monthly"]) == 0

    def test_unknown_recipe_returns_1(self, fake_openeo):
        """An unknown recipe key exits non-zero."""
        assert refresh.main(["validate-recipe", "no-such-recipe"]) == 1

    def test_drift_detected(self, monkeypatch: pytest.MonkeyPatch):
        """A recipe whose process is absent from the backend is flagged."""
        conn = _FakeConnection(collections=["SENTINEL2_L2A"], processes=["ndvi"])
        monkeypatch.setitem(sys.modules, "openeo", _FakeOpeneoModule(conn))
        # ndvi exists but mask_scl_dilation/aggregate_temporal_period do not.
        assert refresh.main(["validate-recipe", "sentinel-2-l2a-ndvi-monthly"]) == 1


@pytest.mark.openeo
class TestAudit:
    """`audit` diffs the curated catalog against the live backend."""

    def _full_module(self) -> "_FakeOpeneoModule":
        """A fake openeo whose live collections cover every curated id (no drift)."""
        from earthlens.openeo.catalog import Catalog

        cat = Catalog()
        live = sorted({c.collection_id for c in cat.datasets.values()})
        procs = [
            "mask_scl_dilation",
            "ndvi",
            "aggregate_temporal_period",
            "reduce_dimension",
            "sar_backscatter",
        ]
        return _FakeOpeneoModule(_FakeConnection(collections=live, processes=procs))

    def test_no_drift_exit_0(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """When every curated id is served, audit reports no drift and exits 0."""
        monkeypatch.setitem(sys.modules, "openeo", self._full_module())
        assert audit.main(["audit", "--strict"]) == 0
        assert "no drift" in capsys.readouterr().out

    def test_missing_collection_strict_exit_1(self, monkeypatch: pytest.MonkeyPatch):
        """A curated collection the backend no longer serves fails under --strict."""
        conn = _FakeConnection(collections=["SENTINEL2_L2A"], processes=["ndvi"])
        monkeypatch.setitem(sys.modules, "openeo", _FakeOpeneoModule(conn))
        assert audit.main(["audit", "--strict"]) == 1

    def test_drift_without_strict_exit_0(self, monkeypatch: pytest.MonkeyPatch):
        """Drift without --strict prints the report but exits 0."""
        conn = _FakeConnection(collections=["SENTINEL2_L2A"], processes=["ndvi"])
        monkeypatch.setitem(sys.modules, "openeo", _FakeOpeneoModule(conn))
        assert audit.main(["audit"]) == 0

    def test_connect_failure_exit_1(self, monkeypatch: pytest.MonkeyPatch):
        """A listing failure exits non-zero."""

        class _BoomModule:
            def connect(self, url):
                raise RuntimeError("backend unreachable")

        monkeypatch.setitem(sys.modules, "openeo", _BoomModule())
        assert audit.main(["audit"]) == 1


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
