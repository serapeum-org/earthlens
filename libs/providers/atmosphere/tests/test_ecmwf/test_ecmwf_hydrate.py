"""Unit tests for `earthlens.ecmwf._hydrate` (CDS retrieve mocked)."""

from __future__ import annotations

import re
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from earthlens.base.yaml_loader import load_yaml_strict
from earthlens.ecmwf import _hydrate as hydrate_mod
from earthlens.ecmwf._hydrate import (
    _claimed_nc_names,
    _dataset_extras,
    _fill_variable,
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


class _FixtureBlocks:
    """A `_ServingBlocks` stand-in over an in-memory constraints fixture."""

    def __init__(self, rows):
        self._rows = rows
        self.enumerated = {key for block in rows for key in block}

    def __call__(self, cds_variable):
        """Return the fixture blocks that list `cds_variable`."""
        return [r for r in self._rows if cds_variable in (r.get("variable") or [])]


def _audit_catalog(monkeypatch, datasets):
    """Point `audit_serveability` at a fake catalog instead of the shipped one."""
    import earthlens.ecmwf as ecmwf_pkg

    monkeypatch.setattr(
        ecmwf_pkg, "Catalog", lambda: SimpleNamespace(datasets=datasets)
    )


class TestRedactCredentials:
    """The redaction itself, before `_error_summary` truncates what it returns."""

    def test_a_secret_past_the_summary_cap_is_still_struck(self):
        """`_error_summary` truncates at 160, which could hide a leak beyond it."""
        raw = "x" * 400 + " Authorization: Bearer leaked-token-value"

        redacted = hydrate_mod._redact_credentials(raw)

        assert "leaked-token-value" not in redacted, "a secret survived past the cap"
        assert hydrate_mod._REDACTED in redacted

    def test_text_carrying_no_credential_is_returned_unchanged(self):
        """Over-redacting costs the diagnosis the summary exists to give."""
        raw = "400 Bad Request: month must be one of 01..12"

        assert hydrate_mod._redact_credentials(raw) == raw

    def test_every_occurrence_of_the_configured_key_is_struck(self, monkeypatch):
        """One survivor is a leak, so a repeated key must not be partly missed."""
        monkeypatch.setattr(
            hydrate_mod, "_configured_keys", lambda: ["a-real-looking-key"]
        )

        redacted = hydrate_mod._redact_credentials(
            "a-real-looking-key failed, retry with a-real-looking-key"
        )

        assert "a-real-looking-key" not in redacted
        assert redacted.count(hydrate_mod._REDACTED) == 2

    def test_the_longest_key_is_struck_first(self, monkeypatch):
        """A key containing another must not be left half-redacted."""
        monkeypatch.setattr(
            hydrate_mod, "_configured_keys", lambda: ["long-key-prefix-and-suffix"]
        )

        redacted = hydrate_mod._redact_credentials("got long-key-prefix-and-suffix")

        assert redacted == f"got {hydrate_mod._REDACTED}"


class TestBlockSatisfies:
    """One constraints block against the whole request a row will send."""

    def test_a_block_offering_every_asked_key_satisfies(self):
        """The ordinary case."""
        block = {"product_type": ["forecast"], "year": ["2020"]}

        assert hydrate_mod._block_satisfies(
            {"product_type": ["forecast"], "year": ["2020"]}, block, set(block)
        )

    def test_one_disagreeing_key_is_enough_to_refuse(self):
        """A request is answered whole or not at all."""
        block = {"product_type": ["forecast"], "year": ["2020"]}

        assert not hydrate_mod._block_satisfies(
            {"product_type": ["forecast"], "year": ["2021"]}, block, set(block)
        )

    @pytest.mark.parametrize("value", [None, [], "surface_level", 3])
    def test_a_value_that_is_not_a_populated_list_is_not_judged(self, value):
        """A stripped or scalar selector carries no set to intersect."""
        block = {"product_type": ["forecast"]}

        assert hydrate_mod._block_satisfies(
            {"product_type": ["forecast"], "level": value}, block, set(block)
        )

    def test_a_key_absent_everywhere_is_a_free_choice(self):
        """Nothing partitions on it, so sending it constrains nothing."""
        assert hydrate_mod._block_satisfies(
            {"day": ["01"]}, {"product_type": ["forecast"]}, {"product_type"}
        )

    def test_a_key_absent_here_but_enumerated_elsewhere_is_a_conflict(self):
        """It belongs to another product, so this block cannot answer it."""
        assert not hydrate_mod._block_satisfies(
            {"leadtime_hour": ["3"]},
            {"product_type": ["analysis"]},
            {"product_type", "leadtime_hour"},
        )


class TestPromisesData:
    """Which rows the audit is entitled to judge."""

    @pytest.mark.parametrize(
        ("units", "unhydratable", "expected"),
        [
            ("K", None, True),
            ("unknown", None, False),
            ("unknown", "pseudo-slug", False),
            ("K", "pseudo-slug", False),
        ],
    )
    def test_only_a_filled_unmarked_row_promises_data(
        self, units, unhydratable, expected
    ):
        """A placeholder promises nothing, so it cannot be failing to deliver."""
        row = SimpleNamespace(units=units, unhydratable=unhydratable)

        assert hydrate_mod._promises_data(row) is expected


class TestUnserveableSelectors:
    """One row's merged selectors, returned only when nothing can serve them."""

    def test_a_serveable_row_returns_none(self):
        """None means there is nothing to report."""
        row = SimpleNamespace(cds_variable="2m_temperature", extras={})
        lookup = _FixtureBlocks(
            [{"variable": ["2m_temperature"], "product_type": ["forecast"]}]
        )

        assert (
            hydrate_mod._unserveable_selectors(
                {"product_type": ["forecast"]}, row, lookup
            )
            is None
        )

    def test_an_unserveable_row_returns_its_merged_selectors(self):
        """The caller reports them, so the row's own extras must win."""
        row = SimpleNamespace(
            cds_variable="2m_temperature", extras={"product_type": ["reanalysis"]}
        )
        lookup = _FixtureBlocks(
            [{"variable": ["2m_temperature"], "product_type": ["forecast"]}]
        )

        effective = hydrate_mod._unserveable_selectors(
            {"product_type": ["analysis"], "day": ["01"]}, row, lookup
        )

        assert effective == {"product_type": ["reanalysis"], "day": ["01"]}

    def test_a_variable_no_block_serves_is_not_judged(self):
        """There is nothing to check the request against."""
        row = SimpleNamespace(cds_variable="unknown_variable", extras={})
        lookup = _FixtureBlocks([{"variable": ["2m_temperature"]}])

        assert (
            hydrate_mod._unserveable_selectors({"product_type": ["x"]}, row, lookup)
            is None
        )


class TestQuoteIfNumberShaped:
    """The guard that keeps a scalar's type the file's rather than the parser's."""

    @pytest.mark.parametrize("value", ["1e-3", "08", "1.0", "0755"])
    def test_a_bare_number_shaped_scalar_is_quoted(self, value):
        """The emitter left it bare; another reader would give it a type."""
        assert hydrate_mod._quote_if_number_shaped(value, value) == f"'{value}'"

    @pytest.mark.parametrize("value", ["K", "W m-2", "2e", "day"])
    def test_a_plain_scalar_is_left_alone(self, value):
        """Quoting everything would churn the catalog for nothing."""
        assert hydrate_mod._quote_if_number_shaped(value, value) == value

    def test_an_already_quoted_scalar_is_not_quoted_twice(self):
        """The emitter having quoted it means the type is already pinned."""
        assert hydrate_mod._quote_if_number_shaped("'1'", "1") == "'1'"


class TestAuditServeability:
    """The #1147 invariant as code, so its clean result can be re-derived."""

    def test_a_row_no_block_can_serve_is_reported(self, monkeypatch):
        """Without this the audit could report clean and mean nothing."""
        _audit_catalog(
            monkeypatch,
            {
                "a-dataset": SimpleNamespace(
                    extras={"product_type": ["reanalysis"]},
                    variables={
                        "t2m": SimpleNamespace(
                            units="K",
                            unhydratable=None,
                            cds_variable="2m_temperature",
                            extras={},
                        )
                    },
                )
            },
        )
        blocks = [{"variable": ["2m_temperature"], "product_type": ["forecast"]}]

        findings = hydrate_mod.audit_serveability(lambda name: _FixtureBlocks(blocks))

        assert [(d, s) for d, s, _ in findings] == [("a-dataset", "t2m")]

    def test_a_row_one_block_can_serve_is_not_reported(self, monkeypatch):
        """The ordinary case must stay silent or the audit is noise."""
        _audit_catalog(
            monkeypatch,
            {
                "a-dataset": SimpleNamespace(
                    extras={"product_type": ["forecast"]},
                    variables={
                        "t2m": SimpleNamespace(
                            units="K",
                            unhydratable=None,
                            cds_variable="2m_temperature",
                            extras={},
                        )
                    },
                )
            },
        )
        blocks = [{"variable": ["2m_temperature"], "product_type": ["forecast"]}]

        assert hydrate_mod.audit_serveability(lambda name: _FixtureBlocks(blocks)) == []

    @pytest.mark.parametrize(
        ("units", "unhydratable"), [("unknown", None), ("unknown", "pseudo-slug")]
    )
    def test_a_placeholder_is_not_audited(self, monkeypatch, units, unhydratable):
        """A row promising nothing cannot be failing to deliver it."""
        _audit_catalog(
            monkeypatch,
            {
                "a-dataset": SimpleNamespace(
                    extras={"product_type": ["reanalysis"]},
                    variables={
                        "t2m": SimpleNamespace(
                            units=units,
                            unhydratable=unhydratable,
                            cds_variable="2m_temperature",
                            extras={},
                        )
                    },
                )
            },
        )
        blocks = [{"variable": ["2m_temperature"], "product_type": ["forecast"]}]

        assert hydrate_mod.audit_serveability(lambda name: _FixtureBlocks(blocks)) == []

    def test_a_dataset_with_no_constraints_is_not_judged(self, monkeypatch):
        """Nothing to check against is not the same as failing the check."""
        _audit_catalog(
            monkeypatch,
            {
                "a-dataset": SimpleNamespace(
                    extras={"product_type": ["reanalysis"]},
                    variables={
                        "t2m": SimpleNamespace(
                            units="K",
                            unhydratable=None,
                            cds_variable="2m_temperature",
                            extras={},
                        )
                    },
                )
            },
        )

        assert hydrate_mod.audit_serveability(lambda name: _FixtureBlocks([])) == []

    @pytest.mark.e2e
    def test_no_shipped_row_is_unserveable_against_the_live_store(self):
        """The invariant on the real catalog, re-derivable rather than asserted."""
        findings = hydrate_mod.audit_serveability()

        assert not findings, "unserveable rows: " + "; ".join(
            f"{dataset}/{slug}" for dataset, slug, _ in findings[:20]
        )


class TestRedactionCoversTheCommonShapes:
    """Sweep output is pasted into issues; a credential must not ride along."""

    @pytest.mark.parametrize(
        ("raw", "secret"),
        [
            ("GET https://alice:s3cret@cds.example/api failed", "s3cret"),
            ("Cookie: session=abcdef123456 rejected", "abcdef123456"),
            ("Set-Cookie: sid=zzzsecret; Path=/", "zzzsecret"),
        ],
    )
    def test_a_url_credential_or_cookie_is_struck(self, raw, secret):
        """Both shapes appear in requests and urllib3 error text."""
        summary = hydrate_mod._error_summary(RuntimeError(raw))

        assert secret not in summary
        assert hydrate_mod._REDACTED in summary

    def test_a_short_configured_key_is_not_struck_blindly(self, monkeypatch):
        """Blanking every occurrence of `test` would cost the diagnosis."""
        monkeypatch.setattr(hydrate_mod, "_configured_keys", lambda: ["test"])

        summary = hydrate_mod._error_summary(RuntimeError("the latest request failed"))

        assert "latest request failed" in summary
        assert hydrate_mod._REDACTED not in summary

    def test_a_full_length_configured_key_is_still_struck(self, monkeypatch):
        """The floor guards against short values, not against real keys."""
        monkeypatch.setattr(
            hydrate_mod, "_configured_keys", lambda: ["a-real-looking-key-value"]
        )

        summary = hydrate_mod._error_summary(
            RuntimeError("refused (a-real-looking-key-value)")
        )

        assert "a-real-looking-key-value" not in summary
        assert hydrate_mod._REDACTED in summary


class TestServingBlocksAreFetchedLazily:
    """A stanza with nothing to hydrate must not pay for a constraints fetch."""

    def test_building_the_lookup_fetches_nothing(self, monkeypatch):
        """The caller builds one per dataset before knowing if any row needs it."""
        import earthlens.ecmwf.cli as ecmwf_cli

        calls = []
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda ds: calls.append(ds) or []
        )

        lookup = hydrate_mod._serving_blocks_for("a-dataset")

        assert calls == [], "constraints were fetched before any row was checked"
        lookup("t2m")
        assert calls == ["a-dataset"]

    def test_the_fetch_happens_once(self, monkeypatch):
        """Per dataset per process, not per row."""
        import earthlens.ecmwf.cli as ecmwf_cli

        calls = []
        monkeypatch.setattr(
            ecmwf_cli, "_ecmwf_constraints", lambda ds: calls.append(ds) or []
        )

        lookup = hydrate_mod._serving_blocks_for("a-dataset")
        lookup("t2m")
        lookup("sst")
        _ = lookup.enumerated

        assert calls == ["a-dataset"]


