"""Unit tests for the STAC catalog tooling (`tools/stac/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "stac"
sys.path.insert(0, str(_TOOLS_DIR))

import audit_stac_catalog as audit  # noqa: E402
import probe_stac_assets as probe  # noqa: E402
import refresh_stac_catalog as refresh  # noqa: E402


class _FakeColl:
    """A pystac Collection stand-in carrying only an id."""

    def __init__(self, cid: str) -> None:
        self.id = cid


class _FakeSearch:
    """A pystac-client search stand-in yielding a fixed item list."""

    def __init__(self, items: list) -> None:
        self._items = items

    def items(self):
        """Yield the configured items."""
        return iter(self._items)


class _FakeCmdClient:
    """A pystac-client Client stand-in for the tool _cmd_* paths."""

    def __init__(self, collection_ids: list[str] | None = None, items: list | None = None) -> None:
        self._collection_ids = collection_ids or []
        self._items = items or []

    def get_collections(self) -> list[_FakeColl]:
        """Return canned collections."""
        return [_FakeColl(c) for c in self._collection_ids]

    def search(self, **kwargs) -> _FakeSearch:
        """Return canned search items."""
        return _FakeSearch(self._items)


def _install_open_client(monkeypatch, client: _FakeCmdClient) -> None:
    """Inject a fake `pyramids.stac` whose `open_client` returns `client`."""
    import types

    mod = types.ModuleType("pyramids.stac")
    mod.open_client = lambda url, **kwargs: client
    monkeypatch.setitem(sys.modules, "pyramids.stac", mod)


def _fake_list_ids(url: str, *, verbose: bool = False) -> list[str]:
    """Stand-in for refresh._list_collection_ids returning canned ids."""
    return ["canned-collection"]


@pytest.mark.stac
class TestRewriteAvailableCollections:
    """`_rewrite_available_collections` swaps the index block, keeps the rest."""

    def test_preserves_endpoints_block(self):
        """The endpoints block and header survive the rewrite untouched."""
        text = (
            "# header comment\n"
            "endpoints:\n  earth-search:\n    url: https://x\n    signer: anonymous\n"
            "available_collections:\n  earth-search:\n    - old-id\n"
        )
        out = refresh._rewrite_available_collections(text, {"earth-search": ["a", "b"]})
        assert "# header comment" in out
        parsed = yaml.safe_load(out)
        assert parsed["endpoints"]["earth-search"]["url"] == "https://x"
        assert parsed["available_collections"]["earth-search"] == ["a", "b"]

    def test_appends_when_no_block_present(self):
        """A source lacking the block gets one appended."""
        text = "endpoints:\n  e:\n    url: u\n"
        out = refresh._rewrite_available_collections(text, {"e": ["x"]})
        parsed = yaml.safe_load(out)
        assert parsed["available_collections"] == {"e": ["x"]}
        assert parsed["endpoints"]["e"]["url"] == "u"

    def test_roundtrips_through_real_index(self):
        """Rewriting the bundled index keeps it loadable by the Catalog."""
        from earthlens.stac.catalog import CATALOG_PATH

        text = (CATALOG_PATH / "_index.yaml").read_text(encoding="utf-8")
        out = refresh._rewrite_available_collections(
            text, {"planetary-computer": ["sentinel-2-l2a"]}
        )
        parsed = yaml.safe_load(out)
        assert parsed["available_collections"]["planetary-computer"] == ["sentinel-2-l2a"]
        assert "endpoints" in parsed


@pytest.mark.stac
class TestProbeAssetSchema:
    """`_asset_schema` recovers per-asset band metadata from a STAC item."""

    def test_extracts_eo_and_raster_band_fields(self):
        """common_name comes from eo:bands, dtype/nodata from raster:bands."""
        item = {
            "assets": {
                "B04": {
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "eo:bands": [{"common_name": "red"}],
                    "raster:bands": [{"data_type": "uint16", "nodata": 0}],
                }
            }
        }
        schema = probe._asset_schema(item)
        assert schema["B04"] == {
            "media_type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "common_name": "red",
            "dtype": "uint16",
            "nodata": 0,
        }

    def test_missing_band_extensions_yield_none(self):
        """An asset without band extensions yields None fields, not an error."""
        schema = probe._asset_schema({"assets": {"data": {"type": "image/tiff"}}})
        assert schema["data"] == {
            "media_type": "image/tiff",
            "common_name": None,
            "dtype": None,
            "nodata": None,
        }

    def test_asset_fields_reads_pystac_like_object(self):
        """A pystac-like Asset (media_type + extra_fields) is normalised to a dict."""
        from types import SimpleNamespace

        asset = SimpleNamespace(
            media_type="image/tiff", extra_fields={"raster:bands": [{"data_type": "int16"}]}
        )
        fields = probe._asset_fields(asset)
        assert fields["type"] == "image/tiff"
        assert fields["raster:bands"][0]["data_type"] == "int16"


@pytest.mark.stac
class TestAuditDiff:
    """`_diff_collections` / `_curated_resolved` flag catalog-vs-live drift."""

    def test_diff_reports_missing_and_untracked(self):
        """Curated-not-live is 'missing'; live-not-curated is 'untracked'."""
        curated = {"e": {"a", "b"}}
        live = {"e": {"b", "c"}}
        report = audit._diff_collections(curated, live)
        assert report["e"]["missing"] == ["a"]
        assert report["e"]["untracked"] == ["c"]

    def test_diff_empty_when_in_sync(self):
        """No drift yields an empty report."""
        assert audit._diff_collections({"e": {"a"}}, {"e": {"a"}}) == {}

    def test_curated_resolved_applies_aliases(self):
        """Each endpoint maps to its curated collections' resolved ids."""
        from earthlens.stac.catalog import Catalog

        resolved = audit._curated_resolved(Catalog())
        assert "sentinel-2-c1-l2a" in resolved["earth-search"]
        assert "sentinel-2-l2a" in resolved["planetary-computer"]
        assert "sentinel-1-grd" in resolved["cdse"]


