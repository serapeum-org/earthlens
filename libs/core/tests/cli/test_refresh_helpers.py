"""Coverage for `earthlens.cli.refresh` network/SDK helpers (all mocked)."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest
import yaml

from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import coverage_one

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestGetText:
    """Tests for the shared `_get_text` HTTP helper."""

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