class TestDeclinedDetailTellsTheTruth:
    """The echo must not describe a response the sweep never received."""

    def test_a_stanza_that_issued_nothing_says_so(self):
        """Every placeholder lacking a cds_variable means no probe was sent."""
        session = hydrate_mod._ProbeSession("a-dataset", None)

        assert "no probe was issued" in hydrate_mod._declined_detail(session, ["x"])

    def test_a_probe_that_returned_nothing_is_reported_as_the_store_answering(self):
        """Asked and answered with nothing is a different fault from never asking."""
        session = hydrate_mod._ProbeSession("a-dataset", None)
        session.issued = 1

        detail = hydrate_mod._declined_detail(session, ["x"])

        assert "no variables at all" in detail
        assert "no probe was issued" not in detail

    def test_an_all_auxiliary_answer_names_what_was_held_back(self):
        """A row to widen, not a store to chase."""
        session = hydrate_mod._ProbeSession("a-dataset", None)
        session.issued = 1
        session.filtered = {"FAPAR_ERR"}

        detail = hydrate_mod._declined_detail(session, ["x"])

        assert "only coordinates and auxiliaries" in detail
        assert "FAPAR_ERR" in detail


class TestCallerDerivedSelectorsAreNotOverridden:
    """`extras` is merged last, so an override on a date key overrules the request."""

    @pytest.mark.parametrize("key", ["year", "month", "day"])
    def test_a_multi_value_date_override_is_refused(self, key):
        """Recording a selector's whole domain says nothing and discards the dates."""
        offered = {key: ["01", "02", "03"]}

        assert hydrate_mod._selector_override(offered, {key: ["01"]}) == {}

    @pytest.mark.parametrize("key", ["year", "month", "day"])
    def test_a_single_value_date_override_is_kept(self, key):
        """A monthly product genuinely requiring `day: 01` is a real pin."""
        assert hydrate_mod._selector_override({key: ["01"]}, {key: ["15"]}) == {
            key: ["01"]
        }

    def test_a_non_date_key_is_unaffected(self):
        """The rule is about keys the backend builds from the caller, not all keys."""
        assert hydrate_mod._selector_override(
            {"product_type": ["forecast", "analysis"]}, {"product_type": ["reanalysis"]}
        ) == {"product_type": ["forecast", "analysis"]}


