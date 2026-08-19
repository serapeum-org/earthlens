"""Retry and error-signalling for a throttled CADS store (#1082, #1083)."""

from __future__ import annotations

import pytest
import requests

import earthlens.ecmwf.backend as backend_mod
from earthlens.ecmwf import CadsUnavailableError

pytestmark = [pytest.mark.ecmwf, pytest.mark.unit]

_THROTTLED = (
    "400 Client Error: Bad Request for url: https://ecds.ecmwf.int/api\n"
    "The job has been rejected\n"
    "Number queued requests for this dataset is temporarily limited."
)


class _Client:
    """A cdsapi stand-in that fails a given number of times, then succeeds."""

    def __init__(self, failures, exc=None):
        self.failures = failures
        self.exc = exc or requests.HTTPError(_THROTTLED)
        self.calls = 0

    def retrieve(self, dataset, request, target):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        open(target, "w").close()


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
        assert backend_mod._looks_like_throttled(Exception(message)) is expected


class TestStatusOf:
    """Tests for `_status_of`."""

    def test_reads_the_status_off_the_response(self):
        """A requests error carrying a response yields its status."""
        exc = requests.HTTPError("boom")
        exc.response = requests.Response()
        exc.response.status_code = 429
        assert backend_mod._status_of(exc) == 429

    def test_falls_back_to_the_message(self):
        """Without a response object the status is read from the text."""
        assert backend_mod._status_of(Exception("400 Client Error: nope")) == 400

    def test_returns_none_when_undiscernible(self):
        """A transport failure carries no status."""
        assert backend_mod._status_of(Exception("connection dropped")) is None


class TestRetrieveWithRetry:
    """Tests for `_retrieve_with_retry`."""

    def test_succeeds_without_retrying_when_the_store_is_healthy(self, tmp_path):
        """A first-attempt success calls retrieve exactly once."""
        client = _Client(failures=0)
        backend_mod._retrieve_with_retry(client, "ds", {}, tmp_path / "o.nc", "ecds")
        assert client.calls == 1

    def test_retries_a_throttled_retrieve_and_succeeds(self, tmp_path, monkeypatch):
        """A transient throttle is retried rather than surfaced."""
        monkeypatch.setattr(backend_mod.time, "sleep", lambda _s: None)
        client = _Client(failures=2)
        backend_mod._retrieve_with_retry(client, "ds", {}, tmp_path / "o.nc", "ecds")
        assert client.calls == 3

    def test_raises_typed_error_once_the_attempts_are_spent(
        self, tmp_path, monkeypatch
    ):
        """Persistent throttling raises `CadsUnavailableError`, not HTTPError."""
        monkeypatch.setattr(backend_mod.time, "sleep", lambda _s: None)
        client = _Client(failures=99)
        with pytest.raises(CadsUnavailableError) as excinfo:
            backend_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == backend_mod.CADS_MAX_ATTEMPTS
        assert excinfo.value.status_code == 400
        assert "temporary" in str(excinfo.value).lower()

    def test_backs_off_exponentially_between_attempts(self, tmp_path, monkeypatch):
        """Each retry waits twice as long as the one before it."""
        waits: list[float] = []
        monkeypatch.setattr(backend_mod.time, "sleep", waits.append)
        with pytest.raises(CadsUnavailableError):
            backend_mod._retrieve_with_retry(
                _Client(failures=99), "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert waits == [
            backend_mod.CADS_BACKOFF_SECONDS * 2**i
            for i in range(backend_mod.CADS_MAX_ATTEMPTS - 1)
        ]

    def test_a_bad_request_is_not_retried(self, tmp_path):
        """A genuine request error fails fast instead of burning attempts."""
        client = _Client(failures=99, exc=requests.HTTPError("400: bad 'variable'"))
        with pytest.raises(requests.HTTPError):
            backend_mod._retrieve_with_retry(
                client, "ds", {}, tmp_path / "o.nc", "ecds"
            )
        assert client.calls == 1

    def test_a_licence_refusal_becomes_a_permission_error(self, tmp_path):
        """An unaccepted licence is permanent, so it is not retried."""
        client = _Client(failures=99, exc=Exception("403: licence not accepted"))
        with pytest.raises(PermissionError, match="licence not accepted"):
            backend_mod._retrieve_with_retry(
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
