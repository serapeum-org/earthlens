"""Tests for the NWM catalog-tooling handlers (`earthlens.nwm.cli`).

Moved out of core's CLI test suite when the NWM refresh + validate handlers moved
into this distribution (issue #863). All boto bodies are mocked; the live walk is
exercised by `test_nwm_live_e2e.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import earthlens.nwm.cli as nwm_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.refresh import audit_one, refresh_one
from earthlens.cli.validate import validate_one
from earthlens.nwm.backend import BUCKET
from earthlens.testing import seed_s3

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the nwm backend."""
    return next(b for b in list_backends() if b.provider == "nwm")


def _date_page(*days):
    """Build a paginator page of `nwm.<day>/` common prefixes."""
    return {"CommonPrefixes": [{"Prefix": f"nwm.{day}/"} for day in days]}


def _nwm_days(client, *days):
    """Publish one object under each `nwm.<day>/` prefix and hand back the client.

    The day prefixes then come back as real `CommonPrefixes` from a real
    delimiter listing, rather than from a paginator double that yielded a
    canned page.
    """
    seed_s3(client, BUCKET, {f"nwm.{day}/placeholder": b"" for day in days})
    return client


def _key(directory, output):
    """Build a NWM object key with the given `{output}` token."""
    return f"nwm.20260602/{directory}/nwm.t00z.short_range.{output}.f001.conus.nc"


class TestRefresher:
    """Tests for the NWM (unsigned operational-bucket walk) lister."""

    def test_collapses_ensemble_members_to_base_config(self):
        """A `_mem<N>` member directory collapses to its base config key."""
        assert nwm_cli._collapse_member("medium_range_mem3") == "medium_range"
        assert nwm_cli._collapse_member("short_range") == "short_range"

    def test_refresh_diffs_collapsed_live_against_configurations(self, monkeypatch):
        """Live config dirs collapse to the curated namespace before the diff."""
        catalog = load_catalog(_info())
        live_dirs = [
            f"{key}_mem1" if cfg.members else key
            for key, cfg in catalog.configurations.items()
        ] + ["usgs_timeslices"]
        monkeypatch.setattr(nwm_cli, "_live_config_dirs", lambda: live_dirs)
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "nwm refresh ran"
        assert outcome.live_count == len(catalog.configurations) + 1, (
            "members collapsed"
        )
        assert outcome.new_ids == ["usgs_timeslices"], "only the uncurated dir is new"
        assert not outcome.removed_ids, "every curated config is still live"

    def test_audit_curated_config_not_broken_uncurated_untracked(self, monkeypatch):
        """A live curated config is not broken; usgs_timeslices is untracked."""
        catalog = load_catalog(_info())
        live_dirs = [
            f"{key}_mem1" if cfg.members else key
            for key, cfg in catalog.configurations.items()
        ] + ["usgs_timeslices"]
        monkeypatch.setattr(nwm_cli, "_live_config_dirs", lambda: live_dirs)
        outcome = audit_one(_info())
        assert outcome.status == "ok", "nwm audit ran"
        assert "short_range" not in outcome.broken, (
            "a live curated config is not broken"
        )
        assert "usgs_timeslices" in outcome.untracked, "the uncurated dir is untracked"

    def test_refresh_has_no_writer(self, monkeypatch):
        """nwm's index is derived from curated rows, so --write is a no-op read."""
        monkeypatch.setattr(nwm_cli, "_live_config_dirs", lambda: ["short_range"])
        outcome = refresh_one(_info(), write=True)
        assert outcome.status == "ok", "nwm refresh ran"
        assert not outcome.written, "no on-disk index block to rewrite"
        assert "live read only" in outcome.detail, "reported as read-only"