class TestNoShippedRowOverrulesTheCallersDates:
    """The catalog on disk must not carry the shape the writer now refuses."""

    def test_no_row_pins_more_than_one_date_value(self):
        """Such a row silently replaces whatever range was asked for."""
        from earthlens.ecmwf import Catalog

        offenders = []
        for name, dataset in Catalog().datasets.items():
            for slug, row in dataset.variables.items():
                for key in ("year", "month", "day"):
                    value = (row.extras or {}).get(key)
                    if isinstance(value, list) and len(value) > 1:
                        offenders.append(f"{name}/{slug}: {key}={len(value)} values")

        assert not offenders, "rows overriding the caller's dates: " + "; ".join(
            offenders
        )


class TestSelectorsAreServeable:
    """A row must not ship selectors the store does not offer for its variable."""

    def test_the_ozone_shape_is_refused(self):
        """The name came from a 0-6 km column; the row asks for limb profiles."""
        blocks = [
            {
                "variable": ["mole_content_of_ozone_in_atmosphere_layer"],
                "sensor": ["gome", "gome2_a"],
                "vertical_aggregation": ["total_and_tropospheric_column_0_6_km_ir"],
            }
        ]
        effective = {
            "sensor": ["ace"],
            "vertical_aggregation": ["vertical_profiles_from_limb_sensors"],
        }
        assert not hydrate_mod._selectors_are_serveable(effective, blocks)

    def test_a_selector_the_blocks_offer_is_accepted(self):
        """One shared value is enough - the store can answer the request."""
        blocks = [{"sensor": ["gome", "gome2_a"]}]
        assert hydrate_mod._selectors_are_serveable({"sensor": ["gome"]}, blocks)

    def test_a_key_the_blocks_do_not_enumerate_is_not_judged(self):
        """Absence means the dataset does not partition on it, not that it is wrong."""
        blocks = [{"sensor": ["gome"]}]
        assert hydrate_mod._selectors_are_serveable({"area": ["global"]}, blocks)

    @pytest.mark.parametrize("value", [None, []])
    def test_an_empty_or_stripped_selector_is_not_judged(self, value):
        """A stripped key sends nothing, so there is nothing to be unserveable."""
        blocks = [{"day": ["01"]}]
        assert hydrate_mod._selectors_are_serveable({"day": value}, blocks)

    def test_a_row_whose_selectors_cannot_be_served_is_declined(self):
        """The whole point: such a row stays a placeholder instead of shipping."""
        text = (
            "datasets:\n  a-dataset:\n    extras:\n"
            "      sensor: [ace]\n"
            "    variables:\n"
            "      layer:\n        cds_variable: ozone\n"
            "        nc_variable: ozone\n        units: unknown\n"
        )
        out, filled, declined = hydrate_mod._hydrate_stanza_per_variable(
            text,
            "a-dataset",
            lambda name: ({"col": {"long_name": "ozone", "units": "mol m-2"}}, {}),
            lambda name: [{"variable": ["ozone"], "sensor": ["gome"]}],
        )
        assert filled == [], f"nothing should have been filled; got {filled}"
        assert declined == ["layer"], f"the row should be declined; got {declined}"
        assert "units: unknown" in out, "the placeholder should be left as it was"

    def test_a_row_the_store_can_serve_is_still_written(self):
        """The guard must not refuse the ordinary case it sits in front of."""
        text = (
            "datasets:\n  a-dataset:\n    extras:\n"
            "      sensor: [gome]\n"
            "    variables:\n"
            "      layer:\n        cds_variable: ozone\n"
            "        nc_variable: ozone\n        units: unknown\n"
        )
        _, filled, declined = hydrate_mod._hydrate_stanza_per_variable(
            text,
            "a-dataset",
            lambda name: ({"ozone": {"long_name": "ozone", "units": "mol m-2"}}, {}),
            lambda name: [{"variable": ["ozone"], "sensor": ["gome"]}],
        )
        assert filled == ["layer"], f"a serveable row was declined; {declined}"


