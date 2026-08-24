"""Unit tests for `earthlens.ecmwf._hydrate` (CDS retrieve mocked)."""

from __future__ import annotations

import textwrap
import time
from types import SimpleNamespace

import pytest
import yaml

from earthlens.base.yaml_loader import load_yaml_strict
from earthlens.ecmwf import _hydrate as hydrate_mod
from earthlens.ecmwf._hydrate import (
    _claimed_nc_names,
    _dataset_extras,
    _fill_variable_extras,
    _find_file_for_dataset,
    _hydrate_stanza_per_variable,
    _indent_of,
    _inline_mapping,
    _is_initialism,
    _match_variables,
    _pair_is_evidenced,
    _retrieve_with_timeout,
    _rewrite_stanza,
    _selector_override,
    _stanza_match,
    _yaml_inline_list,
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

_PROBE_BY_CDS = {
    "2m_temperature": {"t2m": {"long_name": "2 metre temperature", "units": "K"}},
    "sea_surface_temperature": {
        "sst": {"long_name": "Sea surface temperature", "units": "K"}
    },
    "total_precipitation": {"tp": {"long_name": "Total precipitation", "units": "m"}},
}


def _fake_probe(dataset_id, cds_variable):
    """Answer a per-variable probe with just that variable, as a real one does."""
    return dict(_PROBE_BY_CDS.get(cds_variable, {})), {}


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
            # Only `water` of four tokens: real, but below the coverage bar, so
            # it keeps its placeholder rather than being guessed at.
            (
                "terrestrial-water-storage-anomaly",
                "lwe_thickness",
                "Liquid Water Equivalent Thickness",
                False,
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

    def test_a_row_without_an_nc_variable_key_reserves_nothing(self):
        """A hydrated row that never names a variable has claimed none."""
        block = """      keyless-row:
        cds_variable: k
        units: m
"""
        assert _claimed_nc_names(block) == frozenset()

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
        """Tokens shared with the long name are evidence once they cover the slug."""
        meta = {"long_name": "Total precipitation depth"}
        assert _pair_is_evidenced("total-precipitation", "zzz", meta) is True

    def test_initialism(self):
        """An initialism is evidence with no shared token at all."""
        assert _pair_is_evidenced("sea-surface-temperature", "sst", {}) is True

    @pytest.mark.parametrize(
        ("slug", "long_name"),
        [
            ("land-sea-mask", "Mean sea level pressure"),
            ("sub-surface-runoff", "Surface net solar radiation"),
        ],
    )
    def test_one_generic_word_does_not_cover_enough_of_the_slug(self, slug, long_name):
        """A lone generic word shared with the long name is coincidence, not evidence."""
        assert _pair_is_evidenced(slug, "zzz", {"long_name": long_name}) is False

    def test_shared_tokens_covering_half_the_slug_are_evidence(self):
        """Half the slug's tokens is the bar, so a two-token slug needs one of them."""
        assert _pair_is_evidenced("glacier-area", "area", {}) is True

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


_GLOFAS_STANZA = """datasets:
  cems-glofas-historical:
    endpoint: ewds
    extras:
      system_version: [version_4_0]
      timespan: [time_mean]
    variables:
      average-river-discharge-in-the-last-24-hours:
        cds_variable: average_river_discharge_in_the_last_24_hours
        nc_variable: average_river_discharge_in_the_last_24_hours
        units: unknown
      runoff-water-equivalent:
        cds_variable: runoff_water_equivalent
        nc_variable: runoff_water_equivalent
        units: unknown
      snow-depth-water-equivalent:
        cds_variable: snow_depth_water_equivalent
        nc_variable: snow_depth_water_equivalent
        units: unknown
      soil-wetness-index:
        cds_variable: soil_wetness_index
        nc_variable: soil_wetness_index
        units: unknown
"""

_GLOFAS_PROBES = {
    "average_river_discharge_in_the_last_24_hours": (
        {"avg_dis": {"long_name": "Average river discharge", "units": "m3 s-1"}},
        {"timespan": ["time_mean"], "system_version": ["version_4_0"]},
    ),
    "runoff_water_equivalent": (
        {"rowe": {"long_name": "Runoff water equivalent", "units": "kg m-2"}},
        {"timespan": ["time_mean"], "system_version": ["version_4_0"]},
    ),
    "snow_depth_water_equivalent": (
        {"sd": {"long_name": "Snow depth water equivalent", "units": "kg m-2"}},
        {"timespan": ["instantaneous"], "system_version": ["version_4_0"]},
    ),
    "soil_wetness_index": (
        {"swir": {"long_name": "Soil wetness index", "units": "1"}},
        {"timespan": ["instantaneous"], "system_version": ["version_4_0"]},
    ),
}


_DEMO_HEADER = """datasets:
  demo-dataset:
"""

_EXTRAS_STANZA = """datasets:
  demo-dataset:
    endpoint: ewds
    extras:
      # a comment the reader must skip
      timespan: [time_mean]

      system_version: [version_4_0]
    variables:
      already-overridden:
        cds_variable: already_overridden
        nc_variable: already_overridden
        units: unknown
        extras:
          timespan: [stale]
          keep_me: [yes]
      no-cds-variable:
        nc_variable: mystery
        units: unknown
"""


class TestSelectorPlumbing:
    """Tests for the pieces that turn a probe's selectors into a row override."""

    def test_dataset_extras_skips_comments_and_blank_lines(self):
        """The dataset-level extras are read past comments and spacing."""
        block = _stanza_match(_EXTRAS_STANZA, "demo-dataset").group(1)
        assert _dataset_extras(block) == {
            "timespan": ["time_mean"],
            "system_version": ["version_4_0"],
        }

    @pytest.mark.parametrize(
        "spelling",
        [
            "      timespan: [time_mean]",
            "      timespan: [ time_mean ]",
            "      timespan: ['time_mean']",
            "      timespan:" + chr(10) + "        - time_mean",
        ],
    )
    def test_respelling_the_stanza_list_is_not_a_disagreement(self, spelling):
        """Selectors compare as values, so formatting cannot fake an override."""
        block = (
            "    extras:" + chr(10) + spelling + chr(10) + "    variables:" + chr(10)
        )
        extras = _dataset_extras(block)
        assert extras["timespan"] == ["time_mean"]
        assert _selector_override({"timespan": ["time_mean"]}, extras) == {}

    def test_a_dataset_level_inline_mapping_is_read_too(self):
        """The stanza's own extras may be inline; it still parses to values."""
        block = (
            "    extras: {timespan: [time_mean]}" + chr(10) + "    variables:" + chr(10)
        )
        assert _dataset_extras(block) == {"timespan": ["time_mean"]}

    @pytest.mark.parametrize(
        "line",
        [
            "        extras: {timespan: [x]}",
            "        extras:",
            "        extras: not-a-mapping",
            "        extras: [a, b]",
        ],
    )
    def test_only_a_real_inline_mapping_parses(self, line):
        """A block key, a scalar or a sequence carries no inline mapping to read."""
        parsed = _inline_mapping(line)
        expected = {"timespan": ["x"]} if line.endswith("}") else {}
        assert parsed == expected

    def test_an_unparseable_extras_region_yields_nothing(self):
        """Reading half a malformed block would compare selectors against a lie."""
        block = (
            "    extras:"
            + chr(10)
            + "      timespan: [time_mean]"
            + chr(10)
            + "      stray-text-with-no-colon"
            + chr(10)
            + "    variables:"
            + chr(10)
        )
        assert _dataset_extras(block) == {}

    @pytest.mark.parametrize(
        ("shape", "expected"),
        [
            ("      area:" + chr(10) + "        north: 50", {"area": {"north": 50}}),
            (
                "      timespan:" + chr(10) + "        - time_mean",
                {"timespan": ["time_mean"]},
            ),
            ('      note: "keep # this"', {"note": "keep # this"}),
            ("      # only a comment", {}),
        ],
    )
    def test_the_extras_region_is_parsed_as_yaml(self, shape, expected):
        """Nesting, block sequences and quoted hashes all survive the read."""
        block = "    extras:" + chr(10) + shape + chr(10) + "    variables:" + chr(10)
        assert _dataset_extras(block) == expected

    def test_extras_running_to_the_end_of_the_block(self):
        """The reader stops cleanly when the extras are the stanza's last lines."""
        block = "    extras:" + chr(10) + "      timespan: [time_mean]" + chr(10)
        assert _dataset_extras(block) == {"timespan": ["time_mean"]}

    def test_dataset_extras_is_empty_without_the_block(self):
        """A stanza with no dataset-level extras yields nothing to compare against."""
        no_extras = """      a-row:
        units: unknown
"""
        assert _dataset_extras(no_extras) == {}

    @pytest.mark.parametrize(
        ("selectors", "expected"),
        [
            ({"timespan": ["instantaneous"]}, {"timespan": ["instantaneous"]}),
            ({"timespan": ["time_mean"]}, {}),
            ({"unknown_key": ["x"]}, {}),
            ({"system_version": ["version_4_0"]}, {}),
        ],
    )
    def test_only_a_differing_declared_selector_is_written(self, selectors, expected):
        """A selector is an override only when the stanza declares it and disagrees."""
        block = _stanza_match(_EXTRAS_STANZA, "demo-dataset").group(1)
        assert _selector_override(selectors, _dataset_extras(block)) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(["a"], "[a]"), (["a", "b"], "[a, b]"), ("unarchived", "unarchived")],
    )
    def test_selector_values_render_the_way_the_catalog_writes_them(
        self, value, expected
    ):
        """A list becomes an inline sequence; a scalar stays a scalar."""
        assert _yaml_inline_list(value) == expected

    def test_an_existing_override_is_merged_not_replaced(self):
        """A hand-set selector survives unless the probe contradicts it."""
        block = _stanza_match(_EXTRAS_STANZA, "demo-dataset").group(1)
        out = _fill_variable_extras(
            block, "already-overridden", {"timespan": ["instantaneous"]}
        )
        document = _DEMO_HEADER + out
        row = yaml.safe_load(document)["datasets"]["demo-dataset"]["variables"][
            "already-overridden"
        ]
        assert row["extras"]["timespan"] == ["instantaneous"], "probe wins"
        assert row["extras"]["keep_me"] == [True], "untouched key survives"

    def test_an_inline_mapping_extras_is_merged_not_duplicated(self):
        """Appending beside an inline mapping would give the row two extras keys."""
        inline = _EXTRAS_STANZA.replace(
            "        extras:"
            + chr(10)
            + "          timespan: [stale]"
            + chr(10)
            + "          keep_me: [yes]",
            "        extras: {timespan: [stale], keep_me: [yes]}",
        )
        block = _stanza_match(inline, "demo-dataset").group(1)
        out = _fill_variable_extras(
            block, "already-overridden", {"timespan": ["instantaneous"]}
        )
        keys = [
            line
            for line in out.splitlines()
            if line.strip().startswith("extras:") and _indent_of(line) == 8
        ]
        assert len(keys) == 1, "a second extras key makes the shard unloadable"
        row = yaml.safe_load(_DEMO_HEADER + out)["datasets"]["demo-dataset"][
            "variables"
        ]["already-overridden"]
        assert row["extras"]["timespan"] == ["instantaneous"]
        assert row["extras"]["keep_me"] == [True], "the other inline key survives"

    @pytest.mark.parametrize(
        ("shape", "existing"),
        [
            (
                "block sequence",
                "        extras:\n          timespan:\n            - old\n"
                "          keep_me: [yes]\n",
            ),
            (
                "block mapping",
                "        extras:\n          timespan: [old]\n          keep_me: [yes]\n",
            ),
            ("inline mapping", "        extras: {timespan: [old], keep_me: [yes]}\n"),
            ("absent", ""),
        ],
    )
    def test_every_extras_shape_survives_an_override(self, tmp_path, shape, existing):
        """Editing line by line orphans continuation lines; re-emitting cannot."""
        text = (
            "datasets:\n  demo:\n    variables:\n"
            "      a-row:\n        cds_variable: a_row\n        units: unknown\n"
            + existing
            + "      b-row:\n        cds_variable: b_row\n        units: unknown\n"
        )
        match = _stanza_match(text, "demo")
        out = _fill_variable_extras(match.group(1), "a-row", {"timespan": ["new"]})
        doc = text[: match.start()] + "  demo:\n" + out + text[match.end() :]
        path = tmp_path / "shard.yaml"
        path.write_text(doc, encoding="utf-8")
        variables = load_yaml_strict(path)["datasets"]["demo"]["variables"]
        assert variables["a-row"]["extras"]["timespan"] == ["new"], shape
        assert "b-row" in variables, "the neighbouring row must survive"
        if existing:
            assert variables["a-row"]["extras"]["keep_me"] == [True]

    @pytest.mark.parametrize(
        "inline", ["        extras: [a, b]" + chr(10), "        extras: null" + chr(10)]
    )
    def test_a_non_mapping_inline_extras_is_left_alone(self, inline):
        """What cannot be merged into must not be replaced; it is not ours to delete."""
        text = (
            "datasets:"
            + chr(10)
            + "  demo:"
            + chr(10)
            + "    variables:"
            + chr(10)
            + "      a-row:"
            + chr(10)
            + "        cds_variable: a_row"
            + chr(10)
            + "        units: unknown"
            + chr(10)
            + inline
        )
        block = _stanza_match(text, "demo").group(1)
        assert _fill_variable_extras(block, "a-row", {"timespan": ["new"]}) == block

    def test_an_empty_override_leaves_the_block_alone(self):
        """Nothing to record means nothing is written."""
        block = _stanza_match(_EXTRAS_STANZA, "demo-dataset").group(1)
        assert _fill_variable_extras(block, "already-overridden", {}) == block

    def test_an_absent_slug_leaves_the_block_alone(self):
        """A slug that is not in the stanza cannot be given an override."""
        block = _stanza_match(_EXTRAS_STANZA, "demo-dataset").group(1)
        assert _fill_variable_extras(block, "not-here", {"timespan": ["x"]}) == block

    def test_a_row_without_a_cds_variable_is_declined(self):
        """A probe needs the request-side name, so a row lacking one is skipped."""
        out, filled, declined = _hydrate_stanza_per_variable(
            _EXTRAS_STANZA,
            "demo-dataset",
            lambda cds: ({"x": {"long_name": "x", "units": "1"}}, {}),
        )
        assert "no-cds-variable" in declined

    def test_a_missing_stanza_is_a_no_op(self):
        """Hydrating a dataset the shard does not hold changes nothing."""
        out, filled, declined = _hydrate_stanza_per_variable(
            _EXTRAS_STANZA, "not-a-dataset", lambda cds: ({}, {})
        )
        assert (out, filled, declined) == (_EXTRAS_STANZA, [], [])


