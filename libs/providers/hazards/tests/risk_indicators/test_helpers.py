"""Unit tests for `earthlens.risk_indicators._helpers` (queries + parsers)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.risk_indicators import _helpers

pytestmark = pytest.mark.risk_indicators

DATA = Path(__file__).parent / "data"


class _Recorder:
    """A requests.get stand-in that records its call and returns a payload."""

    def __init__(self, payload):
        self.payload = payload
        self.url = None
        self.params = None
        self.headers = None

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.url, self.params, self.headers = url, params, headers
        return _Resp(self.payload)


class _Resp:
    """A minimal requests.Response stand-in."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        """No-op for the 200 fixtures."""

    def json(self):
        """Return the canned payload."""
        return self._payload

    def close(self):
        """No-op close hook (HttpClient calls it on retry/errored responses)."""


def _load(name):
    """Read a captured JSON fixture by name."""
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class _FlakyGet:
    """A requests.get stand-in that raises a sequence of errors, then returns."""

    def __init__(self, errors, payload=None):
        self.errors = list(errors)
        self.payload = payload if payload is not None else {"data": []}
        self.attempts = 0

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        return _Resp(self.payload)


class _ReturnsResponse:
    """A requests.get stand-in that returns a fixed response and counts calls."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return self.response


class _HttpError(_Resp):
    """A response whose raise_for_status raises an HTTPError with a status."""

    def __init__(self, status_code):
        super().__init__({})
        self.status_code = status_code

    def raise_for_status(self):
        """Raise an HTTPError carrying this response (for status checks)."""
        err = _helpers.requests.HTTPError(f"HTTP {self.status_code}")
        err.response = self
        raise err