class TestBucketPrimitives:
    """Tests for the shared NWM bucket primitives."""

    def test_unsigned_client_is_us_east_1_s3(self):
        """`_unsigned_client` builds an unsigned us-east-1 S3 client offline."""
        client = nwm_cli._unsigned_client()
        assert client.meta.region_name == "us-east-1", "region pinned to us-east-1"
        assert client.meta.service_model.service_name == "s3", (
            "an S3 client is returned"
        )

    def test_latest_complete_day_picks_day_before_latest(self, s3_emulator):
        """The day before the newest prefix is chosen (newest may be partial)."""
        client = _nwm_days(s3_emulator, "20260601", "20260603", "20260602")
        assert nwm_cli._latest_complete_day(client) == "nwm.20260602", (
            "second-newest day selected"
        )

    def test_latest_complete_day_single_day_uses_only_day(self, s3_emulator):
        """With a single published day, that day is used as-is."""
        client = _nwm_days(s3_emulator, "20260601")
        assert nwm_cli._latest_complete_day(client) == "nwm.20260601"

    def test_latest_complete_day_ignores_non_nwm_prefixes(self, s3_emulator):
        """Prefixes that do not start with `nwm.` are skipped."""
        seed_s3(s3_emulator, BUCKET, {"index/placeholder": b""})
        client = s3_emulator
        with pytest.raises(RuntimeError, match=r"no nwm\.YYYYMMDD"):
            nwm_cli._latest_complete_day(client)

    def test_config_dirs_parses_and_sorts_directory_names(self, s3_emulator):
        """Configuration directory names are parsed from the day's prefixes."""
        seed_s3(
            s3_emulator,
            BUCKET,
            {
                "nwm.20260602/short_range/f.nc": b"",
                "nwm.20260602/medium_range_mem1/f.nc": b"",
            },
        )
        client = s3_emulator
        assert nwm_cli._config_dirs(client, "nwm.20260602") == [
            "medium_range_mem1",
            "short_range",
        ], "directory names parsed and sorted"

    def test_live_config_dirs_composes_the_primitives(self, monkeypatch):
        """`_live_config_dirs` wires client -> day -> dirs together."""
        monkeypatch.setattr(nwm_cli, "_unsigned_client", lambda: "CLIENT")
        monkeypatch.setattr(
            nwm_cli,
            "_latest_complete_day",
            lambda client: "DAY" if client == "CLIENT" else "WRONG",
        )
        monkeypatch.setattr(
            nwm_cli,
            "_config_dirs",
            lambda client, day: (
                ["a", "b"] if (client, day) == ("CLIENT", "DAY") else []
            ),
        )
        assert nwm_cli._live_config_dirs() == ["a", "b"], "primitives composed"


