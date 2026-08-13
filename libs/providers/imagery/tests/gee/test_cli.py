"""Tests for the GEE catalog-tooling handlers (`earthlens.gee.cli`).

Moved out of core's CLI test suite when the GEE handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import earthlens.gee.cli as gee_cli
from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import coverage_one, refresh_one
from earthlens.cli.stanza import emit_stanza

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the gee backend."""
    return next(b for b in list_backends() if b.provider == "gee")


class TestRefresher:
    """Tests for the GEE (EE STAC walk) lister."""

    def test_fetches_ids_for_each_dataset_href(self, monkeypatch):
        """gee refresh walks the tree then fetches each dataset doc's id."""
        monkeypatch.setattr(
            gee_cli, "_gee_dataset_hrefs", lambda: ["h/a", "h/b", "h/c"]
        )
        monkeypatch.setattr(
            gee_cli, "_gee_fetch_id", lambda href: href.rsplit("/", 1)[1].upper()
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "gee refresh ran"
        assert outcome.live_count == 3, "A/B/C ids fetched"

    def test_grouped_fetches_each_id(self, monkeypatch):
        """gee grouped maps each dataset href to its fetched id."""
        monkeypatch.setattr(gee_cli, "_gee_dataset_hrefs", lambda: ["h1", "h2"])
        monkeypatch.setattr(gee_cli, "_gee_fetch_id", lambda href: href.upper())
        assert gee_cli.refresher(None) == {"gee": ["H1", "H2"]}

    def test_fetch_id_and_error(self, monkeypatch):
        """_gee_fetch_id reads the doc id; an error degrades to None."""
        monkeypatch.setattr(gee_cli, "get_json", lambda url: {"id": "X/Y"})
        assert gee_cli._gee_fetch_id("h") == "X/Y"

        def boom(url):
            raise RuntimeError("offline")

        monkeypatch.setattr(gee_cli, "get_json", boom)
        assert gee_cli._gee_fetch_id("h") is None


class TestDatasetHrefs:
    """Tests for the EE STAC tree walk."""

    def test_bfs_collects_dataset_hrefs(self, monkeypatch):
        """The walk recurses sub-catalogs and collects dataset doc hrefs."""
        tree = {
            gee_cli._GEE_STAC_ROOT: {
                "links": [
                    {"rel": "child", "href": "https://x/sub/catalog.json"},
                    {"rel": "child", "href": "https://x/ds_a.json"},
                    {"rel": "self", "href": "ignored"},
                    {"rel": "child"},
                ]
            },
            "https://x/sub/catalog.json": {
                "links": [{"rel": "child", "href": "https://x/ds_b.json"}]
            },
        }

        def fake_get(url):
            if url == "https://x/unreachable":
                raise RuntimeError("boom")
            return tree[url]

        monkeypatch.setattr(gee_cli, "get_json", fake_get)
        hrefs = gee_cli._gee_dataset_hrefs()
        assert set(hrefs) == {"https://x/ds_a.json", "https://x/ds_b.json"}, hrefs

    def test_unreachable_subcatalog_skipped(self, monkeypatch):
        """An unreachable sub-catalog is skipped rather than raising."""

        def fake_get(url):
            raise RuntimeError("offline")

        monkeypatch.setattr(gee_cli, "get_json", fake_get)
        assert gee_cli._gee_dataset_hrefs() == [], "all unreachable -> []"


class TestClassify:
    """Tests for the gee curation-coverage classifier (network mocked)."""

    def test_curated_id_is_done(self, monkeypatch):
        """An already-curated asset is bucketed DONE without a fetch."""
        monkeypatch.setattr(
            gee_cli, "_gee_stac_or_none", lambda aid: pytest.fail("no fetch")
        )
        assert gee_cli._gee_classify("X/Y", {"X/Y"}) == "DONE"

    def test_bands_with_metadata_are_addressable(self, monkeypatch):
        """An image with a band carrying gee:units is addressable."""
        monkeypatch.setattr(
            gee_cli,
            "_gee_stac_or_none",
            lambda aid: {"summaries": {"eo:bands": [{"name": "B1", "gee:units": "K"}]}},
        )
        assert gee_cli._gee_classify("X/Y", set()) == "addressable"

    def test_bare_bands_are_thin(self, monkeypatch):
        """An image whose bands carry no usable metadata is thin."""
        monkeypatch.setattr(
            gee_cli,
            "_gee_stac_or_none",
            lambda aid: {"summaries": {"eo:bands": [{"name": "B1"}]}},
        )
        assert gee_cli._gee_classify("X/Y", set()) == "thin"

    def test_feature_collection_is_table(self, monkeypatch):
        """A FeatureCollection is bucketed table (out of raster scope)."""
        monkeypatch.setattr(
            gee_cli, "_gee_stac_or_none", lambda aid: {"gee:type": "table"}
        )
        assert gee_cli._gee_classify("X/Y", set()) == "table"

    def test_no_doc_is_missing(self, monkeypatch):
        """An asset with no STAC document is bucketed missing."""
        monkeypatch.setattr(gee_cli, "_gee_stac_or_none", lambda aid: None)
        assert gee_cli._gee_classify("X/Y", set()) == "missing"


class TestCoverage:
    """Tests for the _gee coverage classifier body + coverage_one driver."""

    def test_buckets_available_universe(self, monkeypatch):
        """Each available id is classified; addressable ids feed the todo list."""
        monkeypatch.setattr(
            gee_cli,
            "_gee_classify",
            lambda aid, cur: "DONE" if aid in cur else "addressable",
        )
        cat = SimpleNamespace(available_datasets=["A", "B"], datasets={"A": None})
        counts, todo = gee_cli.coverage(cat)
        assert counts["DONE"] == 1 and counts["addressable"] == 1, counts
        assert todo == ["B"], "uncurated addressable id queued"

    def test_empty_index_raises(self):
        """An empty available_datasets index raises a clear ValueError."""
        with pytest.raises(ValueError, match="available_datasets"):
            gee_cli.coverage(SimpleNamespace(available_datasets=[], datasets={}))

    def test_coverage_one_buckets_available_universe(self, monkeypatch):
        """coverage_one classifies each available id and lists the addressable todo."""
        monkeypatch.setitem(
            refresh_mod._COVERAGE,
            "gee",
            lambda catalog: (
                {"DONE": 1, "addressable": 1, "thin": 1, "table": 0, "missing": 0},
                ["B"],
            ),
        )
        outcome = coverage_one(_info())
        assert outcome.status == "ok", "gee coverage ran"
        assert outcome.counts["addressable"] == 1 and outcome.todo == ["B"]

    def test_coverage_one_reports_error(self, monkeypatch):
        """coverage_one captures a classifier failure as an error outcome."""

        def boom(catalog):
            raise RuntimeError("offline")

        monkeypatch.setitem(refresh_mod._COVERAGE, "gee", boom)
        assert coverage_one(_info()).status == "error"


class TestProber:
    """Tests for the GEE band prober."""

    def test_extracts_band_schema(self, monkeypatch):
        """gee probe reads its STAC doc's eo:bands (gee:units / gsd)."""
        monkeypatch.setattr(
            gee_cli,
            "get_json",
            lambda url, **kw: {
                "summaries": {
                    "eo:bands": [{"name": "hurs", "gee:units": "%", "gsd": [27830]}]
                }
            },
        )
        result = probe_dataset(_info(), "NASA/GDDP-CMIP6")
        assert result.status == "ok", "gee probe ran"
        assert result.assets["hurs"]["units"] == "%", "units parsed"
        assert result.assets["hurs"]["gsd"] == 27830, "gsd unwrapped from list"


class TestEmitter:
    """Tests for the GEE emitter (public EE STAC)."""

    def test_seeds_bands_and_extent(self, monkeypatch):
        """The STAC doc seeds title / cadence / resolution / bands."""
        monkeypatch.setattr(
            gee_cli,
            "get_json",
            lambda url, **kw: {
                "title": "GDDP-CMIP6\nsecond line",
                "gee:type": "image_collection",
                "gee:interval": {"interval": 1, "unit": "day"},
                "extent": {
                    "temporal": {"interval": [["2015-01-01T00:00:00Z", None]]},
                    "spatial": {"bbox": [[-180, -90, 180, 90]]},
                },
                "summaries": {
                    "eo:bands": [
                        {
                            "name": "tas",
                            "description": "temp",
                            "gee:units": "K",
                            "gsd": [27830],
                        }
                    ]
                },
                "providers": [{"name": "NASA"}],
            },
        )
        result = emit_stanza(_info(), "NASA/GDDP-CMIP6")
        assert result.status == "ok", "gee emitter ran"
        assert result.row["title"] == "GDDP-CMIP6", "first title line only"
        assert result.row["cadence"] == {"interval": 1, "unit": "day"}
        assert result.row["spatial_resolution"] == 27830.0, "gsd unwrapped"
        assert result.row["bands"]["tas"]["units"] == "K", "band units kept"
        assert "bbox" not in result.row["extent"], "global bbox dropped"

    def test_minimal_skips_fetch(self):
        """--minimal emits a placeholder row with empty bands and no network."""
        result = emit_stanza(_info(), "projects/foo/bar", minimal=True)
        assert result.status == "ok" and result.row["bands"] == {}

    def test_hydrate_reads_bands_from_earth_engine(self, monkeypatch):
        """--hydrate seeds bands from a live Earth Engine query (creds-gated)."""
        monkeypatch.setattr(
            gee_cli,
            "_gee_live_bands",
            lambda asset_id: ("image", {"B1": {}, "B2": {}}),
        )
        result = emit_stanza(_info(), "projects/foo/bar", hydrate=True)
        assert result.status == "ok", "gee hydrate ran"
        assert result.row["ee_type"] == "image", "ee_type from EE asset"
        assert sorted(result.row["bands"]) == ["B1", "B2"], "live bands seeded"


class TestLiveBands:
    """Tests for _gee_live_bands (Earth Engine mocked)."""

    def test_reads_bands_off_first_image(self, monkeypatch):
        """An image-collection asset resolves its first image's band names."""
        import earthlens.gee.auth as auth_mod

        fake_ee = types.ModuleType("ee")

        class _Img:
            def bandNames(self):
                return self

            def getInfo(self):
                return ["B1", "B2"]

        class _IC:
            def first(self):
                return _Img()

        fake_ee.data = types.SimpleNamespace(
            getAsset=lambda aid: {"type": "IMAGE_COLLECTION"}
        )
        fake_ee.Image = lambda x: _Img()
        fake_ee.ImageCollection = lambda aid: _IC()
        monkeypatch.setitem(sys.modules, "ee", fake_ee)

        class FakeAuth:
            @staticmethod
            def initialize(service_account, service_key, project=None):
                pass

        monkeypatch.setattr(auth_mod, "EarthEngineAuth", FakeAuth)
        ee_type, bands = gee_cli._gee_live_bands("projects/x/y")
        assert ee_type == "image_collection", "asset type lowercased"
        assert sorted(bands) == ["B1", "B2"], "live band names read"