class TestUnhydratableRows:
    """A placeholder no retrieve can answer is not the same as a pending one."""

    def test_a_dataset_whose_only_placeholder_is_marked_is_not_counted(self):
        """Counting it would select a dataset with nothing a retrieve could do."""
        from earthlens.ecmwf import Catalog

        catalog = Catalog()
        marked_only = [
            name
            for name, ds in catalog.datasets.items()
            if any(v.units == "unknown" for v in ds.variables.values())
            and not any(
                v.units == "unknown" and not v.unhydratable
                for v in ds.variables.values()
            )
        ]
        assert marked_only, "expected at least one all-marked dataset in the catalog"
        outstanding = {
            name: sum(
                v.units == "unknown" and not v.unhydratable
                for v in ds.variables.values()
            )
            for name, ds in catalog.datasets.items()
        }
        for name in marked_only:
            assert outstanding[name] == 0, (
                f"{name} has only marked placeholders but still counts as work"
            )

    def test_a_marked_row_is_not_offered_to_a_probe(self):
        """Probing it again spends a request to learn what the row already says."""
        block = (
            "      wanted:\n        nc_variable: x\n        units: unknown\n"
            "      all:\n        nc_variable: y\n        units: unknown\n"
            "        unhydratable: pseudo-slug\n"
        )
        assert hydrate_mod._placeholder_slugs(block) == ["wanted"], (
            "a row nothing can fill was queued for a retrieve anyway"
        )

    def test_an_unmarked_placeholder_is_still_offered(self):
        """The mark has to be what excludes it, not the slug's name."""
        block = "      all:\n        nc_variable: y\n        units: unknown\n"
        assert hydrate_mod._placeholder_slugs(block) == ["all"]

    def test_a_hydrated_row_is_never_offered(self):
        """Unchanged behaviour: a filled row is not a placeholder."""
        block = (
            "      done:\n        nc_variable: z\n        units: K\n"
            "        unhydratable: pseudo-slug\n"
        )
        assert hydrate_mod._placeholder_slugs(block) == []


class _ExceptionWithAnUnusuallyLongClassNameForOneDataset(Exception):
    """Stands in for an SDK error whose type name is itself most of a line."""


class TestErrorSummary:
    """What a skipped dataset reports about why it stopped."""

    def test_a_licence_refusal_and_a_file_lock_are_distinguishable(self):
        """Both are PermissionError; only one is something an operator can act on."""
        lock = hydrate_mod._error_summary(
            PermissionError("[WinError 32] The process cannot access the file")
        )
        licence = hydrate_mod._error_summary(
            PermissionError("CDS rejected the request for 'x': licence not accepted.")
        )
        assert lock != licence, "the two causes still read identically"
        assert "WinError 32" in lock
        assert "licence not accepted" in licence

    @pytest.mark.parametrize(
        ("raw", "secret"),
        [
            ("401: Authorization: Bearer abc123SECRET", "abc123SECRET"),
            ("Authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("x-api-key: 9f8e7d6c rejected", "9f8e7d6c"),
            ("private-token: glpat-XYZ", "glpat-XYZ"),
            ("GET https://cds/api?api_key=deadbeef&x=1 failed", "deadbeef"),
            ("GET https://cds/api?token=deadbeef failed", "deadbeef"),
        ],
    )
    def test_a_credential_never_reaches_the_echo(self, raw, secret):
        """A sweep's output is pasted into issues and CI logs."""
        summary = hydrate_mod._error_summary(RuntimeError(raw))

        assert secret not in summary
        assert hydrate_mod._REDACTED in summary

    def test_the_configured_key_is_struck_by_exact_match(self, monkeypatch):
        """The one secret whose value is knowable is struck outright, not guessed."""
        monkeypatch.setattr(
            hydrate_mod, "_configured_keys", lambda: ["s3cr3t-not-token-shaped"]
        )

        summary = hydrate_mod._error_summary(
            RuntimeError("store refused (s3cr3t-not-token-shaped)")
        )

        assert "s3cr3t" not in summary
        assert hydrate_mod._REDACTED in summary

    def test_an_ordinary_url_survives_redaction(self):
        """Over-redacting would cost the diagnosis the summary exists to give."""
        summary = hydrate_mod._error_summary(
            RuntimeError("403 Forbidden for url: https://cds.example/api/retrieve/v1")
        )

        assert "https://cds.example/api/retrieve/v1" in summary
        assert hydrate_mod._REDACTED not in summary

    def test_an_unimportable_endpoint_table_does_not_raise(self, monkeypatch):
        """The import is itself a way this fails; an env without the SDK must still report."""
        import builtins

        real_import = builtins.__import__

        def _refuse(name, *args, **kwargs):
            if name == "earthlens.ecmwf.endpoints":
                raise ImportError("no cdsapi in this environment")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _refuse)

        assert hydrate_mod._configured_keys() == []
        assert hydrate_mod._error_summary(RuntimeError("boom")) == "RuntimeError: boom"

    def test_reading_the_keys_never_raises(self, monkeypatch):
        """Redaction must not be what turns a reportable failure unreportable."""
        import earthlens.ecmwf.endpoints as endpoints_mod

        monkeypatch.setattr(
            endpoints_mod,
            "_resolve_key",
            lambda key_env: (_ for _ in ()).throw(OSError("no dotfile")),
        )

        assert hydrate_mod._configured_keys() == []

    def test_a_message_free_error_falls_back_to_its_type(self):
        """Some errors carry nothing; the type is then all there is to say."""
        assert hydrate_mod._error_summary(RuntimeError()) == "RuntimeError"

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("x" * 400),
            _ExceptionWithAnUnusuallyLongClassNameForOneDataset("x" * 400),
        ],
        ids=["short-type-name", "long-type-name"],
    )
    def test_a_long_message_is_truncated_to_the_documented_cap(self, error):
        """The cap covers the composed line, so the type name cannot push past it."""
        summary = hydrate_mod._error_summary(error)
        assert len(summary) == hydrate_mod._SUMMARY_LIMIT, (
            f"summary ran to {len(summary)} chars"
        )
        assert summary.endswith("...")


