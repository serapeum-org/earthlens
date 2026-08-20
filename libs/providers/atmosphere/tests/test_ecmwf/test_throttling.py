"""Retry and error-signalling for a throttled CADS store (#1082, #1083)."""

from __future__ import annotations

import pathlib

import pytest
import requests

import earthlens.ecmwf._helpers as helpers_mod
import earthlens.ecmwf.backend as backend_mod
from earthlens.ecmwf import CadsUnavailableError

pytestmark = [pytest.mark.ecmwf, pytest.mark.unit]

_THROTTLED = (
    "400 Client Error: Bad Request for url: https://ecds.ecmwf.int/api\n"
    "The job has been rejected\n"
    "Number queued requests for this dataset is temporarily limited."
)


def _raise(exc: BaseException) -> None:
    """Raise `exc` — a `lambda` cannot, and the repo does not nest defs."""
    raise exc


class _Client:
    """A cdsapi stand-in that fails a given number of times, then succeeds."""

    def __init__(self, failures, exc=None, payload=b""):
        self.failures = failures
        self.exc = exc or requests.HTTPError(_THROTTLED)
        self.payload = payload
        self.calls = 0

    def retrieve(self, dataset, request, target):
        self.calls += 1
        if self.calls <= self.failures:
            # A real client can leave a partial file behind before failing.
            pathlib.Path(target).write_bytes(b"partial")
            raise self.exc
        if self.payload is not None:
            pathlib.Path(target).write_bytes(self.payload)


class TestLooksLikeThrottled:
    """Tests for `_looks_like_throttled`."""

    @pytest.mark.parametrize(
        "message, expected",
        [
            (_THROTTLED, True),
            ("The job has been rejected. Number queued requests is limited", True),
            ("400 Client Error: invalid value 'foo' for 'variable'", False),
            ("403 Forbidden: licence not accepted", False),
            ("", False),
        ],
    )
    def test_classifies_only_queue_limit_refusals(self, message, expected):
        """Only a queue-limit refusal is throttling; a bad request is not."""
        assert helpers_mod._looks_like_throttled(Exception(message)) is expected


class TestStatusOf:
    """Tests for `_status_of`."""

    def test_reads_the_status_off_the_response(self):
        """A requests error carrying a response yields its status."""
        exc = requests.HTTPError("boom")
        exc.response = requests.Response()
        exc.response.status_code = 429
        assert helpers_mod._status_of(exc) == 429

    def test_falls_back_to_the_message(self):
        """Without a response object the status is read from the text."""
        assert helpers_mod._status_of(Exception("400 Client Error: nope")) == 400

    def test_returns_none_when_undiscernible(self):
        """A transport failure carries no status."""
        assert helpers_mod._status_of(Exception("connection dropped")) is None


class TestRetrieveWithRetry:
    """Tests for `_retrieve_with_retry`."""

    def test_succeeds_without_retrying_when_the_store_is_healthy(self, tmp_path):
        """A first-attempt success calls retrieve exactly once."""
        client = _Client(failures=0)
        helpers_mod._retrieve_with_retry(client, "ds", {}, tmp_path / "o.nc", "ecds")
        assert client.calls == 1

    def test_retries_a_throttled_retrieve_and_succeeds(self, tmp_path, monkeypatch):
        """A transient throttle is retried rather than surfaced."""
        monkeypatch.setattr(helpers_mod.time, "sleep", lambda _s: None)
        client = _Client(failures=2)
        helpers_mod._retrieve_with_retry(client, "ds", {}, tmp_path / "o.nc", "ecds")
        assert client.calls == 3

    def test_raises_typed_error_once_the_attempts_are_spent(
        self, tmp_path, monkeypatch
    ):
        """Persistent throttling raises `CadsUnavailableError`, not HTTPError."""
        monkeypatch.setattr(helpers_mod.time, "sleep", lambda _s: None)
        client = _Client(failures=99)
        with pytest.raises(CadsUnavailableError) as excinfo:
            helpers_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == helpers_mod.CADS_MAX_ATTEMPTS
        assert excinfo.value.status_code == 400
        assert "temporary" in str(excinfo.value).lower()

    def test_backs_off_exponentially_between_attempts(self, tmp_path, monkeypatch):
        """Each retry waits twice as long as the one before it."""
        waits: list[float] = []
        monkeypatch.setattr(helpers_mod.time, "sleep", waits.append)
        client = _Client(failures=99)
        with pytest.raises(CadsUnavailableError):
            helpers_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert waits == [
            helpers_mod.CADS_BACKOFF_SECONDS * 2**i
            for i in range(helpers_mod.CADS_MAX_ATTEMPTS - 1)
        ]

    def test_a_bad_request_is_not_retried(self, tmp_path):
        """A genuine request error fails fast instead of burning attempts."""
        client = _Client(failures=99, exc=requests.HTTPError("400: bad 'variable'"))
        with pytest.raises(requests.HTTPError):
            helpers_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == 1

    def test_a_licence_refusal_becomes_a_permission_error(self, tmp_path):
        """An unaccepted licence is permanent, so it is not retried."""
        client = _Client(failures=99, exc=Exception("403: licence not accepted"))
        with pytest.raises(PermissionError, match="licence not accepted"):
            helpers_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == 1


