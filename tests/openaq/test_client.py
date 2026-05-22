"""Unit tests for `earthlens.openaq.client` (pagination + back-off)."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from earthlens.openaq.client import BASE_URL, OpenaqClient, _parse_retry_after


class _Resp:
    """Canned response with json/raise_for_status/status/headers."""

    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _SeqSession:
    """Returns queued responses in order, recording each GET's args."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Resp:
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self._responses.pop(0)


def _client(responses: list[_Resp], **kwargs: Any) -> tuple[OpenaqClient, _SeqSession]:
    """Build a client over a sequenced fake session with no real sleeps."""
    session = _SeqSession(responses)
    waits: list[float] = []
    client = OpenaqClient(
        "key", session=session, sleep=waits.append, **kwargs
    )
    client._waits = waits  # type: ignore[attr-defined]
    return client, session


@pytest.mark.openaq
class TestParseRetryAfter:
    """Retry-After header parsing."""

    @pytest.mark.parametrize(
        "value, expected",
        [("5", 5.0), ("0", 0.0), (None, None), ("soon", None), ("", None)],
    )
    def test_parse(self, value: str | None, expected: float | None):
        """Numeric values parse to seconds; missing/junk yield None."""
        assert _parse_retry_after(value) == expected


@pytest.mark.openaq
class TestRequest:
    """The single-page request with 429 handling."""

    def test_attaches_api_key_header(self):
        """Every request carries the X-API-Key header."""
        client, session = _client([_Resp({"results": []})])
        client._request("locations", {"limit": 10})
        assert session.calls[0]["headers"]["X-API-Key"] == "key"
        assert session.calls[0]["url"] == f"{BASE_URL}/locations"

    def test_429_then_success(self):
        """A 429 with Retry-After is retried, then succeeds."""
        client, session = _client(
            [
                _Resp({}, 429, {"Retry-After": "0"}),
                _Resp({"results": [{"a": 1}]}),
            ]
        )
        payload = client._request("locations", {"limit": 10})
        assert payload == {"results": [{"a": 1}]}
        assert len(session.calls) == 2

    def test_429_without_retry_after_uses_backoff(self):
        """A 429 with no Retry-After falls back to exponential back-off."""
        client, session = _client(
            [_Resp({}, 429), _Resp({"results": []})], backoff_factor=2.0
        )
        client._request("locations", {})
        assert client._waits == [2.0]  # type: ignore[attr-defined]

    def test_raises_after_max_retries(self):
        """Exhausted 429s raise the final HTTPError."""
        client, _ = _client([_Resp({}, 429, {"Retry-After": "0"})] * 3, max_retries=2)
        with pytest.raises(requests.HTTPError):
            client._request("locations", {})

    def test_non_429_error_raises_immediately(self):
        """A non-429 error status raises without retrying."""
        client, session = _client([_Resp({}, 500)])
        with pytest.raises(requests.HTTPError):
            client._request("locations", {})
        assert len(session.calls) == 1


@pytest.mark.openaq
class TestPaginate:
    """Multi-page iteration and capping."""

    def test_walks_until_short_page(self):
        """Pagination stops when a page returns fewer than `limit` rows."""
        client, _ = _client(
            [
                _Resp({"results": [{"i": 1}, {"i": 2}]}),
                _Resp({"results": [{"i": 3}]}),
            ]
        )
        items = list(client.paginate("locations", {"limit": 2}))
        assert [item["i"] for item in items] == [1, 2, 3]

    def test_max_items_caps(self):
        """max_items stops iteration early mid-page."""
        client, _ = _client([_Resp({"results": [{"i": 1}, {"i": 2}, {"i": 3}]})])
        items = list(client.paginate("locations", {"limit": 10}, max_items=2))
        assert len(items) == 2

    def test_empty_results_terminate(self):
        """An empty first page yields nothing and stops."""
        client, _ = _client([_Resp({"results": []})])
        assert list(client.paginate("locations", {"limit": 10})) == []


@pytest.mark.openaq
class TestListEndpoints:
    """The locations and measurements convenience wrappers."""

    def test_list_locations_forwards_bbox_and_ids(self):
        """list_locations forwards bbox + parameters_id to the query."""
        client, session = _client([_Resp({"results": [{"id": 1}]})])
        client.list_locations(bbox="1,2,3,4", parameters_id=[2, 5], limit=100)
        params = session.calls[0]["params"]
        assert params["bbox"] == "1,2,3,4"
        assert params["parameters_id"] == [2, 5]

    def test_list_locations_omits_empty_ids(self):
        """No parameters_id key is sent when the id list is empty."""
        client, session = _client([_Resp({"results": []})])
        client.list_locations(bbox="1,2,3,4", parameters_id=[], limit=100)
        assert "parameters_id" not in session.calls[0]["params"]

    def test_list_measurements_raw_path_uses_datetime_filter(self):
        """A None rollup hits /measurements and filters on datetime_from/to."""
        client, session = _client([_Resp({"results": []})])
        client.list_measurements(
            sensor_id="10",
            datetime_from="2024-01-01T00:00:00",
            datetime_to="2024-01-07T00:00:00",
            rollup=None,
        )
        params = session.calls[0]["params"]
        assert session.calls[0]["url"] == f"{BASE_URL}/sensors/10/measurements"
        assert params["datetime_from"] == "2024-01-01T00:00:00"
        assert "date_from" not in params

    def test_list_measurements_hours_uses_datetime_filter(self):
        """The sub-daily /hours rollup also filters on datetime_from/to."""
        client, session = _client([_Resp({"results": []})])
        client.list_measurements(
            sensor_id="10",
            datetime_from="2024-01-01T00:00:00",
            datetime_to="2024-01-07T00:00:00",
            rollup="hours",
        )
        params = session.calls[0]["params"]
        assert session.calls[0]["url"] == f"{BASE_URL}/sensors/10/hours"
        assert params["datetime_from"] == "2024-01-01T00:00:00"

    @pytest.mark.parametrize("rollup", ["days", "months", "years"])
    def test_list_measurements_date_rollups_use_date_filter(self, rollup: str):
        """The /days, /months, /years rollups filter on a calendar date_from/to."""
        client, session = _client([_Resp({"results": []})])
        client.list_measurements(
            sensor_id="10",
            datetime_from="2024-01-01T00:00:00",
            datetime_to="2024-01-07T00:00:00",
            rollup=rollup,
        )
        params = session.calls[0]["params"]
        assert session.calls[0]["url"] == f"{BASE_URL}/sensors/10/{rollup}"
        assert params["date_from"] == "2024-01-01", "date should be truncated to YYYY-MM-DD"
        assert params["date_to"] == "2024-01-07"
        assert "datetime_from" not in params
