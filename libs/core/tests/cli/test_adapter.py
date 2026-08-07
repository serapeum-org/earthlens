"""Unit tests for `earthlens.cli.adapter`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli import adapter
from earthlens.cli.adapter import (
    BackendInfo,
    LoadError,
    RawRow,
    _row_mapping,
    iter_catalog_rows,
    list_backends,
    load_all_rows,
    load_catalog,
    record_title,
)
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.cli


class TestListBackends:
    """Tests for list_backends."""

    def test_collapses_aliases_to_distinct_backends(self):
        """Every registry alias collapses to one BackendInfo per backend.

        The facade registry exposes 40+ keys (aliases like `chirps`,
        `google-earth-engine`, `protected-planet`); list_backends returns
        one entry per distinct backend module.
        """
        backends = list_backends()
        providers = [b.provider for b in backends]
        assert len(providers) == len(set(providers)), "provider ids must be unique"
        assert len(backends) == 51, f"expected 51 backends, got {len(backends)}"

    def test_sorted_by_provider(self):
        """Backends are returned sorted by canonical provider id."""
        providers = [b.provider for b in list_backends()]
        assert providers == sorted(providers), "backends should be sorted by provider"

    def test_provider_id_is_subpackage_name(self):
        """The provider id is the backend's subpackage name."""
        by_provider = {b.provider: b for b in list_backends()}
        assert by_provider["chc"].module == "earthlens.chc"
        assert by_provider["usgs_water"].module == "earthlens.usgs_water"

    def test_aliases_grouped_under_backend(self):
        """All facade keys pointing at a backend land in its aliases tuple."""
        by_provider = {b.provider: b for b in list_backends()}
        assert "chirps" in by_provider["chc"].aliases, "chc keeps the chirps alias"
        assert "amazon-s3" in by_provider["s3"].aliases, "s3 keeps amazon-s3 alias"
        assert "google-earth-engine" in by_provider["gee"].aliases

    def test_extra_hint_captured(self):
        """The pip extra is captured for SDK backends and empty otherwise."""
        by_provider = {b.provider: b for b in list_backends()}
        assert by_provider["gee"].extra == "gee", "gee needs its SDK extra"
        assert by_provider["chc"].extra == "", "chc is SDK-free (anonymous FTP)"


class TestRegistryEntries:
    """Tests for EarthLens.DataSources.entries (consumed by list_backends)."""

    def test_yields_key_module_extra_triples(self):
        """entries() exposes each key's backing module and pip extra."""
        entries = {
            key: (module, extra)
            for key, module, extra in EarthLens.DataSources.entries()
        }
        assert entries["chc"] == ("earthlens.chc", ""), "SDK-free backend"
        assert entries["gee"] == ("earthlens.gee", "gee"), "extra captured"

    def test_one_entry_per_registry_key(self):
        """entries() yields exactly one triple per registered key."""
        entries = list(EarthLens.DataSources.entries())
        assert len(entries) == len(EarthLens.DataSources), "one per key"


class TestRowMapping:
    """Tests for the _row_mapping divergence absorber."""

    def test_prefers_datasets(self):
        """A populated `datasets` field is used directly."""
        catalog = SimpleNamespace(datasets={"a": 1}, parameters={"x": 2})
        assert _row_mapping(catalog) == {"a": 1}, "datasets wins"

    def test_falls_back_to_alternate_field(self):
        """An empty `datasets` falls back to the first populated alt field."""
        catalog = SimpleNamespace(datasets={}, parameters={"pm25": object()})
        assert set(_row_mapping(catalog)) == {"pm25"}, "parameters used"

    def test_empty_when_nothing_populated(self):
        """No populated row field yields an empty mapping."""
        assert _row_mapping(SimpleNamespace(datasets={})) == {}, "empty -> {}"