@pytest.mark.stac
class TestRefreshCmd:
    """`refresh` rewrites the index offline with canned collection ids."""

    def test_dry_run_prints_without_writing(self, tmp_path, monkeypatch, capsys):
        """--dry-run prints the regenerated index and leaves the file untouched."""
        monkeypatch.setattr(refresh, "_list_collection_ids", _fake_list_ids)
        index = tmp_path / "_index.yaml"
        original = "endpoints:\n  e:\n    url: u\navailable_collections:\n  e:\n    - old\n"
        index.write_text(original, encoding="utf-8")
        rc = refresh.main(["refresh", "--catalog-index", str(index), "--dry-run"])
        assert rc == 0
        assert "canned-collection" in capsys.readouterr().out
        assert index.read_text(encoding="utf-8") == original

    def test_write_updates_index_and_reloads(self, tmp_path, monkeypatch):
        """A full refresh rewrites _index.yaml and reloads the catalog clean."""
        import shutil

        from earthlens.stac.catalog import CATALOG_PATH

        dest = tmp_path / "catalog"
        shutil.copytree(CATALOG_PATH, dest)
        monkeypatch.setattr(refresh, "_list_collection_ids", _fake_list_ids)
        rc = refresh.main(["refresh", "--catalog-index", str(dest / "_index.yaml")])
        assert rc == 0
        assert "canned-collection" in (dest / "_index.yaml").read_text(encoding="utf-8")

    def test_unknown_endpoint_returns_1(self, tmp_path, monkeypatch):
        """Refreshing an unknown endpoint key exits 1."""
        index = tmp_path / "_index.yaml"
        index.write_text("available_collections: {}\n", encoding="utf-8")
        rc = refresh.main(["refresh", "--catalog-index", str(index), "--endpoint", "nope"])
        assert rc == 1

    def test_list_collection_ids_sorts(self, monkeypatch):
        """_list_collection_ids returns the endpoint's collection ids, sorted."""
        _install_open_client(monkeypatch, _FakeCmdClient(collection_ids=["b", "a"]))
        assert refresh._list_collection_ids("https://x", verbose=True) == ["a", "b"]


