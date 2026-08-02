"""Unit tests for `earthlens.cli._ecmwf_hydrate` (CDS retrieve mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from earthlens.cli import _ecmwf_hydrate as hydrate_mod
from earthlens.cli._ecmwf_hydrate import (
    _find_file_for_dataset,
    _match_variables,
    _rewrite_stanza,
    _yaml_value,
    bulk_hydrate_empty,
)

pytestmark = pytest.mark.cli


_STANZA = """datasets:
  reanalysis-era5-single-levels:
    endpoint: cds
    request_kind: form
    variables:
      2m-temperature:
        cds_variable: 2m_temperature
        nc_variable: 2m_temperature
        units: unknown
      sea-surface-temperature:
        cds_variable: sea_surface_temperature
        nc_variable: sea_surface_temperature
        units: unknown
  other-dataset:
    endpoint: cds
    request_kind: form
    variables:
      total-precipitation:
        cds_variable: total_precipitation
        nc_variable: tp
        units: m
"""

_NC_META = {
    "latitude": {"long_name": "latitude", "units": "degrees_north"},
    "sst": {"long_name": "Sea surface temperature", "units": "K"},
    "t2m": {"long_name": "2 metre temperature", "units": "K"},
}


class TestRewriteStanza:
    """Tests for the pure stanza-rewriting core."""

    def test_fills_placeholders_long_name_then_order(self):
        """A long-name hit fills SST; the leftover t2m fills by order."""
        out = _rewrite_stanza(_STANZA, "reanalysis-era5-single-levels", _NC_META)
        variables = yaml.safe_load(out)["datasets"]["reanalysis-era5-single-levels"][
            "variables"
        ]
        assert variables["sea-surface-temperature"]["nc_variable"] == "sst"
        assert variables["sea-surface-temperature"]["units"] == "K"
        assert variables["2m-temperature"]["nc_variable"] == "t2m"
        assert variables["2m-temperature"]["units"] == "K"

    def test_leaves_other_stanza_untouched(self):
        """A sibling dataset's already-hydrated variable is preserved."""
        out = _rewrite_stanza(_STANZA, "reanalysis-era5-single-levels", _NC_META)
        other = yaml.safe_load(out)["datasets"]["other-dataset"]["variables"]
        assert other["total-precipitation"]["nc_variable"] == "tp"
        assert other["total-precipitation"]["units"] == "m"

    def test_missing_stanza_returns_input(self):
        """A dataset id not present returns the text unchanged."""
        assert _rewrite_stanza(_STANZA, "not-a-dataset", _NC_META) == _STANZA

    def test_no_placeholders_returns_input(self):
        """A stanza with no `units: unknown` variable is left unchanged."""
        assert _rewrite_stanza(_STANZA, "other-dataset", _NC_META) == _STANZA

    def test_empty_retrieve_returns_input(self):
        """No usable retrieved variable leaves the placeholders as-is."""
        assert _rewrite_stanza(_STANZA, "reanalysis-era5-single-levels", {}) == _STANZA


class TestMatchVariables:
    """Tests for the best-effort placeholder->variable matcher."""

    def test_long_name_subset_matches(self):
        """A slug whose tokens are a subset of the long name is matched."""
        assignments = _match_variables(
            ["sea-surface-temperature"],
            {"sst": {"long_name": "Sea surface temperature", "units": "K"}},
        )
        assert assignments == {"sea-surface-temperature": ("sst", "K")}

    def test_coordinates_are_never_matched(self):
        """Coordinate variables are dropped before matching."""
        assignments = _match_variables(
            ["some-var"],
            {"latitude": {"long_name": "latitude", "units": "degrees_north"}},
        )
        assert assignments == {}

    def test_order_fallback_pairs_leftovers(self):
        """One unmatched slug + one leftover variable is the unambiguous 1:1 case."""
        assignments = _match_variables(
            ["mystery-variable"],
            {"xx": {"long_name": "totally different", "units": "1"}},
        )
        assert assignments == {"mystery-variable": ("xx", "1")}

    def test_exact_short_name_never_swaps(self):
        """Multiple data vars map by exact short name, never zipped/swapped."""
        assignments = _match_variables(
            ["co2", "xco2"],
            {
                "xco2": {"long_name": "column CO2", "units": "ppm"},
                "co2": {"long_name": "CO2", "units": "ppm"},
                "lat_bnds": {"long_name": "", "units": "degrees_north"},
            },
        )
        assert assignments == {"co2": ("co2", "ppm"), "xco2": ("xco2", "ppm")}

    def test_bounds_variables_are_never_matched(self):
        """A `*_bnds` cell-bounds variable is excluded from matching (H1)."""
        assignments = _match_variables(
            ["co2"],
            {"lat_bnds": {"long_name": "", "units": "degrees_north"}},
        )
        assert assignments == {}, "co2 must not be mapped to lat_bnds"

    def test_specific_slug_wins_over_generic(self):
        """The more specific slug claims its variable; the generic gets the rest."""
        assignments = _match_variables(
            ["temperature", "sea-surface-temperature"],
            {
                "sst": {"long_name": "sea surface temperature", "units": "K"},
                "t": {"long_name": "temperature", "units": "K"},
            },
        )
        assert assignments == {
            "sea-surface-temperature": ("sst", "K"),
            "temperature": ("t", "K"),
        }

    def test_ambiguous_multi_placeholder_is_not_guessed(self):
        """Two unmatched slugs + two unnamed vars are left unhydrated, not zipped."""
        assignments = _match_variables(
            ["alpha", "beta"],
            {
                "v1": {"long_name": "", "units": "1"},
                "v2": {"long_name": "", "units": "1"},
            },
        )
        assert assignments == {}, "ambiguous slugs must keep their placeholders"