class TestServiceRefusalIsNotAnEmptyResult:
    """A store-level refusal must not be absorbed by `errors="warn"` (#1083)."""

    def test_fatal_class_propagates_under_warn(self):
        """`fatal=` overrides the warn policy, so the cause reaches the caller."""
        source = backend_mod.ECMWF.__new__(backend_mod.ECMWF)

        def _boom(_item):
            raise CadsUnavailableError("ECDS refused every job", status_code=400)

        with pytest.raises(CadsUnavailableError, match="refused every job"):
            source._run_items(
                ["a", "b"],
                _boom,
                errors="warn",
                label="variable",
                fatal=(CadsUnavailableError,),
            )

    def test_an_ordinary_failure_still_warns_and_continues(self):
        """A per-variable data gap keeps the documented warn behaviour."""
        source = backend_mod.ECMWF.__new__(backend_mod.ECMWF)

        def _one_bad(item):
            if item == "b":
                raise ValueError("no data for this variable")
            return item

        results, failures = source._run_items(
            ["a", "b", "c"],
            _one_bad,
            errors="warn",
            label="variable",
            fatal=(CadsUnavailableError,),
        )
        assert results == ["a", "c"]
        assert len(failures) == 1

    def test_without_fatal_the_refusal_would_be_swallowed(self):
        """Documents the old behaviour the fatal= hatch exists to prevent."""
        source = backend_mod.ECMWF.__new__(backend_mod.ECMWF)

        def _boom(_item):
            raise CadsUnavailableError("ECDS refused every job")

        results, failures = source._run_items(
            ["a", "b"], _boom, errors="warn", label="variable"
        )
        assert results == []
        assert len(failures) == 2


class TestHydratorDoesNotPairPseudoSlugs:
    """The bulk fill must not invent an `nc_variable` (review H3)."""

    @pytest.mark.parametrize("variable", ["num_covered_hours", "some_variable"])
    def test_the_all_pseudo_slug_is_never_paired(self, variable):
        """`all` names every variable, so it can never identify one.

        This is the pairing that put a coverage counter on a precipitation CDR:
        `all` is always the lone unmatched slug, so rule 4 matched it to
        whatever single variable survived the auxiliary filter.
        """
        from earthlens.ecmwf._hydrate import _match_variables

        assert _match_variables(["all"], {variable: {"units": "x"}}) == {}

    def test_a_coverage_counter_is_auxiliary(self):
        """`num_covered_hours` is a count, never a science variable."""
        from earthlens.ecmwf._hydrate import _is_auxiliary

        assert _is_auxiliary("num_covered_hours") is True

    @pytest.mark.parametrize("name", ["precipitation", "number_of_wet_days"])
    def test_a_real_variable_is_not_auxiliary(self, name):
        """The filter must not swallow genuine data variables."""
        from earthlens.ecmwf._hydrate import _is_auxiliary

        assert _is_auxiliary(name) is False

    def test_an_abbreviation_still_pairs(self):
        """Rule 4 earns its keep on the abbreviations it gets right."""
        from earthlens.ecmwf._hydrate import _match_variables

        assert _match_variables(["2m-temperature"], {"t2m": {"units": "K"}}) == {
            "2m-temperature": ("t2m", "K")
        }


