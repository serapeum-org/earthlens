"""Tests for `AirnowClient` request shaping and back-off."""

from __future__ import annotations

import pytest
import requests

from earthlens.airnow.client import AirnowClient
from .conftest import _FakeAirnow, _FakeResponse, _FakeSession


@pytest.mark.airnow
def test_client_exposes_retry_config():
    """The client surfaces its retry config from the underlying HttpClient."""
    client = AirnowClient("k", max_retries=7, backoff_factor=2.0, timeout=30.0)
    assert (client.max_retries, client.backoff_factor, client.timeout) == (7, 2.0, 30.0)


@pytest.mark.airnow
class TestGetData:
    """The single `get_data` call."""

    def test_injects_key_and_format(self):
        """`API_KEY` and JSON `format` are added to the query."""
        state = _FakeAirnow()
        client = AirnowClient("secret", session=_FakeSession(state))
        client.get_data({"BBOX": "a"})
        assert state.calls[0]["API_KEY"] == "secret"
        assert state.calls[0]["format"] == "application/json"

    def test_returns_rows(self):
        """A JSON array response is returned verbatim."""
        state = _FakeAirnow()
        client = AirnowClient("k", session=_FakeSession(state))
        rows = client.get_data({})
        assert rows == state.rows

    def test_non_list_payload_becomes_empty(self):
        """A non-list JSON body yields an empty list."""

        class _S:
            def get(self, *a, **k):
                return _FakeResponse({"error": "x"})

        client = AirnowClient("k", session=_S())
        assert client.get_data({}) == []

    def test_backoff_on_429_then_success(self):
        """A `429` with `Retry-After` is retried, then the data returns."""
        state = _FakeAirnow()
        state.n_429 = 2
        waits: list[float] = []
        client = AirnowClient(
            "k", session=_FakeSession(state), sleep=waits.append, backoff_factor=1.0
        )
        rows = client.get_data({})
        assert rows == state.rows
        assert len(waits) == 2

    def test_persistent_429_raises(self):
        """A `429` past `max_retries` propagates the HTTP error."""

        class _S:
            def get(self, *a, **k):
                return _FakeResponse({}, status_code=429, headers={"Retry-After": "0"})

        client = AirnowClient("k", session=_S(), max_retries=1, sleep=lambda s: None)
        with pytest.raises(requests.HTTPError):
            client.get_data({})

    def test_non_429_error_raises_immediately(self):
        """A non-`429` error status raises without retry."""

        class _S:
            def __init__(self):
                self.n = 0

            def get(self, *a, **k):
                self.n += 1
                return _FakeResponse({}, status_code=500)

        session = _S()
        client = AirnowClient("k", session=session, sleep=lambda s: None)
        with pytest.raises(requests.HTTPError):
            client.get_data({})
        assert session.n == 1
