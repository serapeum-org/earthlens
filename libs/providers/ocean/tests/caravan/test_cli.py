"""Tests for the Caravan catalog-tooling handlers (`earthlens.caravan.cli`).

Moved out of core's CLI test suite when the Caravan refresh + validate handlers
moved into this distribution (issue #863).
"""

from __future__ import annotations

import pytest

import earthlens.caravan.cli as caravan_cli

pytestmark = pytest.mark.cli


class TestRefresher:
    """The Caravan drift refresher, which watches pinned Zenodo records."""

    def _catalog(self):
        """Build a Caravan catalog with one pinned release and one exclusion."""
        from earthlens.caravan.catalog import (
            ArchiveFile,
            Catalog,
            Extension,
            Version,
        )

        release = Version(
            doi="10.5281/zenodo.1",
            release_date="2025-01-01",
            files={
                "csv": ArchiveFile(
                    record=111, name="a.zip", size=1, md5="x", archive_format="zip"
                )
            },
        )
        return Catalog(
            datasets={
                "demo": Extension(
                    key="demo",
                    concept_doi="10.5281/zenodo.999",
                    default_version="1.0",
                    versions={"1.0": release},
                )
            },
            variables={},
            extension_index=[
                {"key": "demo", "supported": True},
                {"key": "multimet", "supported": False, "records": [777]},
            ],
        )

    def _patch(self, monkeypatch, versions, search):
        """Route the refresher's two endpoint shapes to canned payloads."""

        def _fake(url, **kwargs):
            return versions if "/versions" in url else search

        monkeypatch.setattr(caravan_cli, "get_json", _fake)

    def test_a_newer_release_is_reported_as_drift(self, monkeypatch):
        """A release published after the pin is exactly what this watches for."""
        self._patch(
            monkeypatch,
            {
                "hits": {
                    "hits": [
                        {"id": 222, "metadata": {"publication_date": "2026-01-01"}}
                    ]
                }
            },
            {"hits": {"hits": []}},
        )

        grouped = caravan_cli.refresher(self._catalog())

        assert grouped["demo"] == ["222 (2026-01-01)"]

    def test_an_older_release_is_not_drift(self, monkeypatch):
        """Earlier links in the version chain are history, not news."""
        self._patch(
            monkeypatch,
            {
                "hits": {
                    "hits": [{"id": 99, "metadata": {"publication_date": "2020-01-01"}}]
                }
            },
            {"hits": {"hits": []}},
        )

        assert caravan_cli.refresher(self._catalog())["demo"] == []

    def test_a_deliberately_unsupported_record_is_not_discovered(self, monkeypatch):
        """Reporting a known exclusion every run trains the reader to ignore it."""
        hit = {
            "id": 777,
            "conceptrecid": "776",
            "metadata": {
                "title": "Caravan MultiMet",
                "keywords": ["hydrology", "streamflow"],
            },
        }
        self._patch(monkeypatch, {"hits": {"hits": []}}, {"hits": {"hits": [hit]}})

        assert caravan_cli._is_hydrological(hit), "the fixture must reach the filter"
        assert caravan_cli.refresher(self._catalog())["discovered"] == []

    def test_the_suppression_is_what_keeps_it_out(self, monkeypatch):
        """An unsuppressed hit must surface, or the test above proves nothing."""
        hit = {
            "id": 999,
            "conceptrecid": "998",
            "metadata": {"title": "Caravan MultiMet", "keywords": ["hydrology"]},
        }
        self._patch(monkeypatch, {"hits": {"hits": []}}, {"hits": {"hits": [hit]}})

        discovered = caravan_cli.refresher(self._catalog())["discovered"]

        assert discovered == ["999 (Caravan MultiMet)"]

    def test_an_unknown_record_is_discovered(self, monkeypatch):
        """A new community extension appears as its own record, not in any chain."""
        self._patch(
            monkeypatch,
            {"hits": {"hits": []}},
            {
                "hits": {
                    "hits": [
                        {
                            "id": 555,
                            "conceptrecid": "554",
                            "metadata": {
                                "title": "Caravan extension Narnia",
                                "keywords": ["hydrology"],
                            },
                        }
                    ]
                }
            },
        )

        assert caravan_cli.refresher(self._catalog())["discovered"] == [
            "555 (Caravan extension Narnia)"
        ]

    def test_a_non_hydrological_hit_is_filtered_out(self):
        """Searching full-text for "caravan" also returns camel-trade papers."""
        hit = {
            "metadata": {
                "title": "Camels, donkeys and caravan trade in the Levant",
                "keywords": ["Biodiversity", "Taxonomy"],
            }
        }

        assert not caravan_cli._is_hydrological(hit)

    def test_a_hydrology_title_alone_is_enough(self):
        """Denmark's record carries no keywords at all, only a telling title."""
        hit = {
            "metadata": {
                "title": "Caravan extension Denmark - Danish dataset for "
                "large-sample hydrology",
                "keywords": [],
            }
        }

        assert caravan_cli._is_hydrological(hit)

    def test_a_hydrology_keyword_alone_is_enough(self):
        """The title carries no qualifying term, so only the keywords can match."""
        hit = {
            "metadata": {
                "title": "CAMELS-ES: Attributes and Meteorology for Spain",
                "keywords": ["CAMELS; CARAVAN; large sample hydrology; Spain"],
            }
        }

        assert caravan_cli._is_hydrological(hit)

    def test_the_bare_word_caravan_is_not_a_hydrology_signal(self):
        """ "caravan" and "camels" are both ambiguous, so neither qualifies alone."""
        hit = {
            "metadata": {"title": "A few camels or a whole caravan?", "keywords": []}
        }

        assert not caravan_cli._is_hydrological(hit)