class TestDownloadWiresTheFatalHatch:
    """`download()` must pass `fatal=`, not merely support it (review M3)."""

    @staticmethod
    def _lens(tmp_path):
        """An ECMWF whose one pair resolves without touching the network."""
        source = backend_mod.ECMWF.__new__(backend_mod.ECMWF)
        source.vars = {"tigge-forecasts": ["2m-temperature"]}
        source.root_dir = tmp_path
        return source

    @pytest.mark.parametrize("policy", ["warn", "ignore", "skip"])
    def test_a_refusal_propagates_through_download(self, tmp_path, monkeypatch, policy):
        """Every non-raise policy still surfaces a store refusal."""
        source = self._lens(tmp_path)
        monkeypatch.setattr(
            backend_mod.ECMWF,
            "_download_pair",
            lambda self, pair, **kw: _raise(
                CadsUnavailableError("ECDS refused every job", status_code=400)
            ),
        )
        # The policy must be passed in: download() re-derives `self._errors`
        # from its own argument, so seeding the attribute is a no-op and the
        # parametrisation would silently run "warn" three times.
        with pytest.raises(CadsUnavailableError, match="refused every job"):
            source.download(progress_bar=False, errors=policy)

    def test_an_ordinary_failure_is_still_absorbed_by_download(
        self, tmp_path, monkeypatch
    ):
        """A per-variable gap keeps returning partial results, not raising."""
        source = self._lens(tmp_path)
        monkeypatch.setattr(
            backend_mod.ECMWF,
            "_download_pair",
            lambda self, pair, **kw: _raise(ValueError("no data")),
        )
        assert source.download(progress_bar=False) == []


class TestExtractedHelpers:
    """Direct cover for the two helpers extracted for testability (review N3)."""

    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({"hyear", "hmonth"}, "seasonal_hindcast"),
            ({"hyear", "hmonth", "hday", "year", "month", "day"}, "s2s_reforecast"),
            ({"hyear", "hmonth", "hday"}, "glofas_hindcast"),
        ],
    )
    def test_hindcast_kind_covers_all_three_shapes(self, fields, expected):
        """No `hday` is seasonal; `hday` with `day` pairs; `hday` alone renames."""
        from earthlens.ecmwf.cli import _hindcast_request_kind

        assert _hindcast_request_kind(fields) == expected

    def test_from_info_reads_named_bands_with_metadata(self):
        """A band with a NETCDF_VARNAME and units is extracted."""
        from earthlens.ecmwf.cli import _from_info

        info = {
            "bands": [
                {
                    "metadata": {
                        "": {
                            "NETCDF_VARNAME": "t2m",
                            "long_name": "2 metre temperature",
                            "units": "K",
                        }
                    }
                }
            ]
        }
        assert _from_info(info) == {
            "t2m": {"long_name": "2 metre temperature", "units": "K"}
        }

    @pytest.mark.parametrize(
        "bands",
        [
            [],
            [{"metadata": {"": {"NETCDF_VARNAME": "t2m"}}}],
            [{"metadata": {"": {"units": "K"}}}],
        ],
        ids=["no-bands", "no-metadata", "no-name"],
    )
    def test_from_info_skips_bands_it_cannot_describe(self, bands):
        """A band with no name, or no long_name and no units, is dropped."""
        from earthlens.ecmwf.cli import _from_info

        assert _from_info({"bands": bands}) == {}


class TestClassifierRefusesToSniffOurOwnErrors:
    """A ValueError / AssertionError is never a throttle (review L8)."""

    @pytest.mark.parametrize("exc_type", [ValueError, AssertionError])
    def test_own_errors_are_never_throttling(self, exc_type):
        """Even wording that would otherwise match must not be retried."""
        exc = exc_type("the job has been rejected: queued requests temporarily limited")
        assert helpers_mod._looks_like_throttled(exc) is False

    def test_such_an_error_is_not_retried(self, tmp_path):
        """A ValueError carrying throttle wording still fails on attempt one."""
        client = _Client(
            failures=99, exc=ValueError("queued requests temporarily limited")
        )
        with pytest.raises(ValueError):
            helpers_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == 1


