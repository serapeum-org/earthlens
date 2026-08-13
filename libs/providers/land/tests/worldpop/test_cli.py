"""Tests for the WorldPop catalog-tooling handlers (`earthlens.worldpop.cli`).

Moved out of core's CLI test suite when the WorldPop handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
import yaml

import earthlens.worldpop.cli as worldpop_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one
from earthlens.worldpop import catalog as worldpop_catalog

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the worldpop backend."""
    return next(b for b in list_backends() if b.provider == "worldpop")


class TestRefresher:
    """Tests for the WorldPop (REST sub-alias crawl) lister."""

    def test_crawls_aliases_to_subaliases(self, monkeypatch):
        """worldpop refresh crawls top aliases then each alias's sub-aliases."""

        def fake(url, **kw):
            if url.endswith("/rest/data"):
                return {"data": [{"alias": "pop"}, {"alias": "births"}]}
            return {"data": [{"alias": "wpgp"}, {"alias": "G2_BUILT_S"}]}

        monkeypatch.setattr(worldpop_cli, "get_json", fake)
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "worldpop refresh ran"
        assert outcome.live_count == 2, "deduped sub-alias ids across aliases"

    def test_grouped_crawls(self, monkeypatch):
        """worldpop grouped crawls the top alias then each alias's sub-aliases."""

        def fake(url, **kw):
            if url.rsplit("/", 1)[-1] == "data":
                return {"data": [{"alias": "pop"}]}
            return {"data": [{"alias": "wpgp"}, {"alias": "G2"}]}

        monkeypatch.setattr(worldpop_cli, "get_json", fake)
        assert worldpop_cli.refresher(None) == {"pop": ["G2", "wpgp"]}

    def test_curated_ids(self):
        """curated_ids flattens each record's sub-alias ids."""
        cat = SimpleNamespace(
            datasets={"pop": SimpleNamespace(subaliases=[SimpleNamespace(id="wpgp")])}
        )
        assert worldpop_cli.curated_ids(cat) == ["wpgp"]


class TestWriter:
    """Tests for the sibling available-products writer."""

    def test_writes_available_products_sibling(self, tmp_path, monkeypatch):
        """worldpop --write persists the grouped crawl to a sibling YAML."""
        dst = tmp_path / worldpop_catalog.CATALOG_PATH.name
        shutil.copy(worldpop_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(worldpop_catalog, "CATALOG_PATH", dst)
        path = worldpop_cli.writer(_info(), {"pop": ["wpgp", "wpgp1km"]})
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        assert path.endswith("available_products.yaml"), "sibling written"
        assert data["available_products"]["pop"] == ["wpgp", "wpgp1km"], "crawl kept"


class TestProber:
    """Tests for the WorldPop REST prober (public)."""

    def test_samples_record_fields(self, monkeypatch):
        """worldpop probe records each REST record field's dtype + popyears."""
        monkeypatch.setattr(
            worldpop_cli,
            "_records",
            lambda alias, sub, iso3: [
                {"id": 1, "title": "t", "popyear": "2020"},
                {"id": 2, "popyear": "2021"},
            ],
        )
        alias = next(iter(load_catalog(_info()).datasets))
        result = probe_dataset(_info(), alias)
        assert result.status == "ok", "worldpop probe ran"
        assert result.assets["popyears"]["values"] == ["2020", "2021"], "years unioned"

    def test_resolves_subalias_to_parent(self):
        """A sub-alias id resolves to its (parent_alias, sub_alias)."""
        catalog = load_catalog(_info())
        alias, row = next(
            (a, r)
            for a, r in catalog.datasets.items()
            if getattr(r, "subaliases", None)
        )
        sub_id = row.subaliases[0].id
        assert worldpop_cli._resolve(catalog, sub_id) == (alias, sub_id)

    def test_unknown_dataset_raises(self):
        """A dataset matching no product or sub-alias raises ValueError."""
        with pytest.raises(ValueError, match="no WorldPop"):
            worldpop_cli._resolve(load_catalog(_info()), "nope")

    def test_records_helper_delegates(self, monkeypatch):
        """_records delegates to the package rest_records."""
        import earthlens.worldpop.rest as rest

        monkeypatch.setattr(rest, "rest_records", lambda a, s, i: [{"id": 1}])
        assert worldpop_cli._records("a", "s", "USA") == [{"id": 1}]


class TestValidator:
    """Tests for the structural validator."""

    def test_validates_clean(self):
        """The shipped WorldPop catalog lints clean."""
        result = validate_one(_info())
        assert result.status == "ok" and result.issues == [], f"issues: {result.issues}"
        assert result.checked > 0, "products were checked"
