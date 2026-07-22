"""Edge-case + error-path tests rounding out the Sentinel Hub coverage (C12)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from earthlens.sentinel_hub._helpers import import_sentinelhub
from earthlens.sentinel_hub.backend import SentinelHub

from earthlens.sentinel_hub import Catalog
from earthlens.sentinel_hub.catalog import clear_catalog_cache

pytestmark = pytest.mark.sentinel_hub


class TestImportError:
    """The lazy importer surfaces a friendly message."""

    def test_missing_sdk(self, monkeypatch):
        """A missing `sentinelhub` raises an ImportError naming the extra."""
        monkeypatch.setitem(sys.modules, "sentinelhub", None)
        with pytest.raises(ImportError, match=r"earthlens\[sentinel-hub\]"):
            import_sentinelhub()


class TestDateValidation:
    """Missing dates are rejected with an actionable message."""

    def test_missing_start_rejected(self, output_dir: Path):
        """A `None` start date raises a clear ValueError."""
        with pytest.raises(ValueError, match="both start and end"):
            SentinelHub(
                start=None,
                end="2020-06-02",
                variables={"sentinel-2-l2a-ndvi": []},
                lat_lim=[40.0, 40.1],
                lon_lim=[14.0, 14.1],
                path=output_dir,
            )


class TestCatalogLoader:
    """Catalog loader error + edge paths."""

    def test_missing_path_rejected(self, tmp_path: Path):
        """Loading a non-existent catalog path raises."""
        clear_catalog_cache()
        with pytest.raises(ValueError, match="does not exist"):
            Catalog.load(tmp_path / "nope")

    def test_single_file_path(self, tmp_path: Path):
        """A single YAML file is a valid catalog source."""
        clear_catalog_cache()
        single = tmp_path / "one.yaml"
        single.write_text(
            "collections:\n  c:\n    sh_collection: SENTINEL2_L2A\n", encoding="utf-8"
        )
        cat = Catalog.load(single)
        assert cat.get_collection("c").sh_collection == "SENTINEL2_L2A"

    def test_empty_catalog_rejected(self, tmp_path: Path):
        """A YAML with no collections/recipes raises."""
        clear_catalog_cache()
        empty = tmp_path / "empty.yaml"
        empty.write_text("other: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no 'collections:' or 'recipes:'"):
            Catalog.load(empty)

    def test_duplicate_collection_key_rejected(self, tmp_path: Path):
        """The same collection key in two files is a load-time error."""
        clear_catalog_cache()
        (tmp_path / "a.yaml").write_text(
            "collections:\n  dup:\n    sh_collection: SENTINEL2_L1C\n", encoding="utf-8"
        )
        (tmp_path / "b.yaml").write_text(
            "collections:\n  dup:\n    sh_collection: SENTINEL2_L2A\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="declared in two catalog files"):
            Catalog.load(tmp_path)

    def test_invalid_collection_row_rejected(self, tmp_path: Path):
        """A collection row missing its required field raises a clear error."""
        clear_catalog_cache()
        bad = tmp_path / "bad.yaml"
        bad.write_text("collections:\n  c:\n    description: no id\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid collection"):
            Catalog.load(bad)

    def test_get_recipe_did_you_mean(self):
        """An unknown recipe key suggests the closest match."""
        clear_catalog_cache()
        with pytest.raises(ValueError, match="Did you mean"):
            Catalog().get_recipe("sentinel-2-l2a-ndv")


class TestCustomEvalscriptFile:
    """A custom `evalscript=` may be a `.js` file path."""

    def test_evalscript_from_file(self, fake_sh, tmp_path: Path, output_dir: Path):
        """A `.js` file path passed as evalscript= is read from disk."""
        js = tmp_path / "custom.js"
        js.write_text("//VERSION=3\nfunction setup(){return {};}", encoding="utf-8")
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=output_dir,
            api="process",
            evalscript=str(js),
            client_id="a",
            client_secret="b",
        )
        backend.download()
        req = fake_sh.SentinelHubRequest.instances[-1]
        assert "function setup" in req.evalscript


class TestApiHook:
    """The `_api` hook composes search/fetch."""

    def test_api_renders(self, fake_sh, output_dir: Path):
        """Calling `_api()` directly renders via the resolved plane."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=output_dir,
            client_id="a",
            client_secret="b",
        )
        assert len(backend._api()) == 1