class TestEndpointResolution:
    """`_endpoint_for` covers curated, indexed and unknown ids (review M4)."""

    def test_a_curated_id_uses_its_rows_endpoint(self):
        """A curated row's own `endpoint` is authoritative."""
        from earthlens.ecmwf.cli import _endpoint_for

        assert _endpoint_for("tigge-forecasts") == "ecds"

    def test_an_indexed_but_uncurated_id_resolves_from_the_index(self):
        """The path `curate` depends on: no row, but the index knows the store."""
        from earthlens.ecmwf import Catalog
        from earthlens.ecmwf.cli import _endpoint_for

        catalog = Catalog()
        uncurated = next(
            ident
            for ident in catalog.available_datasets
            if ident not in catalog.datasets
        )
        assert _endpoint_for(uncurated) == catalog.store_for(uncurated)

    def test_an_unknown_id_warns_and_defaults_to_cds(self):
        """The fallback is loud, so a stale index is visible."""
        from loguru import logger

        from earthlens.ecmwf.cli import _endpoint_for

        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING")
        try:
            assert _endpoint_for("definitely-not-a-dataset") == "cds"
        finally:
            logger.remove(sink)
        assert any("definitely-not-a-dataset" in m for m in messages)
        assert any("refresh ecmwf" in m for m in messages)


class TestRetrieveIsAtomic:
    """The `.part` write must land, clean up, and never expose a stale file."""

    def test_the_bytes_land_at_the_target(self, tmp_path):
        """A successful retrieve moves the sidecar onto the target."""
        target = tmp_path / "out.nc"
        client = _Client(failures=0, payload=b"fresh")
        helpers_mod._retrieve_with_retry(client, "ds", {}, target, "ecds")
        assert target.read_bytes() == b"fresh"

    def test_no_part_file_is_left_behind(self, tmp_path):
        """The sidecar is gone once the retrieve succeeds."""
        target = tmp_path / "out.nc"
        helpers_mod._retrieve_with_retry(_Client(failures=0), "ds", {}, target, "ecds")
        assert not (tmp_path / "out.nc.part").exists()

    def test_a_failed_retrieve_leaves_a_pre_existing_file_intact(
        self, tmp_path, monkeypatch
    ):
        """An exhausted retry must not truncate the good file already there."""
        monkeypatch.setattr(helpers_mod.time, "sleep", lambda _s: None)
        target = tmp_path / "out.nc"
        target.write_bytes(b"previous good download")
        client = _Client(failures=99)
        with pytest.raises(CadsUnavailableError):
            helpers_mod._retrieve_with_retry(client, "ds", {}, target, "ecds")
        assert target.read_bytes() == b"previous good download"
        assert not (tmp_path / "out.nc.part").exists()

    def test_a_retrieve_that_writes_nothing_is_a_failure(self, tmp_path):
        """Reporting success would hand back whatever stale file was there."""
        target = tmp_path / "out.nc"
        target.write_bytes(b"stale")
        client = _Client(failures=0, payload=None)
        with pytest.raises(CadsUnavailableError, match="wrote no file"):
            helpers_mod._retrieve_with_retry(client, "ds", {}, target, "ecds")
        assert target.read_bytes() == b"stale"

    def test_a_retry_overwrites_the_previous_partial(self, tmp_path):
        """The second attempt's bytes win, not the first attempt's stub."""
        target = tmp_path / "out.nc"
        client = _Client(failures=1, payload=b"second attempt")
        helpers_mod._retrieve_with_retry(client, "ds", {}, target, "ecds")
        assert target.read_bytes() == b"second attempt"


class TestChainWalkSkipsNonExceptions:
    """The chain yields only `Exception` links, but still walks past others."""

    def test_a_status_below_a_base_exception_is_still_found(self):
        """A KeyboardInterrupt in the chain must not hide the status under it."""
        try:
            try:
                try:
                    response = requests.Response()
                    response.status_code = 503
                    raise requests.HTTPError("503 Server Error", response=response)
                except requests.HTTPError as inner:
                    raise KeyboardInterrupt from inner
            except KeyboardInterrupt as interrupt:
                raise RuntimeError("wrapped") from interrupt
        except RuntimeError as exc:
            assert helpers_mod._status_of(exc) == 503

    def test_a_base_exception_contributes_no_status(self):
        """A bare KeyboardInterrupt carries nothing to read a status from."""
        assert helpers_mod._status_of(KeyboardInterrupt()) is None
