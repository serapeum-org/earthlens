"""Unit tests for `earthlens.ecmwf._hydrate` (CDS retrieve mocked)."""

from __future__ import annotations

import textwrap
import time
from types import SimpleNamespace

import pytest
import yaml

from earthlens.ecmwf import _hydrate as hydrate_mod
from earthlens.ecmwf._hydrate import (
    _claimed_nc_names,
    _find_file_for_dataset,
    _is_initialism,
    _match_variables,
    _pair_is_evidenced,
    _retrieve_with_timeout,
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

_TWO_STANZA = """datasets:
  a-dataset:
    endpoint: cds
    request_kind: form
    variables:
      sea-surface-temperature:
        cds_variable: sea_surface_temperature
        nc_variable: sea_surface_temperature
        units: unknown
  z-dataset:
    endpoint: cds
    request_kind: form
    variables:
      sea-surface-temperature:
        cds_variable: sea_surface_temperature
        nc_variable: sea_surface_temperature
        units: unknown
"""


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

    def test_a_sibling_row_keeps_its_nc_variable_to_itself(self):
        """A leftover slug is not handed a NetCDF name a hydrated row already claims."""
        text = textwrap.dedent(
            """
            datasets:
              demo-dataset:
                variables:
                  total-precipitation:
                    cds_variable: total_precipitation
                    nc_variable: tp
                    units: m
                  precipitation-rate:
                    cds_variable: precipitation_rate
                    nc_variable: precipitation_rate
                    units: unknown
            """
        ).lstrip()
        out = _rewrite_stanza(
            text,
            "demo-dataset",
            {"tp": {"long_name": "Total precipitation", "units": "m"}},
        )
        variables = yaml.safe_load(out)["datasets"]["demo-dataset"]["variables"]
        assert variables["precipitation-rate"]["units"] == "unknown"
        assert [v["nc_variable"] for v in variables.values()].count("tp") == 1

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

    def test_unrelated_leftovers_are_not_paired(self):
        """A lone slug and a lone variable that share no evidence keep the placeholder."""
        assignments = _match_variables(
            ["mystery-variable"],
            {"xx": {"long_name": "totally different", "units": "1"}},
        )
        assert assignments == {}

    def test_leftover_pairs_when_the_short_name_shares_a_token(self):
        """The 1:1 leftover case still resolves when the names agree."""
        assignments = _match_variables(
            ["wind-speed-of-gusts"],
            {"wind_speed_gust": {"long_name": "", "units": "m s-1"}},
        )
        assert assignments == {"wind-speed-of-gusts": ("wind_speed_gust", "m s-1")}

    @pytest.mark.parametrize(
        ("slug", "nc_name", "long_name", "expected"),
        [
            # True positives - abbreviations a token-overlap test would reject.
            ("2m-temperature", "t2m", "", True),
            ("sea-surface-temperature", "sst", "", True),
            ("total-precipitation", "tp", "", True),
            # True positives - a shared token with the short or long name.
            ("mean-uth", "uth", "", True),
            (
                "terrestrial-water-storage-anomaly",
                "lwe_thickness",
                "Liquid Water Equivalent Thickness",
                True,
            ),
            # True negatives - unrelated names must keep the placeholder.
            ("number-of-wet-days", "elevation", "", False),
            ("number-of-dry-spells", "elevation", "Surface elevation", False),
            ("glacier-area", "elevation", "", False),
            # A near-miss prefix is not an initialism.
            ("precipitation", "pressure", "", False),
        ],
    )
    def test_leftover_pairing_table(self, slug, nc_name, long_name, expected):
        """The lone-slug/lone-variable rule pairs only on real name evidence."""
        assignments = _match_variables(
            [slug], {nc_name: {"long_name": long_name, "units": "1"}}
        )
        assert bool(assignments) is expected
        if expected:
            assert assignments == {slug: (nc_name, "1")}

    def test_two_leftovers_are_never_paired(self):
        """The leftover rule needs exactly one of each, so a 2x2 residue stays unhydrated."""
        assignments = _match_variables(
            ["first-mystery", "second-mystery"],
            {
                "xx": {"long_name": "totally different", "units": "1"},
                "yy": {"long_name": "also unrelated", "units": "1"},
            },
        )
        assert assignments == {}

    def test_initialism_ignores_case(self):
        """An upper-case NetCDF short name is still recognised as an initialism."""
        assignments = _match_variables(
            ["sea-surface-temperature"], {"SST": {"long_name": "", "units": "K"}}
        )
        assert assignments == {"sea-surface-temperature": ("SST", "K")}

    def test_reserved_names_are_withheld_from_the_leftover_rule(self):
        """A reserved NetCDF name is not offered to the lone leftover slug."""
        meta = {"t2m": {"long_name": "", "units": "K"}}
        assert _match_variables(["2m-temperature"], meta) != {}
        assert (
            _match_variables(["2m-temperature"], meta, reserved=frozenset({"t2m"}))
            == {}
        )

    def test_reserved_names_still_reach_the_confident_rules(self):
        """Reservation gates only the leftover guess; a long-name match still binds."""
        meta = {"sst": {"long_name": "Sea surface temperature", "units": "K"}}
        assignments = _match_variables(
            ["sea-surface-temperature"], meta, reserved=frozenset({"sst"})
        )
        assert assignments == {"sea-surface-temperature": ("sst", "K")}

    def test_the_all_pseudo_slug_is_never_paired(self):
        """`all` means every variable, so it never stands in for one of them."""
        meta = {"tp": {"long_name": "Total precipitation", "units": "m"}}
        assert _match_variables(["total-precipitation"], meta) != {}, (
            "a real slug pairs with this lone variable"
        )
        assert _match_variables(["all"], meta) == {}, "the pseudo-slug must not"

    def test_stopword_only_slug_is_never_paired(self):
        """Stripping stopwords leaves nothing to match on, so the shared `of` is ignored."""
        assignments = _match_variables(
            ["of-the"], {"of": {"long_name": "totally different", "units": "1"}}
        )
        assert assignments == {}

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

    def test_non_bounds_aux_variable_is_never_matched(self):
        """A viewing-angle aux var is not the sole 1:1 candidate (SZA)."""
        assignments = _match_variables(
            ["cloud-phase"],
            {"SZA": {"long_name": "Solar zenith angle", "units": "degree"}},
        )
        assert assignments == {}, "cloud-phase must not be mapped to SZA"

    def test_count_aux_variable_is_not_zipped(self):
        """A leftover slug is not paired to an observation-count aux var (nobs)."""
        assignments = _match_variables(
            ["temperature", "quality-flag"],
            {
                "t": {"long_name": "temperature", "units": "K"},
                "nobs": {"long_name": "number of observations", "units": "1"},
            },
        )
        assert assignments == {"temperature": ("t", "K")}, (
            "quality-flag stays unhydrated"
        )


_CLAIMED_BLOCK = """      total-precipitation:
        cds_variable: total_precipitation
        nc_variable: tp  # ERA5 short name
        units: m
      quoted-row:
        cds_variable: x
        nc_variable: 'SST'
        units: K
      empty-row:
        cds_variable: y
        nc_variable: null
        units: K
      still-a-placeholder:
        cds_variable: z
        nc_variable: z
        units: unknown
"""


class TestClaimedNcNames:
    """Tests for the reservation set read out of a stanza's hydrated rows."""

    def test_an_inline_comment_does_not_become_part_of_the_name(self):
        """A trailing YAML comment is stripped, so the bare name is reserved."""
        assert "tp" in _claimed_nc_names(_CLAIMED_BLOCK)

    def test_a_quoted_value_is_unquoted_and_lowercased(self):
        """Quotes are dropped and case folded, so SST and sst reserve alike."""
        assert "sst" in _claimed_nc_names(_CLAIMED_BLOCK)

    def test_a_null_value_reserves_nothing(self):
        """A null nc_variable is not a claim on any name."""
        claimed = _claimed_nc_names(_CLAIMED_BLOCK)
        assert "null" not in claimed
        assert "" not in claimed

    def test_a_placeholder_row_claims_nothing(self):
        """A row still carrying the unknown sentinel has not bound its name yet."""
        assert "z" not in _claimed_nc_names(_CLAIMED_BLOCK)

    def test_an_empty_block_reserves_nothing(self):
        """A stanza with no variables reserves nothing."""
        assert _claimed_nc_names("") == frozenset()


class TestIsInitialism:
    """Tests for the order-free initialism predicate."""

    @pytest.mark.parametrize(
        ("name", "tokens", "expected"),
        [
            ("sst", {"sea", "surface", "temperature"}, True),
            ("t2m", {"2m", "temperature"}, True),
            ("tp", {"total", "precipitation"}, True),
            ("pressure", {"precipitation"}, False),
            ("elevation", {"number", "wet", "days"}, False),
            ("sst", {"sea", "surface"}, False),
        ],
    )
    def test_recognises_compressed_spellings(self, name, tokens, expected):
        """A name qualifies only when every token contributes and none is left over."""
        assert _is_initialism(name, tokens) is expected

    def test_no_tokens_is_never_an_initialism(self):
        """An empty token set carries nothing to compress."""
        assert _is_initialism("sst", set()) is False


class TestPairIsEvidenced:
    """Tests for the three arms of the rule 4 evidence check."""

    def test_short_name_token_overlap(self):
        """A token shared with the NetCDF short name is evidence."""
        assert _pair_is_evidenced("mean-uth", "uth", {}) is True

    def test_long_name_token_overlap(self):
        """A token shared with the variable's long name is evidence."""
        meta = {"long_name": "Liquid Water Equivalent Thickness"}
        assert _pair_is_evidenced("terrestrial-water-storage", "lwe_thickness", meta)

    def test_initialism(self):
        """An initialism is evidence with no shared token at all."""
        assert _pair_is_evidenced("sea-surface-temperature", "sst", {}) is True

    def test_a_generic_shared_token_counts_as_evidence(self):
        """One shared generic word satisfies the long-name arm, which is weak by design."""
        assert (
            _pair_is_evidenced(
                "land-sea-mask", "zzz", {"long_name": "Mean sea level pressure"}
            )
            is True
        ), "the only thing in common is the word 'sea'"

    def test_reservation_declines_a_name_a_sibling_row_owns(self):
        """Where a hydrated sibling owns the name, reservation is what stops rule 4."""
        meta = {"msl": {"long_name": "Mean sea level pressure", "units": "Pa"}}
        assert _match_variables(["land-sea-mask"], meta) != {}
        assert (
            _match_variables(["land-sea-mask"], meta, reserved=frozenset({"msl"})) == {}
        )

    @pytest.mark.parametrize(
        ("slug", "name"),
        [("co2", "co"), ("ethene", "e"), ("methane", "met")],
    )
    def test_a_single_word_slug_is_not_abbreviated_by_a_prefix(self, slug, name):
        """Compressing one word leaves only a prefix of it, which is not evidence."""
        assert _pair_is_evidenced(slug, name, {}) is False

    def test_unrelated_names_carry_no_evidence(self):
        """Two names with nothing in common are not paired."""
        assert _pair_is_evidenced("number-of-wet-days", "elevation", {}) is False

    def test_a_stopword_only_slug_carries_no_evidence(self):
        """A slug that reduces to stopwords has nothing left to match on."""
        assert _pair_is_evidenced("of-the", "xx", {"long_name": "of the"}) is False


class TestRetrieveWithTimeout:
    """Tests for the per-retrieve timeout wrapper."""

    def test_zero_timeout_calls_synchronously(self, monkeypatch):
        """A falsy timeout bypasses the thread and returns the retrieve verbatim."""
        meta = {"t2m": {"long_name": "2 metre temperature", "units": "K"}}
        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", lambda ds: meta)
        assert _retrieve_with_timeout("x", timeout=0) == meta

    def test_result_is_returned_within_deadline(self, monkeypatch):
        """A fast retrieve completes inside the deadline and its meta is returned."""
        meta = {"sst": {"long_name": "Sea surface temperature", "units": "K"}}
        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", lambda ds: meta)
        assert _retrieve_with_timeout("x", timeout=5) == meta

    def test_hung_retrieve_raises_timeout(self, monkeypatch):
        """A retrieve that outlasts the deadline is abandoned with TimeoutError."""
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_netcdf_vars", lambda ds: time.sleep(2) or {}
        )
        with pytest.raises(TimeoutError, match="exceeded"):
            _retrieve_with_timeout("stuck-dataset", timeout=0.05)

    def test_retrieve_error_is_reraised(self, monkeypatch):
        """An exception raised inside the worker thread surfaces to the caller."""

        def _boom(dataset_id):
            raise RuntimeError("licence not accepted")

        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", _boom)
        with pytest.raises(RuntimeError, match="licence not accepted"):
            _retrieve_with_timeout("x", timeout=5)


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
            "timed_out": 0,
            "unmatched": 0,
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
        assert summary["hydrated"] == 0
        assert summary["skipped"] == 1

    def test_a_declined_match_is_reported_apart_from_a_skip(
        self, tmp_path, monkeypatch
    ):
        """A retrieve that yields nothing confident is unmatched, not skipped."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(
            hydrate_mod,
            "_retrieve_netcdf_vars",
            lambda ds: {"elevation": {"long_name": "Surface elevation", "units": "m"}},
        )
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 0
        assert summary["unmatched"] == 1
        assert summary["skipped"] == 1, "unmatched still counts toward skipped"

    def test_a_missing_stanza_is_a_skip_not_a_declined_match(
        self, tmp_path, monkeypatch
    ):
        """A shard that never held the dataset had nothing for the matcher to decline."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(hydrate_mod, "_find_file_for_dataset", lambda *a: None)
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_netcdf_vars", lambda ds: dict(_NC_META)
        )
        summary = bulk_hydrate_empty()
        assert summary["unmatched"] == 0
        assert summary["skipped"] == 1

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

    def test_earlier_hydration_persists_when_a_later_one_aborts(
        self, tmp_path, monkeypatch
    ):
        """An abrupt stop keeps the shard writes already made (incremental writes)."""
        (tmp_path / "era5.yaml").write_text(_TWO_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {
                "a-dataset": _placeholder_dataset("sea-surface-temperature"),
                "z-dataset": _placeholder_dataset("sea-surface-temperature"),
            },
        )

        def _hydrate_then_abort(dataset_id):
            if dataset_id == "z-dataset":
                raise KeyboardInterrupt
            return dict(_NC_META)

        monkeypatch.setattr(hydrate_mod, "_retrieve_netcdf_vars", _hydrate_then_abort)
        with pytest.raises(KeyboardInterrupt):
            bulk_hydrate_empty(timeout=0)
        on_disk = yaml.safe_load((tmp_path / "era5.yaml").read_text())["datasets"]
        assert on_disk["a-dataset"]["variables"]["sea-surface-temperature"] == {
            "cds_variable": "sea_surface_temperature",
            "nc_variable": "sst",
            "units": "K",
        }, "the first dataset's hydration was written before the abort"

    def test_timed_out_dataset_is_counted_and_skipped(self, tmp_path, monkeypatch):
        """A retrieve that hits the deadline increments timed_out and skipped."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )

        def _stall(dataset_id, timeout):
            raise TimeoutError("stuck in the CDS queue")

        monkeypatch.setattr(hydrate_mod, "_retrieve_with_timeout", _stall)
        summary = bulk_hydrate_empty()
        assert summary["timed_out"] == 1, "the timed-out dataset is counted"
        assert summary["skipped"] == 1, "a timeout also counts as skipped"
        assert summary["hydrated"] == 0