class TestYamlValue:
    """Tests for the scalar renderer."""

    @pytest.mark.parametrize(
        "value, expected",
        [("K", "K"), ("m3 m-3", "m3 m-3"), ("", "''"), ("1", "'1'")],
    )
    def test_quotes_only_when_needed(self, value, expected):
        """Plain scalars stay unquoted; ambiguous ones are quoted like YAML.

        Args:
            value: The raw units string.
            expected: How it should render after `units: `.
        """
        assert _yaml_value(value) == expected


class TestFindFileForDataset:
    """Tests for locating the per-family shard holding a stanza."""

    def test_finds_the_owning_shard(self, tmp_path):
        """The shard whose body has the dataset head is returned; _index skipped."""
        (tmp_path / "_index.yaml").write_text("available_datasets: []\n")
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        found = _find_file_for_dataset(tmp_path, "reanalysis-era5-single-levels")
        assert found.name == "era5.yaml"

    def test_returns_none_when_absent(self, tmp_path):
        """A dataset in no shard returns None."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        assert _find_file_for_dataset(tmp_path, "not-here") is None


def _patch_catalog(monkeypatch, tmp_path, datasets):
    """Redirect the ecmwf catalog at a fake datasets map + tmp shard dir."""
    import earthlens.ecmwf as ecmwf
    import earthlens.ecmwf.catalog as ecmwf_catalog

    fake = SimpleNamespace(datasets=datasets)
    monkeypatch.setattr(ecmwf, "Catalog", lambda: fake)
    monkeypatch.setattr(ecmwf_catalog, "CATALOG_PATH", tmp_path)
    monkeypatch.setattr(ecmwf_catalog, "clear_catalog_cache", lambda: None)


def _placeholder_dataset(*slugs):
    """A fake Dataset whose named variables all carry the `unknown` sentinel."""
    return SimpleNamespace(
        variables={slug: SimpleNamespace(units="unknown") for slug in slugs}
    )


class TestBulkHydrateEmpty:
    """Tests for the catalog-wide hydrate driver (retrieve + catalog mocked)."""

    def test_fills_every_placeholder_in_place(self, tmp_path, monkeypatch):
        """Each placeholder dataset is hydrated and written back to its shard."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {
                "reanalysis-era5-single-levels": _placeholder_dataset(
                    "2m-temperature", "sea-surface-temperature"
                ),
                "other-dataset": SimpleNamespace(
                    variables={"total-precipitation": SimpleNamespace(units="m")}
                ),
            },
        )
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_netcdf_vars", lambda ds: dict(_NC_META)
        )

        summary = bulk_hydrate_empty()
        assert summary == {
            "candidates": 1,
            "hydrated": 1,
            "skipped": 0,
            "filled": ["reanalysis-era5-single-levels"],
        }
        variables = yaml.safe_load((tmp_path / "era5.yaml").read_text())["datasets"][
            "reanalysis-era5-single-levels"
        ]["variables"]
        assert variables["2m-temperature"]["nc_variable"] == "t2m", "hydrated on disk"

    def test_skips_on_retrieve_failure(self, tmp_path, monkeypatch):
        """A dataset whose retrieve raises is skipped, not fatal."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )

        def _boom(dataset_id):
            raise RuntimeError("licence not accepted")

        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", _boom)
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 0 and summary["skipped"] == 1

    def test_limit_caps_candidates(self, tmp_path, monkeypatch):
        """A --limit truncates the placeholder worklist."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {
                "reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature"),
                "other-dataset": _placeholder_dataset("total-precipitation"),
            },
        )
        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", lambda ds: {})
        summary = bulk_hydrate_empty(limit=1)
        assert summary["candidates"] == 1, "limit applied to the worklist"
