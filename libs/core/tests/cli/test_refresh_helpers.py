"""Coverage for `earthlens.cli.refresh` network/SDK helpers (all mocked)."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest
import yaml
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import _curated_releases, coverage_one

from earthlens.cli import refresh as refresh_mod

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestRadarParse:
    """Tests for the HOMR fixed-width radar parser + writer."""

    _COLS = [("ICAO", 5), ("NAME", 11), ("ST", 3), ("LAT", 10), ("LON", 11)]

    def _homr(self):
        """Build a tiny fixed-width HOMR table aligned to the dash rule."""

        def line(vals):
            return " ".join(v.ljust(w) for v, (_, w) in zip(vals, self._COLS))

        rule = " ".join("-" * w for _, w in self._COLS)
        header = line([name for name, _ in self._COLS])
        rows = [
            line(["KABR", "ABERDEEN", "SD", "45.4558", "-98.4131"]),
            line(["KXXX", "BADLAT", "ZZ", "999.0", "0.0"]),
            line(["AB", "SHORT", "XX", "1.0", "2.0"]),
            line(["K1AB", "NUM", "XX", "1.0", "2.0"]),
            line(["KZZZ", "NOLAT", "XX", "abc", "2.0"]),
        ]
        return "\n".join([header, rule, *rows])

    def test_keeps_only_valid_stations(self):
        """Only 4-letter alphabetic ICAO rows with in-range coords survive."""
        rows = refresh_mod._radar_station_rows(self._homr())
        assert list(rows) == ["KABR"], rows
        assert rows["KABR"]["name"] == "Aberdeen", "name title-cased"
        assert rows["KABR"]["state"] == "SD" and rows["KABR"]["latitude"] == 45.4558

    def test_short_table_is_empty(self):
        """A table with fewer than three lines yields no rows."""
        assert refresh_mod._radar_station_rows("only\ntwo") == {}

    def test_no_icao_column_is_empty(self):
        """A table whose header lacks ICAO yields no rows."""
        assert refresh_mod._radar_station_rows("FOO BAR\n--- ---\nx   y") == {}

    def test_station_ids_sorted(self):
        """_radar_station_ids returns the sorted ICAO ids only."""
        assert refresh_mod._radar_station_ids(self._homr()) == ["KABR"]

    def test_get_text_returns_body(self, monkeypatch):
        """_get_text returns the response body text."""
        monkeypatch.setattr(
            refresh_mod.requests,
            "get",
            lambda url, timeout=None: SimpleNamespace(
                text="BODY", raise_for_status=lambda: None
            ),
        )
        assert refresh_mod._get_text("https://x") == "BODY"

    def test_write_radar_rewrites_stations(self, tmp_path, monkeypatch):
        """_write_radar re-parses HOMR into the curated stations: block."""
        monkeypatch.setattr(refresh_mod, "_get_text", lambda url: self._homr())
        target = tmp_path / "radar_data_catalog.yaml"
        target.write_text("stations:\n  KOLD:\n    name: Old\n", encoding="utf-8")
        monkeypatch.setattr(refresh_mod, "_index_path", lambda info: target)
        path = refresh_mod._write_radar(_info("radar"), {"radar": ["KABR"]})
        data = yaml.safe_load(open(path))
        assert "KABR" in data["stations"], "HOMR rows written"


class TestHdxWriter:
    """Tests for the merge-preserving gzipped HDX sidecar writer."""

    def test_merge_preserves_existing_rows(self, tmp_path, monkeypatch):
        """Surviving names keep their org/title; new names get a bare row."""
        monkeypatch.setattr(
            refresh_mod, "_index_path", lambda info: tmp_path / "x.yaml"
        )
        sidecar = tmp_path / "_available.json.gz"
        with gzip.open(sidecar, "wt", encoding="utf-8") as handle:
            json.dump({"datasets": {"keep": {"org": "o", "title": "t"}}}, handle)
        path = refresh_mod._write_hdx(_info("hdx"), {"g": ["keep", "new"]})
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            datasets = json.load(handle)["datasets"]
        assert datasets["keep"] == {"org": "o", "title": "t"}, "existing row kept"
        assert datasets["new"] == {"org": "", "title": ""}, "new row bare"

    def test_no_sidecar_starts_fresh(self, tmp_path, monkeypatch):
        """With no existing sidecar, every live name gets a fresh bare row."""
        monkeypatch.setattr(
            refresh_mod, "_index_path", lambda info: tmp_path / "x.yaml"
        )
        path = refresh_mod._write_hdx(_info("hdx"), {"g": ["a"]})
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            assert "a" in json.load(handle)["datasets"], "new row created"


class TestGeeCoverageBody:
    """Tests for the _gee_coverage classifier body + coverage_one error path."""

    def test_buckets_available_universe(self, monkeypatch):
        """Each available id is classified; addressable ids feed the todo list."""
        monkeypatch.setattr(
            refresh_mod,
            "_gee_classify",
            lambda aid, cur: "DONE" if aid in cur else "addressable",
        )
        cat = SimpleNamespace(available_datasets=["A", "B"], datasets={"A": None})
        counts, todo = refresh_mod._gee_coverage(cat)
        assert counts["DONE"] == 1 and counts["addressable"] == 1, counts
        assert todo == ["B"], "uncurated addressable id queued"

    def test_empty_index_raises(self):
        """An empty available_datasets index raises a clear ValueError."""
        with pytest.raises(ValueError, match="available_datasets"):
            refresh_mod._gee_coverage(
                SimpleNamespace(available_datasets=[], datasets={})
            )

    def test_coverage_one_reports_error(self, monkeypatch):
        """coverage_one captures a classifier failure as an error outcome."""

        def boom(catalog):
            raise RuntimeError("offline")

        monkeypatch.setitem(refresh_mod._COVERAGE, "gee", boom)
        assert coverage_one(_info("gee")).status == "error"


class TestNetworkPrimitives:
    """Tests for the thin network/SDK list helpers (mocked)."""

    def test_openeo_process_ids(self, monkeypatch):
        """Process ids with an id are collected + sorted; others dropped."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url: {"processes": [{"id": "ndvi"}, {"no": "id"}]},
        )
        assert refresh_mod._openeo_process_ids() == ["ndvi"]

    def test_cmems_describe_delegates(self, monkeypatch):
        """_cmems_describe calls the SDK describe and returns the catalogue."""
        import sys
        import types

        fake = types.ModuleType("copernicusmarine")
        fake.describe = lambda disable_progress_bar=None: "CAT"
        monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
        assert refresh_mod._cmems_describe() == "CAT"

    def test_cmems_grouped_flattens(self, monkeypatch):
        """cmems grouped flattens products[].datasets[].dataset_id."""
        cat = SimpleNamespace(
            products=[
                SimpleNamespace(
                    datasets=[
                        SimpleNamespace(dataset_id="a"),
                        SimpleNamespace(dataset_id="b"),
                    ]
                )
            ]
        )
        monkeypatch.setattr(refresh_mod, "_cmems_describe", lambda: cat)
        assert refresh_mod._cmems_grouped(None) == {"cmems": ["a", "b"]}

    def test_eumetsat_grouped_reads_link_titles(self, monkeypatch):
        """Each browse link's title is taken as the collection id."""
        monkeypatch.setattr(
            refresh_mod,
            "_get_json",
            lambda url, **kw: {"links": [{"title": "EO:1"}, {"title": "EO:2"}]},
        )
        assert refresh_mod._eumetsat_grouped(None) == {"eumetsat": ["EO:1", "EO:2"]}

    def test_sentinel_hub_grouped(self, monkeypatch):
        """sentinel_hub grouped wraps the DataCollection enum names, sorted."""
        monkeypatch.setattr(
            refresh_mod, "_sh_data_collection_names", lambda: ["S2", "S1"]
        )
        assert refresh_mod._sentinel_hub_grouped(None) == {"sentinel_hub": ["S1", "S2"]}

    def test_overture_release_ids_and_grouped(self, monkeypatch):
        """release ids unwrap the (releases, latest) tuple; grouped sorts them."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["2024-01", "2023-12"], "2024-01"),
        )
        assert refresh_mod._overture_release_ids() == ["2024-01", "2023-12"]
        monkeypatch.setattr(
            refresh_mod, "_overture_release_ids", lambda: ["2024-01", "2023-12"]
        )
        assert refresh_mod._overture_grouped(None) == {
            "overture": ["2023-12", "2024-01"]
        }

    def test_fdsn_provider_ids_nonempty(self):
        """obspy's URL_MAPPINGS yields a non-empty provider id list."""
        assert refresh_mod._fdsn_provider_ids(), "obspy registry is non-empty"

    def test_gee_fetch_id_and_error(self, monkeypatch):
        """_gee_fetch_id reads the doc id; an error degrades to None."""
        monkeypatch.setattr(refresh_mod, "_get_json", lambda url: {"id": "X/Y"})
        assert refresh_mod._gee_fetch_id("h") == "X/Y"

        def boom(url):
            raise RuntimeError("offline")

        monkeypatch.setattr(refresh_mod, "_get_json", boom)
        assert refresh_mod._gee_fetch_id("h") is None

    def test_gee_grouped_fetches_each_id(self, monkeypatch):
        """gee grouped maps each dataset href to its fetched id."""
        monkeypatch.setattr(refresh_mod, "_gee_dataset_hrefs", lambda: ["h1", "h2"])
        monkeypatch.setattr(refresh_mod, "_gee_fetch_id", lambda href: href.upper())
        assert refresh_mod._gee_grouped(None) == {"gee": ["H1", "H2"]}

    def test_worldpop_grouped_crawls(self, monkeypatch):
        """worldpop grouped crawls the top alias then each alias's sub-aliases."""

        def fake(url, **kw):
            if url.rsplit("/", 1)[-1] == "data":
                return {"data": [{"alias": "pop"}]}
            return {"data": [{"alias": "wpgp"}, {"alias": "G2"}]}

        monkeypatch.setattr(refresh_mod, "_get_json", fake)
        assert refresh_mod._worldpop_grouped(None) == {"pop": ["G2", "wpgp"]}

    def test_worldpop_curated_ids(self):
        """_worldpop_curated_ids flattens each record's sub-alias ids."""
        cat = SimpleNamespace(
            datasets={"pop": SimpleNamespace(subaliases=[SimpleNamespace(id="wpgp")])}
        )
        assert refresh_mod._worldpop_curated_ids(cat) == ["wpgp"]

    def test_curated_releases(self):
        """_curated_releases returns the catalog's sorted release ids."""
        assert _curated_releases(SimpleNamespace(available_releases=["b", "a"])) == [
            "a",
            "b",
        ]


class TestChcWalk:
    """Tests for the CHC FTP product-tree walk."""

    def test_descends_to_product_dirs(self):
        """The BFS descends intermediates and stops at product directories."""

        class FakeFTP:
            listings = {
                "pub/org/chc/products": ["chirps", "readme.txt"],
                "pub/org/chc/products/chirps": ["2020", "2021"],
            }

            def cwd(self, path):
                self._cwd = "" if path == "/" else path.rstrip("/")
                if self._cwd and self._cwd not in self.listings:
                    from ftplib import error_perm

                    raise error_perm("550")

            def nlst(self):
                return self.listings[self._cwd]

        paths = refresh_mod._chc_walk(FakeFTP(), "pub/org/chc/products", 6)
        assert any(p.endswith("chirps/") for p in paths), paths

    def test_is_product_listing(self):
        """A listing with data files or year subdirs is a product directory."""
        assert refresh_mod._chc_is_product_listing(["x.tif"]) is True
        assert refresh_mod._chc_is_product_listing(["2020"]) is True
        assert refresh_mod._chc_is_product_listing(["sub", "readme"]) is False