class TestSelectorsAreServeablePerBlock:
    """A retrieve is answered by one block, so one block must satisfy it all."""

    def test_a_single_block_answering_everything_is_serveable(self):
        """The ordinary case: the row asks for what one block offers."""
        serving = [{"product_type": ["forecast"], "year": ["2020"]}]

        assert hydrate_mod._selectors_are_serveable(
            {"product_type": ["forecast"], "year": ["2020"]}, serving
        )

    def test_keys_satisfied_by_different_blocks_are_not_serveable(self):
        """Unioning per key passes this; no single block can answer it."""
        serving = [
            {"product_type": ["forecast"], "year": ["2020"]},
            {"product_type": ["analysis"], "year": ["2021"]},
        ]

        assert not hydrate_mod._selectors_are_serveable(
            {"product_type": ["forecast"], "year": ["2021"]}, serving
        ), "key A from one block and key B from another is not one request"

    def test_a_value_no_block_offers_is_not_serveable(self):
        """The plain conflict the per-key form also caught."""
        serving = [{"product_type": ["forecast"]}]

        assert not hydrate_mod._selectors_are_serveable(
            {"product_type": ["reanalysis"]}, serving
        )

    def test_a_key_no_block_constrains_is_a_free_choice(self):
        """Absence everywhere means the dataset does not partition on it."""
        serving = [{"product_type": ["forecast"]}]

        assert hydrate_mod._selectors_are_serveable(
            {"product_type": ["forecast"], "day": ["01"]}, serving
        )

    def test_a_key_the_dataset_partitions_on_elsewhere_is_a_conflict(self):
        """The serving blocks omitting it does not make it free to send."""
        serving = [{"product_type": ["forecast"]}]

        assert not hydrate_mod._selectors_are_serveable(
            {"product_type": ["forecast"], "sensor_on_satellite": ["slstr"]},
            serving,
            {"product_type", "sensor_on_satellite"},
        )

    def test_nothing_to_judge_against_is_permitted(self):
        """A dataset publishing no constraints is written as before, not refused."""
        assert hydrate_mod._selectors_are_serveable({"product_type": ["x"]}, [])

    def test_the_lookup_carries_the_datasets_enumerated_keys(self, monkeypatch):
        """The per-block check needs the whole block set, not just the serving ones."""
        import earthlens.ecmwf.cli as ecmwf_cli

        blocks = [
            {"variable": ["t2m"], "product_type": ["forecast"]},
            {"variable": ["sst"], "sensor_on_satellite": ["slstr"]},
        ]
        monkeypatch.setattr(ecmwf_cli, "_ecmwf_constraints", lambda ds: blocks)
        lookup = hydrate_mod._ServingBlocks("a-dataset")

        assert [b["product_type"] for b in lookup("t2m")] == [["forecast"]]
        assert lookup.enumerated == {"variable", "product_type", "sensor_on_satellite"}


class TestServingBlocksFor:
    """Fetching a dataset's constraints is best-effort, so it cannot be fatal."""

    def test_the_blocks_serving_a_variable_are_returned(self, monkeypatch):
        """Only the blocks that list the variable are its serving blocks."""
        import earthlens.ecmwf.cli as ecmwf_cli

        monkeypatch.setattr(
            ecmwf_cli,
            "_ecmwf_constraints",
            lambda dataset: [
                {"variable": ["t2m"], "year": ["2020"]},
                {"variable": ["sst"], "year": ["2021"]},
                {"variable": ["t2m", "sst"], "year": ["2022"]},
            ],
        )

        serving = hydrate_mod._serving_blocks_for("a-dataset")

        assert [block["year"] for block in serving("t2m")] == [["2020"], ["2022"]]
        assert serving("unknown-variable") == []

    @pytest.mark.parametrize(
        "answer",
        [
            lambda dataset: (_ for _ in ()).throw(RuntimeError("constraints 500")),
            lambda dataset: None,
        ],
        ids=["fetch-raises", "no-constraints"],
    )
    def test_a_dataset_it_cannot_check_yields_no_blocks(self, monkeypatch, answer):
        """Refusing every row of an uncheckable dataset would be worse than not checking."""
        import earthlens.ecmwf.cli as ecmwf_cli

        monkeypatch.setattr(ecmwf_cli, "_ecmwf_constraints", answer)

        assert hydrate_mod._serving_blocks_for("a-dataset")("t2m") == []


class TestNumberShapedScalarsAreQuoted:
    """A written scalar's type must be the file's, not the parser's."""

    @pytest.mark.parametrize(
        "value", ["1e-3", "1e-6", "1e-9", "08", "09", "1.0", "1", "1.", "0755"]
    )
    def test_a_number_shaped_string_is_quoted(self, value):
        """PyYAML leaves `1e-3` and `08` bare; a YAML 1.2 reader would type them."""
        rendered = hydrate_mod._yaml_value(value)

        assert rendered.startswith("'"), f"{rendered} is not quoted"
        assert rendered.endswith("'"), f"{rendered} is not quoted"
        assert yaml.safe_load(f"x: {rendered}")["x"] == value

    @pytest.mark.parametrize("value", ["K", "W m-2", "(0 - 1)", "2e", "day"])
    def test_an_ordinary_unit_is_left_bare(self, value):
        """Quoting everything would churn the catalog for nothing."""
        assert hydrate_mod._yaml_value(value) == value

    def test_a_month_list_does_not_mix_quoted_and_bare(self):
        """`['01', 08, 09, '10']` is one loader change from two types in one list."""
        rendered = hydrate_mod._yaml_inline_list(["01", "07", "08", "09", "10"])

        assert rendered == "['01', '07', '08', '09', '10']"
        assert yaml.safe_load(rendered) == ["01", "07", "08", "09", "10"]


#: Keys whose value the catalog types as a string. `grid_resolution: 0.05` and
#: `version: 3` are genuinely numeric fields and are none of this check's
#: business - only a string that a resolver could retype is.
_STRING_VALUED_KEYS = ("units", "nc_variable", "cds_variable", "unhydratable")


def _scalar_values(code):
    """Every unquoted scalar the catalog means as a string on one line."""
    match = re.match(r"^\s*([A-Za-z_][\w-]*):[ 	]+(.*)$", code)
    if not match:
        return []
    key, value = match.group(1), match.group(2).strip()
    if value.startswith("[") and value.endswith("]"):
        # A selector list: every item is a string, whatever it looks like.
        items = [item.strip() for item in value[1:-1].split(",")]
        return [item for item in items if item and not item.startswith(("'", '"'))]
    if key not in _STRING_VALUED_KEYS:
        return []
    if not value or value.startswith(("'", '"', "{", "&", "*")):
        return []
    return [value]


class TestTheEmitterNeverFoldsAScalar:
    """A folded scalar would break the one line it is spliced into."""

    @pytest.mark.parametrize(
        "value",
        [
            "a very long selector value " * 5,
            "x" * 300,
            "long name with spaces " * 6,
        ],
    )
    def test_a_long_value_stays_on_one_line(self, value):
        """At the emitter's default 80 columns this folds onto a continuation."""
        rendered = hydrate_mod._yaml_value(value)

        assert chr(10) not in rendered, "the scalar was folded across lines"
        assert yaml.safe_load(f"x: {rendered}")["x"] == value

    def test_a_long_item_stays_on_one_line_inside_a_list(self):
        """The list path renders item by item, so each item carries the risk."""
        value = "a very long selector value " * 5

        rendered = hydrate_mod._yaml_inline_list([value, "x"])

        assert chr(10) not in rendered
        assert yaml.safe_load(rendered) == [value, "x"]