class TestRecordTitle:
    """Tests for record_title."""

    @pytest.mark.parametrize(
        "record, expected",
        [
            (SimpleNamespace(title="ERA5 hourly"), "ERA5 hourly"),
            (SimpleNamespace(long_name="2m temperature"), "2m temperature"),
            (SimpleNamespace(description="Sea ice"), "Sea ice"),
            (SimpleNamespace(name="pm25"), "pm25"),
            (SimpleNamespace(site_name="KTLX"), "KTLX"),
        ],
    )
    def test_each_label_field(self, record, expected):
        """Each supported label attribute is used when it is the only one.

        Args:
            record: A record exposing exactly one label attribute.
            expected: The label that should be returned.
        """
        assert record_title(record) == expected, f"should read {expected!r}"

    def test_fallback_order_prefers_title(self):
        """`title` wins over later fields when several are present."""
        record = SimpleNamespace(title="Primary", description="Secondary")
        assert record_title(record) == "Primary", "title outranks description"

    def test_blank_field_is_skipped(self):
        """A whitespace-only field is skipped in favour of the next one."""
        record = SimpleNamespace(title="   ", description="Real label")
        assert record_title(record) == "Real label", "blank title falls through"

    def test_no_label_returns_empty(self):
        """A record with no label attribute yields the empty string."""
        assert record_title(SimpleNamespace(bucket="x")) == "", "no label -> ''"


class TestLoadCatalog:
    """Tests for load_catalog."""

    def test_loads_chc_catalog(self):
        """Reflectively loading the CHC backend returns a populated catalog."""
        info = next(b for b in list_backends() if b.provider == "chc")
        catalog = load_catalog(info)
        assert len(catalog.datasets) > 0, "CHC catalog should have datasets"

    def test_radar_catalog_class_is_aliased(self):
        """Radar exports its `StationCatalog` as `Catalog`, so reflection works."""
        info = next(b for b in list_backends() if b.provider == "radar")
        catalog = load_catalog(info)
        assert len(catalog.datasets) > 0, "radar catalog should have stations"


class TestIterCatalogRows:
    """Tests for iter_catalog_rows."""

    def test_yields_normalised_rows(self):
        """Each yielded row carries the provider, a str id, and the record."""
        info = next(b for b in list_backends() if b.provider == "chc")
        catalog = load_catalog(info)
        rows = list(iter_catalog_rows(info, catalog))
        assert rows, "CHC should yield rows"
        first = rows[0]
        assert isinstance(first, RawRow), "yields RawRow instances"
        assert first.provider == "chc", "provider stamped from the backend"
        assert isinstance(first.dataset_id, str), "dataset_id coerced to str"
        assert first.record is not None, "the pydantic record is retained"

    def test_row_count_matches_catalog(self):
        """One row is yielded per curated dataset."""
        info = next(b for b in list_backends() if b.provider == "chc")
        catalog = load_catalog(info)
        rows = list(iter_catalog_rows(info, catalog))
        assert len(rows) == len(catalog.datasets), "one row per dataset"


class TestLoadAllRows:
    """Tests for load_all_rows."""

    def test_provider_scoped_load(self):
        """Restricting to one provider returns only that provider's rows."""
        rows, errors = load_all_rows(providers=["chc"])
        assert rows, "chc should produce rows"
        assert not errors, "a healthy backend reports no load errors"
        assert {r.provider for r in rows} == {"chc"}, "only chc rows returned"

    def test_alias_resolves_to_backend(self):
        """A registry alias selects the same backend as its canonical id."""
        rows, _ = load_all_rows(providers=["chirps"])
        assert {r.provider for r in rows} == {"chc"}, "chirps alias -> chc"

    def test_failure_is_isolated(self, monkeypatch):
        """A backend that fails to load is captured as a LoadError, not raised."""

        def _boom(info: BackendInfo):
            raise RuntimeError("simulated SDK failure")

        monkeypatch.setattr(adapter, "load_catalog", _boom)
        rows, errors = load_all_rows(providers=["chc"])
        assert rows == [], "no rows when the backend fails"
        assert len(errors) == 1, "exactly one backend failed"
        assert isinstance(errors[0], LoadError), "failure recorded as LoadError"
        assert "simulated SDK failure" in errors[0].error, "reason preserved"