class TestValidateInternals:
    """Direct tests for the NWM validate helpers (network mocked)."""

    def test_sample_tokens_parses_output_and_skips_short_names(self, s3_emulator):
        """The `{output}` token is parsed; names with too few parts are skipped."""
        seed_s3(
            s3_emulator,
            BUCKET,
            {
                _key("short_range", "channel_rt"): b"",
                "nwm.20260602/short_range/nwm.t00z.land.nc": b"",
            },
        )
        client = s3_emulator
        tokens = nwm_cli._sample_tokens(client, "nwm.20260602", "short_range")
        assert tokens == {"channel_rt"}, f"only the well-formed token parsed: {tokens}"

    def test_sample_tokens_empty_listing_returns_empty_set(self, s3_emulator):
        """A directory with no objects samples an empty token set."""
        seed_s3(s3_emulator, BUCKET, {})
        tokens = nwm_cli._sample_tokens(s3_emulator, "d", "x")
        assert tokens == set(), f"expected empty set, got {tokens}"

    def test_config_directory_appends_mem1_for_ensembles(self):
        """An ensemble config maps to its `_mem1` directory; deterministic stays bare."""
        ensemble = SimpleNamespace(members=6)
        deterministic = SimpleNamespace(members=0)
        assert nwm_cli._config_directory(ensemble, "medium_range") == (
            "medium_range_mem1"
        ), "ensemble appends _mem1"
        assert (
            nwm_cli._config_directory(deterministic, "short_range") == "short_range"
        ), "deterministic stays bare"

    @pytest.mark.parametrize(
        "tokens, expected",
        [
            ({"channel_rt"}, True),
            ({"channel_rt_1"}, True),
            ({"channel_rt_12"}, True),
            ({"land", "reservoir"}, False),
            (set(), False),
        ],
    )
    def test_token_present_bare_and_ensemble_forms(self, tokens, expected):
        """A bare token or its `{token}_{member}` ensemble form counts as present."""
        assert nwm_cli._token_present("channel_rt", tokens) is expected, (
            f"_token_present('channel_rt', {tokens}) should be {expected}"
        )

    def test_validate_flags_empty_variables(self):
        """A product with an empty `variables` map is flagged by the offline lint."""
        catalog = SimpleNamespace(
            datasets={"bad": SimpleNamespace(s3_token="x", variables={})},
            configurations={},
        )
        checked, issues = nwm_cli.validator(catalog)
        assert checked == 1, "one product inspected"
        assert any("variables" in issue for issue in issues), "empty variables flagged"

    def test_validate_flags_unknown_product_in_configuration(self):
        """A configuration referencing an uncurated product key is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "chrtout": SimpleNamespace(
                    s3_token="channel_rt", variables={"streamflow": object()}
                )
            },
            configurations={
                "short_range": SimpleNamespace(products=["chrtout", "ghost"])
            },
        )
        _checked, issues = nwm_cli.validator(catalog)
        assert any(
            "ghost" in issue and "unknown product" in issue for issue in issues
        ), f"dangling product reference flagged: {issues}"

    def test_validate_clean_minimal_catalog(self):
        """A coherent minimal catalog produces no offline issues."""
        catalog = SimpleNamespace(
            datasets={
                "chrtout": SimpleNamespace(
                    s3_token="channel_rt", variables={"streamflow": object()}
                )
            },
            configurations={"short_range": SimpleNamespace(products=["chrtout"])},
        )
        checked, issues = nwm_cli.validator(catalog)
        assert checked == 1, f"coherent catalog is clean: {issues}"
        assert issues == [], f"coherent catalog is clean: {issues}"

    def test_live_flags_only_the_absent_product(self, monkeypatch):
        """`live_validator` flags exactly the product whose token no carrier serves."""
        catalog = SimpleNamespace(
            datasets={
                "a": SimpleNamespace(s3_token="ta"),
                "b": SimpleNamespace(s3_token="tb"),
            },
            configurations={"cfg": SimpleNamespace(members=0, products=["a", "b"])},
        )
        monkeypatch.setattr(nwm_cli, "_unsigned_client", lambda: object())
        monkeypatch.setattr(nwm_cli, "_latest_complete_day", lambda c: "d")
        monkeypatch.setattr(nwm_cli, "_sample_tokens", lambda c, d, dir_: {"ta"})
        checked, issues = nwm_cli.live_validator(catalog)
        assert checked == 2, "both products inspected"
        assert len(issues) == 1, f"only 'b' flagged: {issues}"
        assert "b" in issues[0], f"only 'b' flagged: {issues}"

    def test_live_ensemble_carrier_matches_member_token(self, monkeypatch):
        """An ensemble-only carrier's `{token}_{member}` file satisfies the check."""
        catalog = SimpleNamespace(
            datasets={"a": SimpleNamespace(s3_token="channel_rt")},
            configurations={"ens": SimpleNamespace(members=6, products=["a"])},
        )
        captured = {}

        def _sample(client, day, directory):
            """Record the sampled directory and return the member-suffixed token."""
            captured["dir"] = directory
            return {"channel_rt_1"}

        monkeypatch.setattr(nwm_cli, "_unsigned_client", lambda: object())
        monkeypatch.setattr(nwm_cli, "_latest_complete_day", lambda c: "d")
        monkeypatch.setattr(nwm_cli, "_sample_tokens", _sample)
        checked, issues = nwm_cli.live_validator(catalog)
        assert issues == [], "the ensemble member token satisfies the bare token"
        assert captured["dir"] == "ens_mem1", "the ensemble member-1 directory sampled"


class TestLiveValidatorViaFacade:
    """The live validator dispatched through `validate_one`."""

    def test_flags_token_absent_from_bucket(self, monkeypatch):
        """A product whose s3_token shows on no live carrier config is flagged."""
        monkeypatch.setattr(nwm_cli, "_unsigned_client", lambda: object())
        monkeypatch.setattr(nwm_cli, "_latest_complete_day", lambda c: "nwm.0")
        monkeypatch.setattr(nwm_cli, "_sample_tokens", lambda c, d, dir_: set())
        result = validate_one(_info(), live=True)
        assert result.status == "ok", "nwm live validator ran"
        assert result.issues, "an empty bucket flags every product token"
        assert all("s3_token" in issue for issue in result.issues), "token messages"

    def test_clean_when_tokens_present(self, monkeypatch):
        """Every product's token appearing under a carrier config clears live."""
        all_tokens = {
            product.s3_token for product in load_catalog(_info()).datasets.values()
        }
        monkeypatch.setattr(nwm_cli, "_unsigned_client", lambda: object())
        monkeypatch.setattr(nwm_cli, "_latest_complete_day", lambda c: "nwm.0")
        monkeypatch.setattr(
            nwm_cli, "_sample_tokens", lambda c, d, dir_: set(all_tokens)
        )
        result = validate_one(_info(), live=True)
        assert result.issues == [], "every token present under a carrier -> clean"