class TestShippedCatalogHasNoResolverDependentScalars:
    """What the emitter now refuses must also not already be on disk."""

    def test_no_shard_carries_a_bare_number_shaped_scalar(self):
        """These load as strings only by luck of PyYAML's 1.1 resolver."""
        from earthlens.ecmwf.catalog import CATALOG_PATH

        offenders = []
        for shard in sorted(Path(CATALOG_PATH).glob("*.yaml")):
            for number, line in enumerate(
                shard.read_text(encoding="utf-8").splitlines(), 1
            ):
                for value in _scalar_values(line.split("#")[0]):
                    if hydrate_mod._NUMBER_SHAPED.fullmatch(value):
                        offenders.append(f"{shard.name}:{number}: {line.strip()}")

        assert not offenders, "bare number-shaped scalars: " + "; ".join(offenders)


class TestUnhydratableIsReadTheSameWay:
    """The regex and the pydantic model read one row's mark identically."""

    @pytest.mark.parametrize(
        ("written", "is_terminal"),
        [
            ("pseudo-slug", True),
            ("pseudo-slug  # all-variables", True),
            ("null", False),
            ("Null", False),
            ("NULL", False),
            ("~", False),
            ("~   ", False),
            ("null  # pending", False),
            ("", False),
            ("   ", False),
        ],
    )
    def test_both_readers_agree_on_every_spelling(self, written, is_terminal):
        """A null loads as None - pending - so the sweep must not skip the row."""
        from earthlens.ecmwf.catalog import Variable

        newline = chr(10)
        body = (
            f"        units: unknown{newline}        unhydratable: {written}{newline}"
        )
        loaded = Variable.model_validate(
            {
                "cds_dataset": "a-dataset",
                "cds_variable": "a-variable",
                "nc_variable": "v",
                "units": "unknown",
                **yaml.safe_load(f"unhydratable: {written}"),
            }
        )

        assert bool(hydrate_mod._UNHYDRATABLE.search(body)) is is_terminal
        assert bool(loaded.unhydratable) is is_terminal, (
            "the sweep and the catalog disagree about whether this row is done"
        )

    @pytest.mark.parametrize("written", ["nullish", "null-ish", "nullary reason"])
    def test_a_value_merely_starting_with_null_still_marks(self, written):
        """The exclusion is for the null scalar, not for anything spelt near it."""
        newline = chr(10)
        body = (
            f"        units: unknown{newline}        unhydratable: {written}{newline}"
        )

        assert hydrate_mod._UNHYDRATABLE.search(body)