class TestHydrateStanzaWhole:
    """Tests for the whole-dataset fallback used when no block names a row."""

    def test_a_timeout_is_recorded_on_the_session(self, monkeypatch):
        """The deadline is reported as a timeout, not a generic failure."""
        session = hydrate_mod._ProbeSession("demo-dataset", 1.0)

        def _stall(dataset_id, timeout):
            raise TimeoutError("stuck")

        monkeypatch.setattr(hydrate_mod, "_retrieve_with_timeout", _stall)
        out, filled, declined = hydrate_mod._hydrate_stanza_whole(
            _EXTRAS_STANZA, "demo-dataset", session
        )
        assert session.timed_out is True
        assert (out, filled, declined) == (_EXTRAS_STANZA, [], [])

    def test_a_refusal_is_recorded_without_claiming_a_timeout(self, monkeypatch):
        """A licence refusal sets the error but leaves timed_out false."""
        session = hydrate_mod._ProbeSession("demo-dataset", 1.0)

        def _refuse(dataset_id, timeout):
            raise RuntimeError("licence not accepted")

        monkeypatch.setattr(hydrate_mod, "_retrieve_with_timeout", _refuse)
        hydrate_mod._hydrate_stanza_whole(_EXTRAS_STANZA, "demo-dataset", session)
        assert session.timed_out is False
        assert isinstance(session.error, RuntimeError)