class TestRequestJsonRetry:
    """_request_json retries transient failures and fails fast on 4xx."""

    def test_retries_then_succeeds(self, monkeypatch):
        """A connection reset is retried and the next attempt's body returned."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        flaky = _FlakyGet([_helpers.requests.ConnectionError("reset")], {"data": [1]})
        monkeypatch.setattr(_helpers.requests, "get", flaky)
        out = _helpers.gfw_query("d", "v", "SELECT 1", api_key="k")
        assert out == {"data": [1]} and flaky.attempts == 2

    def test_gives_up_after_retries(self, monkeypatch):
        """A persistent reset raises after exhausting the retries."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        errs = [_helpers.requests.ConnectionError("reset")] * 5
        monkeypatch.setattr(_helpers.requests, "get", _FlakyGet(errs))
        with pytest.raises(_helpers.requests.ConnectionError):
            _helpers.thinkhazard_query("133")

    def test_4xx_fails_fast(self, monkeypatch):
        """A 404 is not retried — it raises on the first attempt."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        returns_404 = _ReturnsResponse(_HttpError(404))
        monkeypatch.setattr(_helpers.requests, "get", returns_404)
        with pytest.raises(_helpers.requests.HTTPError):
            _helpers.inform_query(505, "INFORM")
        assert returns_404.calls == 1

    def test_timeout_is_retried(self, monkeypatch):
        """A Timeout is transient and retried, then the next attempt succeeds."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        flaky = _FlakyGet([_helpers.requests.Timeout("slow")], {"data": [1]})
        monkeypatch.setattr(_helpers.requests, "get", flaky)
        assert _helpers.thinkhazard_query("133") is not None
        assert flaky.attempts == 2

    def test_chunked_encoding_error_is_retried(self, monkeypatch):
        """A mid-body ChunkedEncodingError (the GFW case) is retried."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        flaky = _FlakyGet(
            [_helpers.requests.exceptions.ChunkedEncodingError("mid-body")],
            {"data": [1]},
        )
        monkeypatch.setattr(_helpers.requests, "get", flaky)
        assert _helpers.gfw_query("d", "v", "SELECT 1", api_key="k") == {"data": [1]}
        assert flaky.attempts == 2

    def test_content_decoding_error_is_retried(self, monkeypatch):
        """A ContentDecodingError is transient and retried."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        flaky = _FlakyGet(
            [_helpers.requests.exceptions.ContentDecodingError("gzip")],
            {"data": [1]},
        )
        monkeypatch.setattr(_helpers.requests, "get", flaky)
        assert _helpers.gfw_query("d", "v", "SELECT 1", api_key="k") == {"data": [1]}
        assert flaky.attempts == 2

    def test_5xx_is_retried(self, monkeypatch):
        """A 5xx status is retried via the 500-599 forcelist."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        first_503 = _HttpError(503)
        second_ok = _Resp({"data": [1]})
        responses = iter([first_503, second_ok])
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            return next(responses)

        monkeypatch.setattr(_helpers.requests, "get", fake_get)
        assert _helpers.gfw_query("d", "v", "SELECT 1", api_key="k") == {"data": [1]}
        assert calls["n"] == 2

    def test_bare_request_exception_fails_fast(self, monkeypatch):
        """A bare RequestException (not in retry_on_exceptions) is not retried."""
        monkeypatch.setattr(_helpers.time, "sleep", lambda _s: None)
        flaky = _FlakyGet([_helpers.requests.RequestException("weird")], {"data": [1]})
        monkeypatch.setattr(_helpers.requests, "get", flaky)
        with pytest.raises(_helpers.requests.RequestException):
            _helpers.thinkhazard_query("133")
        assert flaky.attempts == 1


class TestThinkhazardQuery:
    """thinkhazard_query builds the public report URL, no key header."""

    def test_all_hazards_url(self, monkeypatch):
        """An all-hazards query targets /report/{code}.json with no key."""
        rec = _Recorder(_load("thinkhazard_report_133.json"))
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.thinkhazard_query("133")
        assert rec.url.endswith("/report/133.json")
        assert _helpers.GFW_KEY_HEADER not in rec.headers

    def test_single_hazard_url(self, monkeypatch):
        """A single-hazard query appends the hazard mnemonic."""
        rec = _Recorder(_load("thinkhazard_report_133_FL.json"))
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.thinkhazard_query("133", "FL")
        assert rec.url.endswith("/report/133/FL.json")


class TestInformQuery:
    """inform_query builds the Scores URL with the workflow + indicator."""

    def test_url_and_params(self, monkeypatch):
        """The query passes WorkflowId and IndicatorId, no key header."""
        rec = _Recorder(_load("inform_scores_INFORM.json"))
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.inform_query(505, "INFORM")
        assert rec.url.endswith("/countries/Scores/")
        assert rec.params == {"WorkflowId": 505, "IndicatorId": "INFORM"}
        assert _helpers.GFW_KEY_HEADER not in rec.headers


class TestGfwQuery:
    """gfw_query builds the query/json URL and attaches the key header."""

    def test_url_params_and_key_header(self, monkeypatch):
        """The query targets query/json with the SQL and x-api-key header."""
        rec = _Recorder(_load("gfw_tcl_iso_change_KEN.json"))
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.gfw_query("ds", "v1", "SELECT 1", api_key="secret-key")
        assert rec.url.endswith("/dataset/ds/v1/query/json")
        assert rec.params == {"sql": "SELECT 1"}
        assert rec.headers[_helpers.GFW_KEY_HEADER] == "secret-key"


class TestGfwGeostore:
    """gfw_geostore builds the admin geostore URL with the key header."""

    def test_country_url(self, monkeypatch):
        """A country geostore targets /geostore/admin/{iso} with the key."""
        rec = _Recorder({"data": {}})
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.gfw_geostore("KEN", api_key="k")
        assert rec.url.endswith("/geostore/admin/KEN")
        assert rec.headers[_helpers.GFW_KEY_HEADER] == "k"

    def test_subnational_segments(self, monkeypatch):
        """Extra admin segments extend the geostore path."""
        rec = _Recorder({"data": {}})
        monkeypatch.setattr(_helpers.requests, "get", rec)
        _helpers.gfw_geostore("KEN", api_key="k", admin=("1", "2"))
        assert rec.url.endswith("/geostore/admin/KEN/1/2")


class TestToFrame:
    """to_frame flattens the supported JSON row shapes."""

    def test_data_envelope(self):
        """A {data: [...]} envelope yields one row per record."""
        df = _helpers.to_frame(_load("gfw_tcl_iso_change_KEN.json"))
        assert isinstance(df, pd.DataFrame)
        assert "umd_tree_cover_loss__ha" in df.columns and len(df) == 6

    def test_bare_list(self):
        """A bare list of dicts becomes a frame."""
        df = _helpers.to_frame([{"a": 1}, {"a": 2}])
        assert list(df["a"]) == [1, 2]

    def test_single_dict(self):
        """A single dict becomes a one-row frame."""
        assert len(_helpers.to_frame({"a": 1})) == 1

    def test_empty_with_columns(self):
        """An empty payload with columns returns a typed empty frame."""
        df = _helpers.to_frame({"data": []}, columns=["x", "y"])
        assert list(df.columns) == ["x", "y"] and df.empty


class TestThinkhazardToFrame:
    """thinkhazard_to_frame flattens both report shapes."""

    def test_all_hazards(self):
        """The all-hazards list flattens to one row per hazard."""
        df = _helpers.thinkhazard_to_frame(
            _load("thinkhazard_report_133.json"), admin_code="133", country="KEN"
        )
        assert list(df.columns) == _helpers.THINKHAZARD_COLUMNS
        assert len(df) == 11
        assert df.iloc[0]["hazard"] == "FL" and df.iloc[0]["country"] == "KEN"

    def test_single_hazard(self):
        """The single-hazard report flattens to one row with the level."""
        df = _helpers.thinkhazard_to_frame(
            _load("thinkhazard_report_133_FL.json"),
            admin_code="133",
            hazard="FL",
            country="KEN",
        )
        assert len(df) == 1
        assert df.iloc[0]["hazard"] == "FL"
        assert df.iloc[0]["hazard_type"] == "River flood"
        assert df.iloc[0]["level"] == "HIG"
        assert df.iloc[0]["level_title"] == "High"


class TestInformToFrame:
    """inform_to_frame renames columns and filters by country."""

    def test_filter_to_country(self):
        """Filtering to KEN keeps a single row with its score."""
        df = _helpers.inform_to_frame(_load("inform_scores_INFORM.json"), country="KEN")
        assert list(df.columns) == _helpers.INFORM_COLUMNS
        assert len(df) == 1 and df.iloc[0]["iso3"] == "KEN"

    def test_no_filter_keeps_all(self):
        """Without a country filter every row is kept."""
        rows = _load("inform_scores_INFORM.json")
        df = _helpers.inform_to_frame(rows)
        assert len(df) == len(rows)

    def test_empty_payload(self):
        """An empty payload yields a typed empty frame."""
        df = _helpers.inform_to_frame([])
        assert df.empty and list(df.columns) == _helpers.INFORM_COLUMNS


class TestToFeatureCollection:
    """to_feature_collection wraps GeoJSON into a pyramids collection."""

    def test_populated(self):
        """A populated GeoJSON yields a CRS-tagged FeatureCollection."""
        fc = _helpers.to_feature_collection(_load("gfw_geostore_admin_KEN.json"))
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 1 and fc.crs.to_epsg() == 4326

    def test_empty_features(self):
        """An empty features list yields an empty collection."""
        fc = _helpers.to_feature_collection(
            {"type": "FeatureCollection", "features": []}
        )
        assert len(fc) == 0 and fc.crs.to_epsg() == 4326

    def test_missing_features_key_raises(self):
        """A mapping with no features key is rejected."""
        with pytest.raises(ValueError, match="'features' key"):
            _helpers.to_feature_collection({"type": "x"})


class TestGfwGeostoreToFeatureCollection:
    """gfw_geostore_to_feature_collection digs the geostore payload safely."""

    def test_extracts_geojson(self):
        """A well-formed geostore payload yields the boundary collection."""
        payload = {
            "data": {"attributes": {"geojson": _load("gfw_geostore_admin_KEN.json")}}
        }
        fc = _helpers.gfw_geostore_to_feature_collection(payload)
        assert isinstance(fc, FeatureCollection) and len(fc) == 1

    @pytest.mark.parametrize(
        "payload", [{}, {"data": {}}, {"data": {"attributes": {}}}]
    )
    def test_malformed_payload_raises_clear_error(self, payload):
        """A payload missing data.attributes.geojson raises a descriptive error."""
        with pytest.raises(ValueError, match="data.attributes.geojson"):
            _helpers.gfw_geostore_to_feature_collection(payload)


class TestResolveAdminAndEmpty:
    """The module-level resolve_admin and empty_canonical helpers."""

    def test_resolve_admin(self):
        """resolve_admin loads the catalog and maps Kenya to 133."""
        assert _helpers.resolve_admin("KEN", 0) == "133"

    def test_empty_canonical(self):
        """empty_canonical returns a zero-row frame with the given columns."""
        df = _helpers.empty_canonical(["a", "b"])
        assert df.empty and list(df.columns) == ["a", "b"]