class TestDropRestatements:
    """A temporal restatement is the same quantity, not a second candidate."""

    def test_the_monthly_twin_is_dropped_when_the_base_is_present(self):
        """The CAMS inventories offer emiss_bio beside emiss_bio_monthly."""
        offered = {
            "emiss_bio": {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
            "emiss_bio_monthly": {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
        }
        assert list(hydrate_mod._data_variables(offered)) == ["emiss_bio"]

    def test_a_row_now_matches_where_it_previously_declined(self):
        """Two spellings of one quantity left the matcher nothing to choose."""
        offered = {
            "emiss_bio": {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
            "emiss_bio_monthly": {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
        }
        assert hydrate_mod._match_variables(["acetaldehyde"], offered) == {
            "acetaldehyde": ("emiss_bio", "kg m-2 s-1")
        }

    def test_a_restatement_without_its_base_is_kept(self):
        """It is then the only candidate, and dropping it would lose the row."""
        offered = {"emiss_bio_monthly": {"long_name": "acetaldehyde", "units": "1"}}
        assert list(hydrate_mod._data_variables(offered)) == ["emiss_bio_monthly"]

    def test_an_unrelated_name_ending_in_a_cadence_is_kept(self):
        """Only a restatement of a base the file also holds is dropped."""
        offered = {
            "t2m_monthly": {"long_name": "temperature", "units": "K"},
            "sst": {"long_name": "sea surface temperature", "units": "K"},
        }
        assert sorted(hydrate_mod._data_variables(offered)) == ["sst", "t2m_monthly"]

    @pytest.mark.parametrize("base", ["EMISS_BIO", "emiss_bio"])
    @pytest.mark.parametrize("twin", ["EMISS_BIO_MONTHLY", "emiss_bio_monthly"])
    def test_the_two_spellings_need_not_agree_on_case(self, base, twin):
        """A producer spells the base one way and the restatement another."""
        offered = {
            base: {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
            twin: {"long_name": "acetaldehyde", "units": "kg m-2 s-1"},
        }
        assert list(hydrate_mod._data_variables(offered)) == [base]

    @pytest.mark.parametrize("suffix", ["_annual", "_daily", "_climatology"])
    def test_a_non_monthly_cadence_sibling_is_kept(self, suffix):
        """A climatology is a long-term mean, not the field restated."""
        offered = {
            "emi": {"long_name": "x", "units": "1"},
            f"emi{suffix}": {"long_name": "x", "units": "1"},
        }
        assert sorted(hydrate_mod._data_variables(offered)) == ["emi", f"emi{suffix}"]


#: One name a producer actually spells for each auxiliary suffix. Independent
#: data rather than `f"quantity{suffix}"`, which would only re-assert that
#: `endswith` works — a suffix nothing is named after could be added and pass.
_SUFFIX_SPELLINGS = {
    "_bnds": "lat_bnds",
    "_bounds": "time_bounds",
    "_count": "pixel_count",
    "_status": "retrieval_status",
    "_flag": "quality_flag",
    "_flags": "surface_flags",
    "_qflag": "FAPAR_QFLAG",
    "_qflags": "LAI_QFLAGS",
    "_err": "FAPAR_ERR",
    "_error": "standard_error",
    "_unc": "swe_unc",
    "_uncertainty": "sst_uncertainty",
    "_stddev": "ice_conc_stddev",
    "_sigma": "aod_sigma",
    "_zenith_angle": "sensor_zenith_angle",
    "_azimuth_angle": "solar_azimuth_angle",
    "_covered_hours": "num_covered_hours",
}


class TestIsAuxiliary:
    """Which retrieved variables may stand in for a catalog slug."""

    @pytest.mark.parametrize(
        "name",
        [
            "lat_bnds",
            "pixel_count",
            "quality_flag",
            "FAPAR_QFLAG",
            "FAPAR_ERR",
            "swe_unc",
            "sla_uncertainty",
            "ice_conc_stddev",
            "chl_qflags",
            "sst_error",
            "wind_sigma",
            "sensor_zenith_angle",
        ],
        ids=[
            "bounds",
            "count",
            "flag",
            "cdr-quality-flag",
            "uncertainty-err",
            "uncertainty-unc",
            "uncertainty-spelled-out",
            "spread",
            "quality-flags-plural",
            "error-spelled-out",
            "sigma",
            "viewing-angle",
        ],
    )
    def test_a_band_describing_a_measurement_is_not_one(self, name):
        """An uncertainty or flag band is about a variable, not a variable."""
        assert hydrate_mod._is_auxiliary(name), f"{name!r} was offered as data"

    @pytest.mark.parametrize(
        "name",
        [
            "t2m",
            "sst",
            "glacier_area",
            # A suffix is a tail, so the same word leading a name is a variable.
            "error_estimate",
            "flagship_index",
            "uncertainty_budget",
            "count_of_wet_days",
            "status_of_forest",
            # And the bare word is a measurement in its own right.
            "err",
            "flag",
            "count",
            "sigma",
        ],
    )
    def test_a_real_variable_still_reads_as_data(self, name):
        """A suffix is a tail; the same word leading or alone is a measurement."""
        assert not hydrate_mod._is_auxiliary(name), f"{name!r} was filtered out"

    @pytest.mark.parametrize(("suffix", "spelt"), sorted(_SUFFIX_SPELLINGS.items()))
    def test_each_suffix_filters_a_name_a_producer_spells(self, suffix, spelt):
        """Grounded in a real spelling, so it cannot pass by rebuilding the tuple."""
        assert spelt.lower().endswith(suffix)
        assert hydrate_mod._is_auxiliary(spelt), f"{spelt!r} escaped the filter"

    def test_every_suffix_has_such_a_name(self):
        """Adding a suffix must mean naming the variable that motivated it."""
        assert set(_SUFFIX_SPELLINGS) == set(hydrate_mod._AUXILIARY_SUFFIXES)

    def test_no_shipped_variable_is_filtered_by_the_widening(self):
        """The real false-positive check: nothing curated must read as auxiliary."""
        from earthlens.ecmwf import Catalog

        swallowed = [
            f"{name}/{slug}"
            for name, dataset in Catalog().datasets.items()
            for slug, row in dataset.variables.items()
            if row.units != "unknown" and hydrate_mod._is_auxiliary(row.nc_variable)
        ]

        assert not swallowed, f"curated rows the filter would now reject: {swallowed}"

    @pytest.mark.parametrize("name", ["FAPAR_ERR", "fapar_err", "FaPaR_Err"])
    def test_the_match_ignores_case(self, name):
        """Producers vary the case; the filter must not depend on it."""
        assert hydrate_mod._is_auxiliary(name), f"{name!r} escaped the filter"

    @pytest.mark.parametrize("name", ["err", "unc", "sigma", "flag"])
    def test_a_bare_word_is_not_a_suffix(self, name):
        """These are tails, not names; a variable actually called `flag` is data."""
        assert not hydrate_mod._is_auxiliary(name), (
            f"{name!r} was filtered although nothing precedes the suffix"
        )

    def test_a_slug_is_not_bound_to_its_own_uncertainty_band(self):
        """The leftover rule pairs on shared words, and a band shares them all."""
        meta = {"FAPAR_ERR": {"long_name": "FAPAR uncertainty", "units": "1"}}
        assert hydrate_mod._match_variables(["fapar"], meta) == {}, (
            "the slug took the uncertainty band, which mis-extracts silently "
            "at aggregate= time with plausible units"
        )


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


def _placeholder_dataset(*slugs, unhydratable=None):
    """A fake Dataset whose named variables all carry the `unknown` sentinel.

    Carries `unhydratable` because `Variable` declares it: a stub missing a
    field the model has lets the source read it defensively, which would turn a
    later rename into every marked row silently re-entering the sweep.
    """
    return SimpleNamespace(
        variables={
            slug: SimpleNamespace(units="unknown", unhydratable=unhydratable)
            for slug in slugs
        }
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


class TestFillVariable:
    """Tests for writing a row's nc_variable and units back into the shard."""

    @pytest.mark.parametrize("nc_name", ["no", "yes", "on", "off", "null", "y", "sst"])
    def test_a_yaml_hostile_short_name_survives(self, nc_name):
        """Nitrogen monoxide is `no`, which bare YAML reads back as a boolean."""
        block = (
            "      a-row:"
            + chr(10)
            + "        cds_variable: a"
            + chr(10)
            + "        nc_variable: seeded"
            + chr(10)
            + "        units: unknown"
            + chr(10)
        )
        out = _fill_variable(block, "a-row", nc_name, "kg kg**-1")
        parsed = yaml.safe_load("root:" + chr(10) + out.replace("      ", "  "))
        row = parsed["root"]["a-row"]
        assert row["nc_variable"] == nc_name, "must stay the string it was given"
        assert row["units"] == "kg kg**-1"


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

    def test_a_blank_extras_region_reads_as_nothing(self):
        """Whitespace under the key is not a mapping to compare selectors against."""
        block = (
            "    extras:" + chr(10) + "      " + chr(10) + "    variables:" + chr(10)
        )
        assert _dataset_extras(block) == {}

    def test_a_row_whose_cds_variable_is_empty_is_not_indexed(self):
        """A blank cds_variable gives a probe nothing to ask for."""
        block = (
            "      a-row:"
            + chr(10)
            + "        cds_variable:"
            + chr(10)
            + "        units: unknown"
            + chr(10)
        )
        assert hydrate_mod._slug_cds_variables(block) == {}

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
        assert filled == []
        assert declined == []


class TestFoldedMatching:
    """Case-only differences between a slug and a NetCDF short name."""

    def test_a_slug_matches_a_name_differing_only_in_case(self):
        """A leaf CDR spells them FAPAR and LAI; the catalog spells them lowercase."""
        meta = {
            "FAPAR": {"long_name": "Fraction of absorbed PAR", "units": "1"},
            "LAI": {"long_name": "Leaf area index", "units": "m2/m2"},
        }
        assert hydrate_mod._match_variables(["fapar", "lai"], meta) == {
            "fapar": ("FAPAR", "1"),
            "lai": ("LAI", "m2/m2"),
        }

    def test_a_near_neighbour_is_not_reached_by_folding(self):
        """Only case may differ, so an uncertainty band is not a folded match."""
        candidates = {
            "FAPAR_ERR": {"long_name": "FAPAR uncertainty", "units": "1"},
            "LAI": {"long_name": "Leaf area index", "units": "m2/m2"},
        }
        assert hydrate_mod._folded_match("fapar", candidates, set()) is None

    def test_two_names_folding_together_are_declined(self):
        """Guessing between them is the mis-extraction this matcher avoids."""
        meta = {
            "Sst": {"long_name": "sea surface temperature", "units": "K"},
            "SST": {"long_name": "sea surface temperature", "units": "K"},
        }
        assert hydrate_mod._match_variables(["sst"], meta) == {}

    def test_an_exact_match_still_wins(self):
        """Folding is a fallback; it must not disturb a name that matched already."""
        meta = {
            "tp": {"long_name": "Total precipitation", "units": "m"},
            "TP": {"long_name": "other", "units": "mm"},
        }
        assert hydrate_mod._match_variables(["tp"], meta)["tp"] == ("tp", "m")


def _probe_that_leaks_scratch(dataset, cds_variable):
    """Stand in for a probe whose scratch directory could not be removed."""
    from earthlens.ecmwf import cli as ecmwf_cli

    path = "D:/earthlens-cache/probe-7f2a"
    if path not in ecmwf_cli.UNREMOVED_SCRATCH:
        ecmwf_cli.UNREMOVED_SCRATCH.append(path)
    return _fake_probe(dataset, cds_variable)


class TestBulkHydrateEmpty:
    """Tests for the catalog-wide hydrate driver (retrieve + catalog mocked)."""

    def test_the_scratch_a_sweep_could_not_remove_reaches_the_summary(
        self, monkeypatch, tmp_path, capsys
    ):
        """A silent tolerance leaves accumulating disk with nothing to attribute it to."""
        from earthlens.ecmwf import cli as ecmwf_cli

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
        monkeypatch.setattr(ecmwf_cli, "UNREMOVED_SCRATCH", [])
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_variable_meta", _probe_that_leaks_scratch
        )

        summary = bulk_hydrate_empty()

        assert summary["unremoved_scratch"] == ["D:/earthlens-cache/probe-7f2a"]
        assert "probe-7f2a" in capsys.readouterr().out

    def test_an_earlier_sweeps_survivors_are_not_reported_again(
        self, monkeypatch, tmp_path, capsys
    ):
        """The list is a module global; a second sweep must not inherit the first's."""
        from earthlens.ecmwf import cli as ecmwf_cli

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
        monkeypatch.setattr(
            ecmwf_cli, "UNREMOVED_SCRATCH", ["D:/earthlens-cache/from-an-earlier-run"]
        )
        monkeypatch.setattr(hydrate_mod, "_retrieve_variable_meta", _fake_probe)

        summary = bulk_hydrate_empty()

        assert summary["unremoved_scratch"] == []
        assert "from-an-earlier-run" not in capsys.readouterr().out

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
                    variables={
                        "total-precipitation": SimpleNamespace(
                            units="m", unhydratable=None
                        )
                    }
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
            "unremoved_scratch": [],
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
        echoed = capsys.readouterr().out
        assert "only coordinates and auxiliaries" in echoed
        assert "latitude" in echoed, "the echo does not say what was held back"

    def test_an_empty_answer_reads_differently_from_an_all_auxiliary_one(
        self, tmp_path, monkeypatch, capsys
    ):
        """One is a row to widen; the other is a store to chase."""
        (tmp_path / "era5.yaml").write_text(_STANZA, encoding="utf-8")
        _patch_catalog(
            monkeypatch,
            tmp_path,
            {"reanalysis-era5-single-levels": _placeholder_dataset("2m-temperature")},
        )
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_variable_meta", lambda ds, cds: ({}, {})
        )
        monkeypatch.setattr(
            hydrate_mod, "_retrieve_with_timeout", lambda ds, timeout: {}
        )

        bulk_hydrate_empty()

        echoed = capsys.readouterr().out
        assert "no variables at all" in echoed
        assert "only coordinates and auxiliaries" not in echoed

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
            lambda text, ds, probe, serving=None: (
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

    @pytest.mark.parametrize("bad", ["no", "yes", "null", "123", "off"])
    def test_a_row_that_reloads_as_a_non_string_is_refused(self, bad):
        """A written value can be valid YAML and still come back the wrong type."""
        shard = (
            "datasets:"
            + chr(10)
            + "  a-dataset:"
            + chr(10)
            + "    variables:"
            + chr(10)
            + "      2m-temperature:"
            + chr(10)
            + "        nc_variable: "
            + bad
            + chr(10)
            + "        units: K"
            + chr(10)
        )
        assert not hydrate_mod._written_rows_survive(
            shard, "a-dataset", ["2m-temperature"]
        ), "a non-string nc_variable breaks the catalog for every later dataset"

    def test_a_pre_existing_bad_row_does_not_block_the_write(self):
        """Only the rows this pass filled are judged; older defects are not ours."""
        shard = (
            "datasets:"
            + chr(10)
            + "  a-dataset:"
            + chr(10)
            + "    variables:"
            + chr(10)
            + "      2m-temperature:"
            + chr(10)
            + "        nc_variable: t2m"
            + chr(10)
            + "        units: K"
            + chr(10)
            + "      untouched:"
            + chr(10)
            + "        nc_variable: no"
            + chr(10)
            + "        units: '1'"
            + chr(10)
        )
        assert hydrate_mod._written_rows_survive(
            shard, "a-dataset", ["2m-temperature"]
        ), "refusing here would cost hydration for a defect the sweep did not cause"

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

    def test_limit_larger_than_the_catalog_takes_everything(self):
        """A budget nothing reaches leaves the worklist whole."""
        rows = {"a-dataset": 2, "z-dataset": 3}
        assert hydrate_mod._take_rows(["a-dataset", "z-dataset"], rows, 999) == [
            "a-dataset",
            "z-dataset",
        ]

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

    def test_a_refusal_after_a_partial_fill_is_named_too(
        self, tmp_path, monkeypatch, capsys
    ):
        """A store that declines mid-way is as re-runnable as a deadline."""
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
        seen = []

        def _one_then_refuse(dataset_id, cds_variable, timeout):
            seen.append(cds_variable)
            if len(seen) == 1:
                return _fake_probe(dataset_id, cds_variable)
            raise RuntimeError("licence not accepted")

        monkeypatch.setattr(hydrate_mod, "_probe_with_timeout", _one_then_refuse)
        summary = bulk_hydrate_empty()
        assert summary["partial"] == 1
        assert "RuntimeError" in capsys.readouterr().out

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
