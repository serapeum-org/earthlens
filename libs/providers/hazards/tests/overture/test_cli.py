"""Tests for the Overture catalog-tooling handlers (`earthlens.overture.cli`).

These moved out of core's CLI test suite when the Overture refresh / probe /
validate handlers moved into this distribution (issue #863); they exercise the
handlers directly and through core's command entry points.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
import yaml

import earthlens.overture.cli as overture_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one
from earthlens.cli.validate import validate_one
from earthlens.overture import catalog as overture_catalog
from earthlens.overture.releases import ReleaseLookupError

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the overture backend."""
    return next(b for b in list_backends() if b.provider == "overture")


def _unreachable_stac() -> list[str]:
    """Stand in for a child-link read that cannot reach the STAC catalog."""
    raise ReleaseLookupError("could not read Overture's STAC catalog (no route)")


class TestRefresher:
    """Tests for the Overture releases lister."""

    def test_release_ids_unwrap_tuple(self, monkeypatch):
        """release ids unwrap the (releases, latest) tuple and sort them."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["2026-07-22.0", "2026-06-17.0"], "2026-07-22.0"),
        )
        assert overture_cli._release_ids() == ["2026-06-17.0", "2026-07-22.0"]
        assert overture_cli.refresher(None) == {
            "overture": ["2026-06-17.0", "2026-07-22.0"]
        }, "the refresher reports what the lister found, already sorted"

    def test_release_ids_drop_unparsed_hrefs(self, monkeypatch):
        """Ids that are not shaped like a release never reach the index."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["https:", "https:"], "2026-07-22.0"),
        )
        monkeypatch.setattr("earthlens.overture.releases.child_release_ids", list)
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_release_ids_survive_an_unreachable_recovery(self, monkeypatch):
        """A recovery that cannot reach the catalog keeps whatever did parse."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["https:", "2026-07-22.0"], "2026-07-22.0"),
        )
        monkeypatch.setattr(
            "earthlens.overture.releases.child_release_ids", _unreachable_stac
        )
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_release_ids_recover_the_list_from_the_child_links(self, monkeypatch):
        """Unparsed ids are re-read from the STAC catalog's child links."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["https:", "https:"], "2026-07-22.0"),
        )
        monkeypatch.setattr(
            "earthlens.overture.releases.child_release_ids",
            lambda: ["2026-07-22.0", "2026-06-17.0"],
        )
        assert overture_cli._release_ids() == ["2026-06-17.0", "2026-07-22.0"], (
            "the release the SDK could not parse must still be indexed"
        )

    def test_release_ids_raise_rather_than_blank_the_index(self, monkeypatch):
        """Nothing parseable upstream is an error, not an empty index."""
        import overturemaps.core as core

        monkeypatch.setattr(core, "get_available_releases", lambda: (["https:"], None))
        monkeypatch.setattr("earthlens.overture.releases.child_release_ids", list)
        with pytest.raises(ReleaseLookupError, match=r"offline fallback"):
            overture_cli._release_ids()

    def test_release_ids_recover_when_the_sdk_lists_nothing(self, monkeypatch):
        """An empty SDK list triggers recovery too, not just a malformed one."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core, "get_available_releases", lambda: ([], "2026-07-22.0")
        )
        monkeypatch.setattr(
            "earthlens.overture.releases.child_release_ids",
            lambda: ["2026-07-22.0", "2026-06-17.0"],
        )
        assert overture_cli._release_ids() == ["2026-06-17.0", "2026-07-22.0"], (
            "a short list leaves the index as wrong as a mangled one"
        )

    def test_release_ids_keep_latest_when_recovery_finds_nothing(self, monkeypatch):
        """The latest release still lands when recovery turns up empty."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core, "get_available_releases", lambda: ([], "2026-07-22.0")
        )
        monkeypatch.setattr("earthlens.overture.releases.child_release_ids", list)
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_release_ids_tolerate_a_bare_list(self, monkeypatch):
        """A non-tuple return is treated as the release list alone."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core, "get_available_releases", lambda: ["2026-07-22.0", "junk"]
        )
        monkeypatch.setattr("earthlens.overture.releases.child_release_ids", list)
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_release_ids_without_a_latest(self, monkeypatch):
        """A `None` latest is skipped rather than indexed as a release."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core, "get_available_releases", lambda: (["2026-07-22.0"], None)
        )
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_release_ids_deduplicate_the_latest(self, monkeypatch):
        """A latest already present in the list is not indexed twice."""
        import overturemaps.core as core

        monkeypatch.setattr(
            core,
            "get_available_releases",
            lambda: (["2026-07-22.0", "2026-07-22.0"], "2026-07-22.0"),
        )
        assert overture_cli._release_ids() == ["2026-07-22.0"]

    def test_diffs_releases_not_feature_types(self, monkeypatch):
        """overture diffs the live releases against available_releases."""
        monkeypatch.setattr(
            overture_cli,
            "_release_ids",
            lambda: ["2099-01-01.0", "2026-05-20.0"],
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "overture refresh ran"
        assert "2099-01-01.0" in outcome.new_ids, "a new release is flagged"
        assert all("-" in rid for rid in outcome.removed_ids), "diffed vs releases"


class TestCuratedIds:
    """Tests for the curated-release axis."""

    def test_returns_sorted_releases(self):
        """curated_ids returns the catalog's sorted release ids."""
        catalog = SimpleNamespace(available_releases=["b", "a"])
        assert overture_cli.curated_ids(catalog) == ["a", "b"]


class TestWriter:
    """Tests for the `--write` release-index writer."""

    def test_writes_releases_keeps_feature_types(self, tmp_path, monkeypatch):
        """overture --write rewrites available_releases, keeping the type set."""
        dst = tmp_path / overture_catalog.CATALOG_PATH.name
        shutil.copy(overture_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(overture_catalog, "CATALOG_PATH", dst)
        before = yaml.safe_load(dst.read_text("utf-8"))

        overture_cli.writer(_info(), {"overture": ["2099-01-01.0"]})

        after = yaml.safe_load(dst.read_text("utf-8"))
        assert after["available_releases"] == ["2099-01-01.0"], "releases rewritten"
        assert after["available_datasets"] == before["available_datasets"], "types kept"


class TestProber:
    """Tests for the Overture column prober (public SDK)."""

    def test_reads_column_dtypes(self, monkeypatch):
        """overture probe records each column's dtype from a tiny bbox."""
        monkeypatch.setattr(
            overture_cli,
            "_columns",
            lambda overture_type: {"id": "object", "height": "float64"},
        )
        info = _info()
        key = next(iter(load_catalog(info).datasets))
        result = probe_dataset(info, key)
        assert result.status == "ok", "overture probe ran"
        assert result.assets["height"]["dtype"] == "float64", "dtype recorded"

    def test_columns_helper_reads_dtypes(self, monkeypatch):
        """_columns maps a geodataframe's dtypes to {name: str(dtype)}."""
        import overturemaps.core as core

        class FakeFrame:
            dtypes = {"id": "int64", "geometry": "geometry"}

        monkeypatch.setattr(core, "geodataframe", lambda t, bbox: FakeFrame())
        out = overture_cli._columns("building")
        assert out == {"id": "int64", "geometry": "geometry"}, "dtypes stringified"


class TestValidator:
    """Tests for the structural and live validators."""

    def test_default_type_must_be_in_types(self):
        """A default_type not among types is flagged."""
        catalog = SimpleNamespace(
            datasets={"x": SimpleNamespace(types=["a", "b"], default_type="c")}
        )
        _checked, issues = overture_cli.validator(catalog)
        assert any("default_type" in i for i in issues), "mismatch flagged"

    def test_live_sample_reports_sources(self, monkeypatch):
        """_live_sample returns (row_count, has_sources_column)."""
        import overturemaps.core as core

        class FakeFrame:
            columns = ["id", "sources"]

            def __len__(self):
                return 2

        monkeypatch.setattr(core, "geodataframe", lambda t, bbox: FakeFrame())
        rows, has_sources = overture_cli._live_sample("building")
        assert rows == 2, "rows + sources column reported"
        assert has_sources is True, "rows + sources column reported"

    def test_live_flags_missing_sources(self, monkeypatch):
        """An Overture type without a sources column is flagged live."""
        monkeypatch.setattr(overture_cli, "_live_sample", lambda t: (0, False))
        result = validate_one(_info(), live=True)
        assert any("sources" in i for i in result.issues), "missing sources flagged"

    def test_live_reports_fetch_failure(self, monkeypatch):
        """An Overture type whose fetch raises is reported, not raised."""

        def boom(t):
            raise RuntimeError("network")

        monkeypatch.setattr(overture_cli, "_live_sample", boom)
        result = validate_one(_info(), live=True)
        assert any("fetch failed" in i for i in result.issues), "fetch failure reported"

    def test_live_reports_nothing_when_every_type_resolves(self, monkeypatch):
        """A clean live validation walks every curated theme and flags none."""
        sampled: list[str] = []
        monkeypatch.setattr(
            overture_cli, "_live_sample", lambda t: (sampled.append(t), (3, True))[1]
        )
        result = validate_one(_info(), live=True)
        assert not result.issues, (
            f"expected a clean live validation, got {result.issues}"
        )
        catalog = load_catalog(_info())
        assert sorted(sampled) == sorted(
            getattr(r, "default_type", None) or k for k, r in catalog.datasets.items()
        ), "every curated theme's default type should have been sampled"