class TestRetrieveVariableMeta:
    """Tests for the credentialed per-variable seam."""

    def test_it_forwards_to_the_ecmwf_deep_sampler(self, monkeypatch):
        """The seam only delegates, so the credentialed call stays in one place."""
        import earthlens.ecmwf.cli as ecmwf_cli

        seen = {}

        def _fake(dataset_id, cds_variable):
            seen["args"] = (dataset_id, cds_variable)
            return {"t2m": {"units": "K"}}, {"timespan": ["time_mean"]}

        monkeypatch.setattr(ecmwf_cli, "_ecmwf_deep_sample_variable", _fake)
        meta, selectors = hydrate_mod._retrieve_variable_meta("ds", "2m_temperature")
        assert seen["args"] == ("ds", "2m_temperature")
        assert meta == {"t2m": {"units": "K"}}
        assert selectors == {"timespan": ["time_mean"]}


class TestHydrateStanzaPerVariable:
    """Tests for the per-variable hydration of a whole stanza."""

    def test_every_placeholder_of_a_multi_variable_dataset_is_filled(self):
        """One probe per row is what lets a four-variable dataset finish."""
        asked = []

        def probe(cds_variable):
            asked.append(cds_variable)
            return _GLOFAS_PROBES[cds_variable]

        out, filled, declined = _hydrate_stanza_per_variable(
            _GLOFAS_STANZA, "cems-glofas-historical", probe
        )
        assert len(asked) == 4, "one probe per placeholder"
        assert len(filled) == 4
        assert declined == []
        variables = yaml.safe_load(out)["datasets"]["cems-glofas-historical"][
            "variables"
        ]
        assert (
            variables["average-river-discharge-in-the-last-24-hours"]["nc_variable"]
            == "avg_dis"
        )
        assert variables["soil-wetness-index"]["units"] == "1"

    def test_a_selector_the_dataset_default_covers_is_not_written(self):
        """Discharge runs under the stanza's own timespan, so it needs no override."""
        out, _, _ = _hydrate_stanza_per_variable(
            _GLOFAS_STANZA,
            "cems-glofas-historical",
            lambda cds: _GLOFAS_PROBES[cds],
        )
        variables = yaml.safe_load(out)["datasets"]["cems-glofas-historical"][
            "variables"
        ]
        row = variables["average-river-discharge-in-the-last-24-hours"]
        assert "extras" not in row

    def test_a_selector_that_differs_becomes_a_per_variable_override(self):
        """Snow depth is only served under instantaneous, and that is recorded."""
        out, _, _ = _hydrate_stanza_per_variable(
            _GLOFAS_STANZA,
            "cems-glofas-historical",
            lambda cds: _GLOFAS_PROBES[cds],
        )
        variables = yaml.safe_load(out)["datasets"]["cems-glofas-historical"][
            "variables"
        ]
        assert variables["snow-depth-water-equivalent"]["extras"] == {
            "timespan": ["instantaneous"]
        }
        assert variables["soil-wetness-index"]["extras"] == {
            "timespan": ["instantaneous"]
        }

    def test_a_row_the_store_cannot_serve_keeps_its_placeholder(self):
        """A variable no constraints block serves is declined, not guessed at."""
        out, filled, declined = _hydrate_stanza_per_variable(
            _GLOFAS_STANZA,
            "cems-glofas-historical",
            lambda cds: ({}, {}) if "snow" in cds else _GLOFAS_PROBES[cds],
        )
        assert "snow-depth-water-equivalent" in declined
        assert len(filled) == 3
        variables = yaml.safe_load(out)["datasets"]["cems-glofas-historical"][
            "variables"
        ]
        assert variables["snow-depth-water-equivalent"]["units"] == "unknown"

    def test_two_rows_cannot_claim_one_netcdf_variable(self):
        """A name bound earlier in the pass is withheld from later rows."""
        one = (
            {"avg_dis": {"long_name": "Average river discharge", "units": "m3 s-1"}},
            {},
        )
        out, filled, declined = _hydrate_stanza_per_variable(
            _GLOFAS_STANZA, "cems-glofas-historical", lambda cds: one
        )
        variables = yaml.safe_load(out)["datasets"]["cems-glofas-historical"][
            "variables"
        ]
        bound = [v["nc_variable"] for v in variables.values()]
        assert bound.count("avg_dis") == 1, "only the first row may take it"
        assert len(declined) == 3

    def test_a_stanza_without_placeholders_is_left_alone(self):
        """Nothing to fill means no probes and no rewrite."""
        filled_text = _GLOFAS_STANZA.replace("units: unknown", "units: m")
        out, filled, declined = _hydrate_stanza_per_variable(
            filled_text, "cems-glofas-historical", lambda cds: ({}, {})
        )
        assert out == filled_text
        assert filled == [] and declined == []


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
        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _fake_probe)

        summary = bulk_hydrate_empty()
        assert summary == {
            "candidates": 1,
            "hydrated": 1,
            "skipped": 0,
            "timed_out": 0,
            "unmatched": 0,
            "partial": 0,
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

        def _boom(dataset_id, cds_variable):
            raise RuntimeError("licence not accepted")

        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _boom)
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
            "_retrieve_variable_meta",
            lambda ds, cds: (
                {
                    "elevation": {"long_name": "Surface elevation", "units": "m"},
                    "orography": {"long_name": "Orography", "units": "m"},
                },
                {},
            ),
        )
        monkeypatch.setattr(
            hydrate_mod,
            "_retrieve_with_timeout",
            lambda ds, timeout: {
                "elevation": {"long_name": "Surface elevation", "units": "m"},
                "orography": {"long_name": "Orography", "units": "m"},
            },
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
        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _fake_probe)
        summary = bulk_hydrate_empty()
        assert summary["unmatched"] == 0
        assert summary["skipped"] == 1

    def test_a_stanza_with_nothing_left_to_fill_is_a_skip(self, tmp_path, monkeypatch):
        """A stanza whose placeholders are already filled is not a declined match."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"other-dataset": _placeholder_dataset("total-precipitation")},
        )
        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _fake_probe)
        summary = bulk_hydrate_empty()
        assert summary["unmatched"] == 0
        assert summary["skipped"] == 1

    def test_a_retrieve_of_only_auxiliaries_says_so(
        self, tmp_path, monkeypatch, capsys
    ):
        """When nothing retrieved is a data variable, the echo names that case."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(
            hydrate_mod,
            "_retrieve_variable_meta",
            lambda ds, cds: (
                {"latitude": {"long_name": "latitude", "units": "degrees"}},
                {},
            ),
        )
        monkeypatch.setattr(
            hydrate_mod,
            "_retrieve_with_timeout",
            lambda ds, timeout: {"latitude": {"long_name": "latitude", "units": "deg"}},
        )
        summary = bulk_hydrate_empty()
        assert summary["unmatched"] == 1
        assert "only coordinates and auxiliaries" in capsys.readouterr().out

    def test_a_dataset_no_block_names_falls_back_to_one_whole_probe(
        self, tmp_path, monkeypatch
    ):
        """A product that does not partition by variable is still hydratable."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        # No constraints block names the row, so every per-variable probe is empty.
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_variable_meta", lambda ds, cds: ({}, {})
        )
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_with_timeout", lambda ds, timeout: dict(_NC_META)
        )
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 1, "the whole-dataset probe finished it"
        variables = yaml.safe_load((tmp_path / "era5.yaml").read_text())["datasets"][
            "reanalysis-era5-single-levels"
        ]["variables"]
        assert variables["2m-temperature"]["nc_variable"] == "t2m"

    def test_the_fallback_recovers_a_row_the_per_variable_pass_declined(
        self, tmp_path, monkeypatch
    ):
        """A row no block serves is still reachable by name after siblings answer."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(
            hydrate_mod,
            "_retrieve_variable_meta",
            lambda ds, cds: ({"latitude": {"long_name": "lat", "units": "deg"}}, {}),
        )

        monkeypatch.setattr(
            hydrate_mod, "_retrieve_with_timeout", lambda ds, timeout: dict(_NC_META)
        )
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 1, "the fallback matched it by name"
        variables = yaml.safe_load((tmp_path / "era5.yaml").read_text())["datasets"][
            "reanalysis-era5-single-levels"
        ]["variables"]
        assert variables["2m-temperature"]["nc_variable"] == "t2m"

    def test_a_rewrite_that_does_not_parse_is_never_written(
        self, tmp_path, monkeypatch, capsys
    ):
        """A splicing bug must cost one dataset's hydration, not the whole shard."""
        shard = tmp_path / "era5.yaml"
        shard.write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _fake_probe)
        monkeypatch.setattr(
            hydrate_mod,
            "_hydrate_stanza_per_variable",
            lambda text, ds, probe: (
                "datasets:"
                + chr(10)
                + "  a:"
                + chr(10)
                + "    x: 1"
                + chr(10)
                + "    x: 2"
                + chr(10),
                ["2m-temperature"],
                [],
            ),
        )
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 0, "a shard that would not load is not written"
        assert shard.read_text(encoding="utf-8") == _STANZA, "shard untouched"
        assert "did not parse" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [(1, ["a-dataset"]), (2, ["a-dataset"]), (3, ["a-dataset", "z-dataset"])],
    )
    def test_limit_counts_placeholder_rows_not_datasets(self, limit, expected):
        """One retrieve per row, so a dataset-counting limit would not bound the work."""
        rows = {"a-dataset": 2, "z-dataset": 5}
        assert (
            hydrate_mod._take_rows(["a-dataset", "z-dataset"], rows, limit) == expected
        )

    def test_limit_never_splits_a_dataset(self):
        """Half a hydrated stanza is not a useful stopping point, so it is a floor."""
        rows = {"wide-dataset": 82}
        assert hydrate_mod._take_rows(["wide-dataset"], rows, 1) == ["wide-dataset"]

    def test_a_dataset_that_stops_midway_says_why(self, tmp_path, monkeypatch, capsys):
        """A partial fill must not hide the deadline that ended it."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {
                "reanalysis-era5-single-levels": _placeholder_dataset(
                    "2m-temperature", "sea-surface-temperature"
                )
            },
        )
        calls = []

        def _one_then_stall(dataset_id, cds_variable, timeout):
            calls.append(cds_variable)
            if len(calls) == 1:
                return _fake_probe(dataset_id, cds_variable)
            raise TimeoutError("stuck in the CDS queue")

        monkeypatch.setattr(hydrate_mod, "_probe_with_timeout", _one_then_stall)
        summary = bulk_hydrate_empty()
        assert summary["hydrated"] == 1, "the row it did fill still counts"
        assert summary["partial"] == 1, "and the dataset is flagged for a re-run"
        assert "timed out" in capsys.readouterr().out

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
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_variable_meta", lambda ds, cds: ({}, {})
        )
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

        def _hydrate_then_abort(dataset_id, cds_variable):
            if dataset_id == "z-dataset":
                raise KeyboardInterrupt
            return _fake_probe(dataset_id, cds_variable)

        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _hydrate_then_abort)
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

        def _stall(dataset_id, cds_variable, timeout):
            raise TimeoutError("stuck in the CDS queue")

        monkeypatch.setattr(hydrate_mod, "_probe_with_timeout", _stall)
        summary = bulk_hydrate_empty()
        assert summary["timed_out"] == 1, "the timed-out dataset is counted"
        assert summary["skipped"] == 1, "a timeout also counts as skipped"
        assert summary["hydrated"] == 0