class TestValidator:
    """The offline structural validator for the Caravan catalog."""

    def _row(self, **overrides):
        """Build one Caravan extension, overridable per test."""
        from earthlens.caravan.catalog import ArchiveFile, Extension, Source, Version

        version = Version(
            release_date="2025-01-01",
            data_period=overrides.pop("data_period", (2000, 2020)),
            column_set=overrides.pop("column_set", "current"),
            files={
                "csv": ArchiveFile(
                    record=overrides.pop("record", 1),
                    name="a.zip",
                    size=overrides.pop("size", 10),
                    md5=overrides.pop("md5", "abc"),
                    archive_format="zip",
                )
            },
        )
        return Extension(
            key="demo",
            license=overrides.pop("license", "CC-BY-4.0"),
            sources={"dk": Source(n_catchments=1)},
            default_version=overrides.pop("default_version", "1.0"),
            versions={"1.0": version},
        )

    def _validate(self, extension):
        """Run the validator over a one-row catalog."""
        from earthlens.caravan.catalog import Catalog

        return caravan_cli.validator(
            Catalog(datasets={"demo": extension}, variables={})
        )

    def test_a_well_formed_row_passes(self):
        """The bundled shape must not trip its own validator."""
        checked, issues = self._validate(self._row())

        assert (checked, issues) == (1, [])

    def test_an_unpinned_record_is_reported(self):
        """Reproducibility is the catalog's whole job."""
        _, issues = self._validate(self._row(record=0))

        assert any("no pinned Zenodo record" in issue for issue in issues)

    def test_a_missing_checksum_is_reported(self):
        """Without an md5 a download cannot be verified."""
        _, issues = self._validate(self._row(md5=""))

        assert any("no md5" in issue for issue in issues)

    def test_an_inverted_data_period_is_reported(self):
        """A reversed period would silently mislead every user of the row."""
        _, issues = self._validate(self._row(data_period=(2020, 1990)))

        assert any("inverted" in issue for issue in issues)

    def test_a_default_version_that_does_not_exist_is_reported(self):
        """A bare request resolves through default_version, so it must exist."""
        _, issues = self._validate(self._row(default_version="nope"))

        assert any("default_version" in issue for issue in issues)

    def test_the_bundled_catalog_validates_clean(self):
        """The shipped catalog must pass its own checks."""
        from earthlens.caravan import Catalog

        checked, issues = caravan_cli.validator(Catalog())

        assert issues == []
        assert checked == 7


class TestDiscoveryFilterEdges:
    """Titles the filter must accept even though they read as non-hydrological."""

    def test_an_extension_named_only_for_its_region_is_kept(self):
        """ "Caravan extension Iceland" carries no hydrology word at all."""
        hit = {"metadata": {"title": "Caravan extension Iceland", "keywords": []}}

        assert caravan_cli._is_hydrological(hit)

    def test_a_hyphenated_caravan_name_is_kept(self):
        """Caravan-AUS-VIC names a river, not a hydrological concept."""
        hit = {
            "metadata": {
                "title": "New dataset extension: Caravan-AUS-VIC (Maribyrnong "
                "River, Victoria, Australia)",
                "keywords": [],
            }
        }

        assert caravan_cli._is_hydrological(hit)
