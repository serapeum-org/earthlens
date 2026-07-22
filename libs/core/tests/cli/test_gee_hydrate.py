"""Unit tests for `earthlens.cli._gee_hydrate` (Earth Engine mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from earthlens.cli._gee_hydrate import (
    _band_records,
    _configure_ee,
    _date_window,
    _fetch_asset_payload,
    _find_file_for_asset,
    _looks_like_placeholder_title,
    _properties_text,
    _rewrite_stanza,
    _splice_bands,
    _strip_html,
    bulk_hydrate_empty,
)

from earthlens.cli import _gee_hydrate as hydrate_mod

pytestmark = pytest.mark.cli


class _FakeImage:
    """Stand-in for `ee.Image` whose bandNames().getInfo() returns fixed names."""

    def __init__(self, names):
        self._names = names

    def bandNames(self):
        return self

    def getInfo(self):
        return self._names


class _FakeCollection:
    """Stand-in for `ee.ImageCollection` returning a `_FakeImage` first image."""

    def __init__(self, names):
        self._names = names

    def first(self):
        return _FakeImage(self._names)


class _FakeData:
    """Stand-in for `ee.data` whose getAsset returns a fixed asset (or raises)."""

    def __init__(self, asset):
        self._asset = asset

    def getAsset(self, asset_id):
        if self._asset is None:
            raise RuntimeError("getAsset failed")
        return self._asset


class _FakeEE:
    """Minimal fake of the `ee` module for the hydrate helpers."""

    def __init__(self, asset, names=None):
        self.data = _FakeData(asset)
        self._names = names or []

    def Image(self, asset_id):
        return _FakeImage(self._names)

    def ImageCollection(self, asset_id):
        return _FakeCollection(self._names)


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

    def test_limit_caps_candidates(self, tmp_path, monkeypatch):
        """A --limit truncates the empty-row worklist."""
        (tmp_path / "f.yaml").write_text(
            "datasets:\n  A/B:\n    bands: {}\n  C/D:\n    bands: {}\n",
            encoding="utf-8",
        )
        fake = SimpleNamespace(
            datasets={
                "A/B": SimpleNamespace(bands={}, title="x"),
                "C/D": SimpleNamespace(bands={}, title="y"),
            }
        )
        import earthlens.gee as gee
        import earthlens.gee.catalog as gee_catalog

        monkeypatch.setattr(gee, "Catalog", lambda: fake)
        monkeypatch.setattr(gee_catalog, "CATALOG_PATH", tmp_path)
        monkeypatch.setattr(gee_catalog, "clear_catalog_cache", lambda: None)
        monkeypatch.setattr(hydrate_mod, "_configure_ee", lambda: None)
        monkeypatch.setattr(hydrate_mod, "_fetch_asset_payload", lambda a, e: None)
        summary = bulk_hydrate_empty(limit=1)
        assert summary["candidates"] == 1, "limit applied to the worklist"


class TestStripHtml:
    """Tests for _strip_html."""

    def test_removes_tags_and_collapses_whitespace(self):
        """HTML tags + entities + whitespace runs collapse to single spaces."""
        assert _strip_html("<p>Sea&nbsp;  surface</p>\n temp") == "Sea surface temp"


class TestLooksLikePlaceholderTitle:
    """Tests for _looks_like_placeholder_title."""

    @pytest.mark.parametrize(
        "title, expected",
        [
            (None, True),
            ("", True),
            ("   ", True),
            ("x (community-published catalog reference)", True),
            ("A real title", False),
        ],
    )
    def test_classification(self, title, expected):
        """A blank / community-reference title reads as a placeholder.

        Args:
            title: The candidate existing title.
            expected: Whether it should read as a placeholder.
        """
        assert _looks_like_placeholder_title(title) is expected


class TestDateWindow:
    """Tests for _date_window."""

    def test_truncates_iso_timestamps_to_date(self):
        """startTime / endTime ISO stamps are clipped to YYYY-MM-DD."""
        asset = {"startTime": "2001-02-03T00:00:00Z", "endTime": "2020-12-31T23:59Z"}
        assert _date_window(asset) == ("2001-02-03", "2020-12-31")

    def test_missing_dates_return_none(self):
        """An asset with no time fields yields (None, None)."""
        assert _date_window({}) == (None, None)


class TestPropertiesText:
    """Tests for _properties_text."""

    def test_returns_first_non_empty_key(self):
        """The first present, truthy property among the keys is returned."""
        asset = {"properties": {"title": "", "system:title": "Real"}}
        assert _properties_text(asset, "title", "system:title") == "Real"

    def test_none_when_no_key_present(self):
        """No matching property returns None."""
        assert _properties_text({"properties": {}}, "title") is None


class TestBandRecords:
    """Tests for _band_records (asset-doc bands, else live band names)."""

    def test_prefers_asset_doc_bands(self):
        """When the asset doc carries bands, they are returned verbatim."""
        asset = {"type": "IMAGE", "bands": [{"id": "B1"}]}
        assert _band_records("X/Y", asset, _FakeEE(asset)) == [{"id": "B1"}]

    def test_image_falls_back_to_live_band_names(self):
        """An IMAGE with no doc bands reads names off ee.Image."""
        asset = {"type": "IMAGE"}
        ee = _FakeEE(asset, names=["a", "b"])
        assert _band_records("X/Y", asset, ee) == [{"id": "a"}, {"id": "b"}]

    def test_image_collection_uses_first_image(self):
        """An IMAGE_COLLECTION reads names off its first image."""
        asset = {"type": "IMAGE_COLLECTION"}
        assert _band_records("X/Y", asset, _FakeEE(asset, names=["c"])) == [{"id": "c"}]

    def test_non_raster_type_returns_empty(self):
        """A TABLE asset has no bands."""
        assert _band_records("X/Y", {"type": "TABLE"}, _FakeEE({})) == []

    def test_ee_error_returns_empty(self):
        """An exception resolving live band names degrades to []."""

        class Boom:
            def Image(self, asset_id):
                raise RuntimeError("denied")

        assert _band_records("X/Y", {"type": "IMAGE"}, Boom()) == []


class TestFetchAssetPayload:
    """Tests for _fetch_asset_payload."""

    def test_builds_payload_from_asset(self):
        """A readable IMAGE asset yields ee_type / dates / title / bands."""
        asset = {
            "type": "IMAGE",
            "bands": [{"id": "B1"}],
            "startTime": "2001-02-03T00:00:00Z",
            "endTime": "2020-01-01T00:00:00Z",
            "properties": {"title": "Real Title"},
        }
        payload = _fetch_asset_payload("X/Y", _FakeEE(asset))
        assert payload["ee_type"] == "image", "type lowercased"
        assert payload["title"] == "Real Title", "title read from properties"
        assert payload["start_date"] == "2001-02-03", "start clipped"
        assert payload["bands"] == [{"id": "B1"}], "bands carried"

    def test_long_html_title_is_cleaned_and_truncated(self):
        """An HTML title over 180 chars is stripped and clipped to 180."""
        asset = {"type": "IMAGE", "properties": {"title": "<b>" + "x" * 200 + "</b>"}}
        payload = _fetch_asset_payload("X/Y", _FakeEE(asset))
        assert len(payload["title"]) == 180, "title truncated to 180"

    def test_no_title_yields_none_title(self):
        """An asset with no title property yields title=None."""
        payload = _fetch_asset_payload("X/Y", _FakeEE({"type": "IMAGE", "bands": []}))
        assert payload["title"] is None, "absent title -> None"

    def test_table_asset_has_no_bands(self):
        """A TABLE asset hydrates with an empty band list."""
        payload = _fetch_asset_payload("X/Y", _FakeEE({"type": "TABLE"}))
        assert payload["ee_type"] == "table" and payload["bands"] == []

    def test_unreadable_asset_returns_none(self):
        """A getAsset failure returns None (skipped upstream)."""
        assert _fetch_asset_payload("X/Y", _FakeEE(None)) is None


class TestSpliceBands:
    """Tests for _splice_bands."""

    def test_emits_all_band_metadata(self):
        """wavelength / units / scale / offset all render under the band."""
        out = _splice_bands(
            "    bands: {}\n",
            [
                {
                    "id": "B1",
                    "wavelength_um": 0.49,
                    "units": "K",
                    "scale": 2,
                    "offset": 5,
                }
            ],
        )
        assert "      B1:" in out and "wavelength: 0.49" in out
        assert "units: K" in out and "scale: 2" in out and "offset: 5" in out

    def test_center_wavelength_alias(self):
        """center_wavelength is used when wavelength_um is absent."""
        out = _splice_bands("    bands: {}\n", [{"id": "B1", "center_wavelength": 1.6}])
        assert "wavelength: 1.6" in out, "center_wavelength aliased to wavelength"

    def test_quotes_boolean_and_digit_band_ids(self):
        """Band ids that parse as bool / int are YAML-quoted."""
        out = _splice_bands("    bands: {}\n", [{"id": "1"}, {"name": "true"}])
        assert '      "1":' in out and '      "true":' in out

    def test_skips_band_without_id(self):
        """A band with neither id nor name is skipped."""
        out = _splice_bands("    bands: {}\n", [{"units": "K"}, {"id": "B1"}])
        assert "B1:" in out and "units: K" not in out

    def test_default_scale_offset_omitted(self):
        """A scale of 1 / offset of 0 are defaults and are omitted."""
        out = _splice_bands("    bands: {}\n", [{"id": "B1", "scale": 1, "offset": 0}])
        assert "scale:" not in out and "offset:" not in out


class TestConfigureEe:
    """Tests for _configure_ee (auth wiring mocked)."""

    def test_configures_and_returns_ee(self, monkeypatch):
        """It authenticates the service account and returns the ee module."""
        import earthlens.gee.auth as auth_mod

        calls = {}

        class FakeAuth:
            @staticmethod
            def initialize(service_account, service_key, project=None):
                calls["initialized"] = True

        monkeypatch.setattr(auth_mod, "EarthEngineAuth", FakeAuth)
        module = _configure_ee()
        assert calls.get("initialized") is True, "initialize() called"
        assert module.__name__ == "ee", "the ee module is returned"
