"""Unit tests for `earthlens.cli._gee_hydrate` (Earth Engine mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from earthlens.cli import _gee_hydrate as hydrate_mod
from earthlens.cli._gee_hydrate import (
    _find_file_for_asset,
    _rewrite_stanza,
    bulk_hydrate_empty,
)

pytestmark = pytest.mark.cli

_STANZA = """datasets:
  FOO/BAR:
    title: '(community-published catalog reference)'
    ee_type: image_collection
    extent:
      start_date: "1970-01-01"
      end_date: null
    bands: {}
  OTHER/THING:
    title: keep me
    bands:
      X: {}
"""

_PAYLOAD = {
    "ee_type": "image",
    "title": "Real Title",
    "start_date": "2001-02-03",
    "end_date": "2020-01-01",
    "bands": [{"id": "B1", "units": "K"}, {"id": "B2"}],
}


class TestRewriteStanza:
    """Tests for the pure stanza-rewriting core."""

    def test_fills_placeholder_fields(self):
        """ee_type / title / dates / bands are spliced into the target stanza."""
        out = _rewrite_stanza(
            _STANZA, "FOO/BAR", _PAYLOAD, "(community-published catalog reference)"
        )
        parsed = yaml.safe_load(out)["datasets"]["FOO/BAR"]
        assert parsed["ee_type"] == "image", "ee_type filled"
        assert parsed["title"] == "Real Title", "placeholder title overwritten"
        assert list(parsed["bands"]) == ["B1", "B2"], "bands seeded"

    def test_leaves_siblings_untouched(self):
        """The other stanzas in the file are preserved byte-for-byte."""
        out = _rewrite_stanza(_STANZA, "FOO/BAR", _PAYLOAD, _PAYLOAD["title"])
        assert "  OTHER/THING:\n    title: keep me\n" in out, "sibling intact"

    def test_keeps_real_title(self):
        """A non-placeholder existing title is not overwritten."""
        out = _rewrite_stanza(_STANZA, "FOO/BAR", _PAYLOAD, "A real curated title")
        assert "Real Title" not in out, "real title kept"

    def test_missing_stanza_returns_input(self):
        """An asset id not present returns the text unchanged."""
        assert _rewrite_stanza(_STANZA, "NOPE/NONE", _PAYLOAD, None) == _STANZA


class TestFindFileForAsset:
    """Tests for locating the per-family file holding a stanza."""

    def test_finds_the_owning_file(self, tmp_path):
        """The file whose body has the asset head is returned; _index skipped."""
        (tmp_path / "_index.yaml").write_text("available_datasets: []\n")
        (tmp_path / "sar-radar.yaml").write_text(_STANZA, encoding="utf-8")
        assert _find_file_for_asset(tmp_path, "FOO/BAR").name == "sar-radar.yaml"

    def test_returns_none_when_absent(self, tmp_path):
        """An asset in no file returns None."""
        (tmp_path / "a.yaml").write_text(_STANZA, encoding="utf-8")
        assert _find_file_for_asset(tmp_path, "NOT/HERE") is None


class TestBulkHydrateEmpty:
    """Tests for the catalog-wide hydrate driver (EE + catalog mocked)."""

    def test_fills_every_empty_row_in_place(self, tmp_path, monkeypatch):
        """Each empty-band row is hydrated and written back to its file."""
        catalog_file = tmp_path / "sar-radar.yaml"
        catalog_file.write_text(_STANZA, encoding="utf-8")

        fake_catalog = SimpleNamespace(
            datasets={
                "FOO/BAR": SimpleNamespace(
                    bands={}, title="(community-published catalog reference)"
                ),
                "OTHER/THING": SimpleNamespace(bands={"X": {}}, title="keep me"),
            }
        )
        import earthlens.gee as gee
        import earthlens.gee.catalog as gee_catalog

        monkeypatch.setattr(gee, "Catalog", lambda: fake_catalog)
        monkeypatch.setattr(gee_catalog, "CATALOG_PATH", tmp_path)
        monkeypatch.setattr(gee_catalog, "clear_catalog_cache", lambda: None)
        monkeypatch.setattr(hydrate_mod, "_configure_ee", lambda: None)
        monkeypatch.setattr(
            hydrate_mod, "_fetch_asset_payload", lambda aid, ee: dict(_PAYLOAD)
        )

        summary = bulk_hydrate_empty()
        assert summary == {
            "candidates": 1,
            "hydrated": 1,
            "skipped": 0,
            "filled": ["FOO/BAR"],
        }
        parsed = yaml.safe_load(catalog_file.read_text())["datasets"]
        assert list(parsed["FOO/BAR"]["bands"]) == ["B1", "B2"], "row hydrated on disk"

    def test_skips_unreadable_assets(self, tmp_path, monkeypatch):
        """An asset whose EE read returns None is skipped, not hydrated."""
        (tmp_path / "sar-radar.yaml").write_text(_STANZA, encoding="utf-8")
        fake_catalog = SimpleNamespace(
            datasets={"FOO/BAR": SimpleNamespace(bands={}, title="x")}
        )
        import earthlens.gee as gee
        import earthlens.gee.catalog as gee_catalog

        monkeypatch.setattr(gee, "Catalog", lambda: fake_catalog)
        monkeypatch.setattr(gee_catalog, "CATALOG_PATH", tmp_path)
        monkeypatch.setattr(gee_catalog, "clear_catalog_cache", lambda: None)
        monkeypatch.setattr(hydrate_mod, "_configure_ee", lambda: None)
        monkeypatch.setattr(hydrate_mod, "_fetch_asset_payload", lambda aid, ee: None)

        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 0 and summary["skipped"] == 1
