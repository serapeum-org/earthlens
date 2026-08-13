"""Tests for the CHC catalog-tooling handlers (`earthlens.chc.cli`).

Moved out of core's CLI test suite when the CHC handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.chc.cli as chc_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the chc backend."""
    return next(b for b in list_backends() if b.provider == "chc")


class _FakeFTP:
    """A minimal in-memory FTP stand-in for the CHC walk test."""

    def __init__(self, tree):
        self._tree = tree
        self._cwd = ""

    def cwd(self, path):
        self._cwd = "" if path == "/" else path

    def nlst(self):
        return self._tree.get(self._cwd.rstrip("/"), [])


class TestRefresher:
    """Tests for the CHC (anonymous-FTP product-tree walk) lister."""

    def test_walk_classifies_product_dirs(self):
        """A dir of data files / year-subdirs is a product dir; others descend."""
        tree = {
            "pub/org/chc/products": ["CHIRPS", "README.txt"],
            "pub/org/chc/products/CHIRPS": ["daily", "monthly"],
            "pub/org/chc/products/CHIRPS/daily": ["1981", "1982", "x.tif"],
            "pub/org/chc/products/CHIRPS/monthly": ["data.nc"],
        }
        found = chc_cli._chc_walk(_FakeFTP(tree), "pub/org/chc/products", 6)
        assert found == [
            "pub/org/chc/products/CHIRPS/daily/",
            "pub/org/chc/products/CHIRPS/monthly/",
        ], "both leaf product dirs discovered, README skipped"

    def test_refresh_diffs_against_ftp_bases(self, monkeypatch):
        """CHC diffs the live tree against catalog ftp_bases, not the slugs."""
        bases = chc_cli.bundled_ids(load_catalog(_info()))
        live = bases[:-1] + ["pub/org/chc/products/NEW_PRODUCT/daily/"]
        monkeypatch.setattr(chc_cli, "_chc_discovered_paths", lambda: live)
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "chc refresh ran"
        assert outcome.new_ids == ["pub/org/chc/products/NEW_PRODUCT/daily/"], (
            "only-on-ftp surfaced as new"
        )
        assert len(outcome.removed_ids) == 1, "the dropped base is only-in-yaml"

    def test_refresh_has_no_writer(self, monkeypatch):
        """CHC's curated-slug index can't be machine-written: live read only."""
        monkeypatch.setattr(chc_cli, "_chc_discovered_paths", lambda: [])
        outcome = refresh_one(_info(), write=True)
        assert outcome.status == "ok"
        assert "not supported" in outcome.detail


class TestWalk:
    """Tests for the CHC FTP product-tree walk helpers."""

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

        paths = chc_cli._chc_walk(FakeFTP(), "pub/org/chc/products", 6)
        assert any(p.endswith("chirps/") for p in paths), paths

    def test_is_product_listing(self):
        """A listing with data files or year subdirs is a product directory."""
        assert chc_cli._chc_is_product_listing(["x.tif"]) is True
        assert chc_cli._chc_is_product_listing(["2020"]) is True
        assert chc_cli._chc_is_product_listing(["sub", "readme"]) is False


class TestProber:
    """Tests for the CHC FTP-sample prober (anonymous FTP)."""

    def test_lists_sample_filenames(self, monkeypatch):
        """chc probe lists a sample of filenames under the dataset's ftp_base."""
        monkeypatch.setattr(
            chc_cli, "_chc_sample_files", lambda base, limit=10: ["a.tif", "b.tif"]
        )
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        assert result.status == "ok", "chc probe ran"
        assert "a.tif" in result.assets, "sample filename listed"

    def test_suggests_a_filename_pattern(self, monkeypatch):
        """chc probe adds a (suggested pattern) row inferred from the listing."""
        monkeypatch.setattr(
            chc_cli,
            "_chc_sample_files",
            lambda base, limit=10: ["chirps-v2.0.2009.01.01.tif"],
        )
        dataset = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), dataset)
        suggestion = result.assets.get("(suggested pattern)", {}).get("pattern", "")
        assert suggestion == "chirps-v2.0.{year}.{month}.{day}.tif", suggestion

    def test_suggest_pattern_empty_listing(self):
        """The pattern suggester returns empty for an empty listing."""
        assert chc_cli._suggest_pattern([]) == ""


class TestSampleFiles:
    """Tests for the anonymous-FTP CHC sampler."""

    def test_lists_directory(self, monkeypatch):
        """_chc_sample_files logs in, cds to the base, and returns sorted names."""

        class FakeFTP:
            def __init__(self, host, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self):
                pass

            def cwd(self, base):
                pass

            def nlst(self):
                return ["b.tif", "a.tif"]

        monkeypatch.setattr(chc_cli, "FTP", FakeFTP)
        assert chc_cli._chc_sample_files("/x", limit=1) == ["a.tif"], "sorted + capped"