@pytest.mark.stac
class TestAuditCmd:
    """`audit` reports drift between the curated catalog and live collections."""

    def test_in_sync_returns_0(self, monkeypatch, capsys):
        """When every curated id is served, --strict still exits 0."""
        from earthlens.stac.catalog import Catalog

        all_ids = sorted({i for ids in audit._curated_resolved(Catalog()).values() for i in ids})
        _install_open_client(monkeypatch, _FakeCmdClient(collection_ids=all_ids))
        rc = audit.main(["audit", "--strict"])
        assert rc == 0

    def test_missing_collection_strict_returns_1(self, monkeypatch):
        """A curated collection the endpoint no longer serves fails --strict."""
        _install_open_client(monkeypatch, _FakeCmdClient(collection_ids=[]))
        assert audit.main(["audit", "--strict"]) == 1
        assert audit.main(["audit"]) == 0

    def test_walk_failure_returns_1(self, monkeypatch):
        """An endpoint that fails to list collections exits 1."""
        import types

        mod = types.ModuleType("pyramids.stac")

        def _boom(url, **kwargs):
            raise RuntimeError("network down")

        mod.open_client = _boom
        monkeypatch.setitem(sys.modules, "pyramids.stac", mod)
        assert audit.main(["audit"]) == 1


@pytest.mark.stac
class TestProbeCmd:
    """`probe` dumps the sample item's asset schema as JSON."""

    def test_probe_prints_asset_schema(self, monkeypatch, capsys):
        """A found item's asset schema is emitted as JSON to stdout."""
        item = {"assets": {"red": {"type": "image/tiff", "eo:bands": [{"common_name": "red"}]}}}
        _install_open_client(monkeypatch, _FakeCmdClient(items=[item]))
        rc = probe.main(["probe", "earth-search", "sentinel-2-l2a"])
        out = capsys.readouterr().out
        assert rc == 0
        assert '"common_name": "red"' in out

    def test_probe_no_items_returns_1(self, monkeypatch):
        """A collection that yields no items exits 1."""
        _install_open_client(monkeypatch, _FakeCmdClient(items=[]))
        assert probe.main(["probe", "earth-search", "sentinel-2-l2a"]) == 1

    def test_probe_unknown_endpoint_returns_1(self, monkeypatch):
        """An unknown endpoint key exits 1 before any network call."""
        assert probe.main(["probe", "no-such-endpoint", "x"]) == 1

    def test_probe_out_writes_json_file(self, monkeypatch, tmp_path):
        """--out writes the schema JSON to the given path."""
        item = {"assets": {"red": {"type": "image/tiff", "eo:bands": [{"common_name": "red"}]}}}
        _install_open_client(monkeypatch, _FakeCmdClient(items=[item]))
        out = tmp_path / "schema.json"
        rc = probe.main(["probe", "earth-search", "sentinel-2-l2a", "--out", str(out)])
        assert rc == 0
        assert '"common_name": "red"' in out.read_text(encoding="utf-8")


@pytest.mark.stac
class TestArgparse:
    """Each tool's argparse requires a subcommand and supports --help."""

    @pytest.mark.parametrize("module", [refresh, probe, audit])
    def test_no_subcommand_exits_2(self, module):
        """Invoking with no subcommand is a usage error (exit 2)."""
        with pytest.raises(SystemExit) as exc:
            module.main([])
        assert exc.value.code == 2

    @pytest.mark.parametrize("module", [refresh, probe, audit])
    def test_help_exits_0(self, module):
        """--help prints usage and exits 0."""
        with pytest.raises(SystemExit) as exc:
            module.main(["--help"])
        assert exc.value.code == 0
