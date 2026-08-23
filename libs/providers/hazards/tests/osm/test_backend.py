"""Unit + integration tests for the OSM backend (faked overpy / ohsome)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from earthlens.osm import OSM
from earthlens.osm._helpers import LicenseWarning

pytestmark = pytest.mark.osm


class TestConstruction:
    """Constructor validation and normalisation."""

    def test_string_variables_wrapped(self, osm_kwargs):
        """A bare string `variables` is wrapped into a one-element list."""
        osm = OSM(**{**osm_kwargs(), "variables": "overpass:hospitals"})
        assert osm.vars == ["overpass:hospitals"]

    def test_mapping_variables_rejected(self, osm_kwargs):
        """A mapping `variables` raises TypeError (this backend takes a list)."""
        with pytest.raises(TypeError, match="must be a list"):
            OSM(**{**osm_kwargs(), "variables": {"overpass:hospitals": []}})

    def test_empty_variables_rejected(self, osm_kwargs):
        """An empty `variables` raises ValueError."""
        with pytest.raises(ValueError, match="is empty"):
            OSM(**{**osm_kwargs(), "variables": []})

    def test_bad_file_format_rejected(self, osm_kwargs):
        """An unsupported file_format raises ValueError."""
        with pytest.raises(ValueError, match="file_format"):
            OSM(**{**osm_kwargs(), "file_format": "shp"})

    def test_output_kind_is_vector(self, osm_kwargs):
        """The backend declares vector output."""
        assert OSM(**osm_kwargs()).OUTPUT_KIND == "vector"

    def test_temporal_resolution_forced_all(self, osm_kwargs):
        """temporal_resolution is pinned to the 'all' sentinel."""
        assert (
            OSM(**{**osm_kwargs(), "temporal_resolution": "daily"}).temporal_resolution
            == "all"
        )


class TestOverpassRoute:
    """The Overpass branch: POST the QL, parse, build geometry."""

    def test_posts_ql_with_user_agent(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """The QL is POSTed to the endpoint with a non-empty User-Agent."""
        OSM(**osm_kwargs()).download()
        call = fake_overpass_post.calls[0]
        assert call["url"].endswith("/api/interpreter")
        assert call["headers"]["User-Agent"]
        assert "amenity" in call["data"]["data"]

    def test_bbox_in_swne_order(self, osm_kwargs, fake_overpy, fake_overpass_post):
        """The QL bbox is filled in Overpass order S,W,N,E."""
        OSM(**osm_kwargs()).download()
        ql = fake_overpass_post.calls[0]["data"]["data"]
        assert "(49.4,8.67,49.42,8.71)" in ql

    def test_returns_feature_collection(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """The parsed result becomes a FeatureCollection in EPSG:4326."""
        fc = OSM(**osm_kwargs()).download()
        assert fc.crs.to_epsg() == 4326
        assert sorted(fc.geometry.geom_type) == ["LineString", "Point", "Polygon"]

    def test_raw_query_override(self, osm_kwargs, fake_overpy, fake_overpass_post):
        """A raw query= override replaces the catalog QL template."""
        raw = "[out:json];(node({bbox}););out geom;"
        OSM(**{**osm_kwargs(), "query": raw}).download()
        ql = fake_overpass_post.calls[0]["data"]["data"]
        assert ql.startswith("[out:json];(node(49.4,8.67")

    def test_http_error_propagates(self, osm_kwargs, fake_overpy, fake_overpass_post):
        """A non-2xx Overpass status propagates (not silently swallowed)."""
        fake_overpass_post.ok = False
        with pytest.raises(Exception):
            OSM(**osm_kwargs()).download()

    def test_raw_query_without_bbox_sent_verbatim(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """A raw query with no {bbox} placeholder is POSTed unchanged."""
        raw = "[out:json];node(1);out;"
        OSM(**{**osm_kwargs(), "query": raw}).download()
        assert fake_overpass_post.calls[0]["data"]["data"] == raw

    def test_raw_query_with_regex_brace_quantifier(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """A raw query mixing {bbox} with a regex brace-quantifier does not crash."""
        raw = '[out:json];(node["name"~"^A.{2,5}$"]({bbox}););out geom;'
        OSM(**{**osm_kwargs(), "query": raw}).download()
        sent = fake_overpass_post.calls[0]["data"]["data"]
        assert "{2,5}" in sent  # the quantifier survives untouched
        assert "(49.4,8.67,49.42,8.71)" in sent  # the bbox was substituted


class TestOhsomeRoute:
    """The ohsome branch: post(bboxes, time, filter) -> as_dataframe."""

    def test_bbox_in_wsen_order_and_filter(self, osm_kwargs, fake_ohsome):
        """ohsome is called with bbox W,S,E,N and the catalog filter."""
        OSM(
            **{
                **osm_kwargs(),
                "variables": ["ohsome:buildings"],
                "start": "2020-01-01",
                "end": "2021-01-01",
            }
        ).download()
        kwargs = fake_ohsome.post_kwargs
        assert kwargs["bboxes"] == "8.67,49.4,8.71,49.42"
        assert kwargs["filter"] == "building=* and geometry:polygon"

    def test_time_range_built_from_window(self, osm_kwargs, fake_ohsome):
        """A start+end window becomes a 'start/end' ohsome time."""
        OSM(
            **{
                **osm_kwargs(),
                "variables": ["ohsome:buildings"],
                "start": "2018-01-01",
                "end": "2020-01-01",
            }
        ).download()
        assert fake_ohsome.post_kwargs["time"] == "2018-01-01/2020-01-01"

    def test_single_snapshot_time(self, osm_kwargs, fake_ohsome):
        """A start with no end becomes a single-snapshot ohsome time."""
        OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-06-01"}
        ).download()
        assert fake_ohsome.post_kwargs["time"] == "2020-06-01"

    def test_history_index_reset_into_columns(self, osm_kwargs, fake_ohsome):
        """The (@osmId, @snapshotTimestamp) index becomes ordinary columns."""
        fc = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        ).download()
        assert {"@osmId", "@snapshotTimestamp"} <= set(fc.columns)
        assert fc.crs.to_epsg() == 4326

    def test_raw_filter_override(self, osm_kwargs, fake_ohsome):
        """A raw filter= override replaces the catalog ohsome_filter."""
        OSM(
            **{
                **osm_kwargs(),
                "variables": ["ohsome:buildings"],
                "start": "2020-01-01",
                "filter": "amenity=cafe",
            }
        ).download()
        assert fake_ohsome.post_kwargs["filter"] == "amenity=cafe"

    def test_missing_time_raises(self, osm_kwargs, fake_ohsome):
        """An ohsome query without a start raises a helpful ValueError."""
        with pytest.raises(ValueError, match="needs a time"):
            OSM(**{**osm_kwargs(), "variables": ["ohsome:buildings"]}).download()

    def test_plain_index_frame_not_reset(self, osm_kwargs, fake_ohsome):
        """A response frame with a plain index is wrapped without reset."""
        import geopandas as gpd
        from shapely.geometry import Point

        fake_ohsome.frame = gpd.GeoDataFrame(
            {"@other_tags": ["{}"]}, geometry=[Point(8.69, 49.41)], crs="EPSG:4326"
        )
        fc = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        ).download()
        assert "index" not in fc.columns
        assert len(fc) == 1

    def test_request_targets_elements_geometry_endpoint(self, osm_kwargs, fake_ohsome):
        """The request goes through the root client's post(endpoint=...) form."""
        OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        ).download()
        assert fake_ohsome.post_kwargs["endpoint"] == "elements/geometry"

    def test_retry_and_user_agent_policy_applied(self, osm_kwargs, fake_ohsome):
        """The client carries our UA, log=False, and a capped 429/5xx (not 403) retry."""
        from earthlens.osm.backend import USER_AGENT

        OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        ).download()
        client_kwargs = fake_ohsome.client_kwargs
        assert client_kwargs["user_agent"] == USER_AGENT
        assert client_kwargs["log"] is False
        retry = client_kwargs["retry"]
        assert retry.total == OSM.MAX_OHSOME_RETRIES
        assert retry.backoff_factor == OSM.OHSOME_BACKOFF_FACTOR
        assert 429 in retry.status_forcelist
        assert 403 not in retry.status_forcelist
        # The wait ceiling matches HttpClient's, so a hostile Retry-After cannot
        # pin the thread — both the backoff and the Retry-After are capped.
        assert retry.backoff_max == OSM.OHSOME_MAX_BACKOFF
        assert retry.retry_after_max == OSM.OHSOME_MAX_BACKOFF

    def test_forbidden_via_leaked_jsondecodeerror_becomes_unavailable(
        self, osm_kwargs, fake_ohsome
    ):
        """A 403 leaked as a bare JSONDecodeError is recovered via the chain."""
        import requests

        from earthlens.osm import OhsomeUnavailableError

        # One of the two SDK shapes: the HTML-403 body leaks a bare
        # JSONDecodeError whose originating HTTPError (status 403) is only in the
        # __context__ chain.
        http_error = requests.HTTPError("403 Forbidden")
        http_error.response = types.SimpleNamespace(status_code=403)
        leaked = ValueError("Expecting value: line 1 column 1 (char 0)")
        leaked.__context__ = http_error
        fake_ohsome.error = leaked

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        assert excinfo.value.status_code == 403
        assert "public" in str(excinfo.value)
        assert excinfo.value.__cause__ is leaked

    def test_forbidden_via_ohsome_exception_becomes_unavailable(
        self, osm_kwargs, fake_ohsome
    ):
        """A 403 wrapped as OhsomeException(error_code=403) is also classified."""
        from earthlens.osm import OhsomeUnavailableError

        # The other SDK shape: the failure is wrapped into an OhsomeException-like
        # error exposing error_code directly (no leaked JSONDecodeError).
        ohsome_error = RuntimeError("Forbidden")
        ohsome_error.error_code = 403
        fake_ohsome.error = ohsome_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        assert excinfo.value.status_code == 403

    def test_unauthorized_propagates_unchanged(self, osm_kwargs, fake_ohsome):
        """A 401 is a real auth-contract change, not a throttle, so it propagates."""
        from earthlens.osm import OhsomeUnavailableError

        ohsome_error = RuntimeError("Unauthorized")
        ohsome_error.error_code = 401
        fake_ohsome.error = ohsome_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(RuntimeError, match="Unauthorized") as excinfo:
            backend.download()
        assert not isinstance(excinfo.value, OhsomeUnavailableError)

    def test_rate_limited_becomes_unavailable_error(self, osm_kwargs, fake_ohsome):
        """A 429 outliving the retries surfaces as a typed skip signal."""
        from earthlens.osm import OhsomeUnavailableError

        ohsome_error = RuntimeError("Too Many Requests")
        ohsome_error.error_code = 429
        fake_ohsome.error = ohsome_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        assert excinfo.value.status_code == 429

    def test_non_throttle_error_propagates_unchanged(self, osm_kwargs, fake_ohsome):
        """An error with no throttle status propagates as-is (not masked)."""
        fake_ohsome.error = RuntimeError("some genuine bug")
        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(RuntimeError, match="some genuine bug"):
            backend.download()

    def test_non_json_body_becomes_response_error(self, osm_kwargs, fake_ohsome):
        """A non-JSON body (an HTML page) surfaces as a typed OhsomeResponseError."""
        import json

        from earthlens.osm import OhsomeResponseError, OhsomeUnavailableError

        response = types.SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text="<html><body>Service under maintenance</body></html>",
        )
        decode_error = json.JSONDecodeError("Expecting value", "<html>", 0)
        decode_error.response = response
        fake_ohsome.error = decode_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeResponseError) as excinfo:
            backend.download()
        err = excinfo.value
        assert err.status_code == 200
        assert err.content_type == "text/html"
        assert err.body_preview.startswith("<html>")
        assert "non-JSON" in str(err)
        assert "text/html" in str(err)
        # a 200 body is not a throttle, so it is the base error, not the subtype
        assert not isinstance(err, OhsomeUnavailableError)

    def test_non_json_body_without_status_still_typed(self, osm_kwargs, fake_ohsome):
        """A non-JSON failure with no recoverable status still yields the typed error."""
        import json

        from earthlens.osm import OhsomeResponseError

        fake_ohsome.error = json.JSONDecodeError("Expecting value", "", 0)
        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeResponseError) as excinfo:
            backend.download()
        err = excinfo.value
        assert err.status_code is None
        assert err.content_type is None
        assert err.body_preview is None

    def test_json_error_response_propagates_unchanged(self, osm_kwargs, fake_ohsome):
        """A genuine ohsome error served AS JSON is not masked as a response error."""
        from earthlens.osm import OhsomeResponseError

        ohsome_error = RuntimeError("bad request")
        ohsome_error.error_code = 400  # recovered status, but no JSONDecodeError
        fake_ohsome.error = ohsome_error
        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(RuntimeError, match="bad request") as excinfo:
            backend.download()
        assert not isinstance(excinfo.value, OhsomeResponseError)

    def test_transport_error_propagates_unchanged(self, osm_kwargs, fake_ohsome):
        """A transport error (no status, not non-JSON) propagates untouched."""
        import requests

        from earthlens.osm import OhsomeResponseError

        fake_ohsome.error = requests.ConnectionError("connection reset")
        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(
            requests.ConnectionError, match="connection reset"
        ) as excinfo:
            backend.download()
        assert not isinstance(excinfo.value, OhsomeResponseError)

    def test_failure_logs_status_content_type_and_body_preview(
        self, osm_kwargs, fake_ohsome
    ):
        """The recovered status, content-type and body preview are logged (#930)."""
        import json

        from loguru import logger

        from earthlens.osm import OhsomeResponseError

        response = types.SimpleNamespace(
            status_code=503,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html>rate limited</html>",
        )
        decode_error = json.JSONDecodeError("Expecting value", "<html>", 0)
        decode_error.response = response
        fake_ohsome.error = decode_error

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            backend = OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            )
            with pytest.raises(OhsomeResponseError):
                backend.download()
        finally:
            logger.remove(sink_id)
        logged = "".join(str(message) for message in messages)
        assert "503" in logged
        assert "text/html" in logged
        assert "rate limited" in logged

    def test_forbidden_carries_and_logs_evidence(self, osm_kwargs, fake_ohsome):
        """A 403 throttle carries the recovered content-type/body and logs them."""
        import json

        from loguru import logger

        from earthlens.osm import OhsomeUnavailableError

        response = types.SimpleNamespace(
            status_code=403,
            headers={"Content-Type": "text/html"},
            text="<html>Forbidden</html>",
        )
        decode_error = json.JSONDecodeError("Expecting value", "<html>", 0)
        decode_error.response = response
        fake_ohsome.error = decode_error

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            backend = OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            )
            with pytest.raises(OhsomeUnavailableError) as excinfo:
                backend.download()
        finally:
            logger.remove(sink_id)
        err = excinfo.value
        assert err.status_code == 403
        assert err.content_type == "text/html"
        assert err.body_preview.startswith("<html>")
        logged = "".join(str(message) for message in messages)
        assert "403" in logged
        assert "text/html" in logged

    def test_json_error_pass_through_does_not_log(self, osm_kwargs, fake_ohsome):
        """A JSON-served ohsome error is re-raised quietly, with no stray warning."""
        from loguru import logger

        ohsome_error = RuntimeError("bad request")
        ohsome_error.error_code = 400  # recovered status, but served AS JSON
        fake_ohsome.error = ohsome_error

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            backend = OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            )
            with pytest.raises(RuntimeError, match="bad request"):
                backend.download()
        finally:
            logger.remove(sink_id)
        assert messages == [], f"expected no warning on pass-through, got: {messages}"

    def test_server_error_becomes_unavailable(self, osm_kwargs, fake_ohsome):
        """A 503 (the SDK's KeyError on a 5xx body) surfaces as unavailable (#790)."""
        import requests

        from earthlens.osm import OhsomeUnavailableError

        # The ohsome SDK does e.response.json()["message"] on the 503 body, which
        # lacks a "message" key, dying with a raw KeyError whose originating
        # HTTPError (status 503) is only in the __context__ chain.
        http_error = requests.HTTPError("503 Service Unavailable")
        http_error.response = types.SimpleNamespace(
            status_code=503, headers={}, text=""
        )
        key_error = KeyError("message")
        key_error.__context__ = http_error
        fake_ohsome.error = key_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        assert excinfo.value.status_code == 503
        assert "503" in str(excinfo.value)
        assert excinfo.value.__cause__ is key_error

    @pytest.mark.parametrize("status", [500, 502, 504, 599])
    def test_server_error_variants_become_unavailable(
        self, osm_kwargs, fake_ohsome, status
    ):
        """A 5xx wrapped as OhsomeException(error_code=5xx) becomes unavailable."""
        from earthlens.osm import OhsomeUnavailableError

        ohsome_error = RuntimeError("server error")
        ohsome_error.error_code = status
        ohsome_error.response = types.SimpleNamespace(
            status_code=status,
            headers={"Content-Type": "text/html"},
            text="<html>server error</html>",
        )
        fake_ohsome.error = ohsome_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        err = excinfo.value
        assert err.status_code == status, f"expected {status}, got {err.status_code}"
        assert err.content_type == "text/html", f"got {err.content_type}"
        assert err.body_preview.startswith("<html>"), f"got {err.body_preview!r}"

    def test_server_error_with_html_body_prefers_unavailable(
        self, osm_kwargs, fake_ohsome
    ):
        """A 5xx served as HTML is unavailable, not a response error (5xx wins)."""
        import json

        from earthlens.osm import OhsomeResponseError, OhsomeUnavailableError

        response = types.SimpleNamespace(
            status_code=503,
            headers={"Content-Type": "text/html"},
            text="<html>Service Unavailable</html>",
        )
        decode_error = json.JSONDecodeError("Expecting value", "<html>", 0)
        decode_error.response = response
        fake_ohsome.error = decode_error

        backend = OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        )
        with pytest.raises(OhsomeUnavailableError) as excinfo:
            backend.download()
        err = excinfo.value
        assert isinstance(err, OhsomeResponseError), "subtype should keep the base"
        assert err.status_code == 503, f"expected 503, got {err.status_code}"
        assert err.content_type == "text/html", f"got {err.content_type}"
        assert err.body_preview.startswith("<html>"), f"got {err.body_preview!r}"


class TestE2ESkipHelper:
    """The e2e `_skip_on_network` decides skip-vs-fail from the typed error."""

    def test_skips_on_server_error(self):
        """A 5xx OhsomeUnavailableError skips the lane rather than failing (#790)."""
        from _pytest.outcomes import Skipped

        from earthlens.osm import OhsomeUnavailableError

        from .test_osm_e2e import _skip_on_network

        outage = OhsomeUnavailableError("outage", status_code=503)
        with pytest.raises(Skipped):
            _skip_on_network(outage)

    def test_skips_on_throttle(self):
        """A 403 OhsomeUnavailableError still skips (issue #1025 behaviour)."""
        from _pytest.outcomes import Skipped

        from earthlens.osm import OhsomeUnavailableError

        from .test_osm_e2e import _skip_on_network

        throttled = OhsomeUnavailableError("throttled", status_code=403)
        with pytest.raises(Skipped):
            _skip_on_network(throttled)

    def test_reraises_a_genuine_error(self):
        """A non-throttle, non-outage error re-raises and fails the lane."""
        from .test_osm_e2e import _skip_on_network

        genuine = ValueError("boom")
        with pytest.raises(ValueError, match="boom"):
            _skip_on_network(genuine)

    def test_does_not_skip_on_non_transient_status(self):
        """A non-transient-status OhsomeUnavailableError re-raises, not skipped."""
        from earthlens.osm import OhsomeUnavailableError

        from .test_osm_e2e import _skip_on_network

        unexpected = OhsomeUnavailableError("odd", status_code=404)
        with pytest.raises(OhsomeUnavailableError):
            _skip_on_network(unexpected)

    def test_skips_at_upper_5xx_boundary(self):
        """The top of the 5xx range (599) still skips."""
        from _pytest.outcomes import Skipped

        from earthlens.osm import OhsomeUnavailableError

        from .test_osm_e2e import _skip_on_network

        top = OhsomeUnavailableError("outage", status_code=599)
        with pytest.raises(Skipped):
            _skip_on_network(top)

    def test_does_not_skip_at_600(self):
        """Just past the 5xx range (600) re-raises, not skipped."""
        from earthlens.osm import OhsomeUnavailableError

        from .test_osm_e2e import _skip_on_network

        past = OhsomeUnavailableError("odd", status_code=600)
        with pytest.raises(OhsomeUnavailableError):
            _skip_on_network(past)

    def test_shared_hook_does_not_mask_deliberate_failures(self):
        """The shared availability classifier fails what `_skip_on_network` re-raises.

        `_skip_on_network` runs inside the e2e test body; a non-transient
        `OhsomeUnavailableError` (404 / 600) or a bare non-JSON `OhsomeResponseError`
        it re-raises then reaches the shared `pytest_runtest_call` hook, which
        classifies via `is_upstream_unavailable`. That classifier must return
        `None` (fail) for these, or a real ohsome request-shape regression is
        masked as a skip (#1088).
        """
        from earthlens.osm import OhsomeResponseError, OhsomeUnavailableError
        from earthlens.testing import is_upstream_unavailable

        non_transient = OhsomeUnavailableError("bad filter", status_code=404)
        beyond_5xx = OhsomeUnavailableError("odd", status_code=600)
        non_json = OhsomeResponseError("non-JSON maintenance page")
        assert is_upstream_unavailable(non_transient) is None, "404 must stay a failure"
        assert is_upstream_unavailable(beyond_5xx) is None, "600 must stay a failure"
        assert is_upstream_unavailable(non_json) is None, (
            "bare body must stay a failure"
        )


class TestDownloadContract:
    """Cross-cutting download() behaviour."""

    def test_license_warning_always(self, osm_kwargs, fake_overpy, fake_overpass_post):
        """Every successful download emits an ODbL LicenseWarning."""
        with pytest.warns(LicenseWarning, match="ODbL"):
            OSM(**osm_kwargs()).download()

    def test_license_warning_on_empty(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """The ODbL warning fires even when nothing matched."""
        fake_overpy.result.nodes = []
        fake_overpy.result.ways = []
        with pytest.warns(LicenseWarning):
            fc = OSM(**osm_kwargs()).download()
        assert len(fc) == 0

    def test_aggregate_rejected(self, osm_kwargs):
        """A non-None aggregate= raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="aggregate"):
            OSM(**osm_kwargs()).download(aggregate=object())

    def test_writes_file_when_non_empty(
        self, osm_kwargs, fake_overpy, fake_overpass_post, tmp_path
    ):
        """A non-empty result is written to one vector file under path."""
        OSM(**osm_kwargs()).download()
        written = list(Path(tmp_path).glob("osm_*.geojson"))
        assert len(written) == 1

    def test_writes_gpkg_with_mixed_geometry(
        self, osm_kwargs, fake_overpy, fake_overpass_post, tmp_path
    ):
        """file_format='gpkg' writes a GeoPackage even with mixed geometry types."""
        import geopandas as gpd

        OSM(**{**osm_kwargs(), "file_format": "gpkg"}).download()
        written = list(Path(tmp_path).glob("osm_*.gpkg"))
        assert len(written) == 1
        reloaded = gpd.read_file(written[0])
        assert len(reloaded) == 3  # Point + LineString + Polygon round-tripped

    def test_no_file_when_empty(
        self, osm_kwargs, fake_overpy, fake_overpass_post, tmp_path
    ):
        """An empty result writes nothing."""
        fake_overpy.result.nodes = []
        fake_overpy.result.ways = []
        OSM(**osm_kwargs()).download()
        assert list(Path(tmp_path).glob("osm_*")) == []

    def test_multiple_queries_combined(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """Several overpass queries combine into one collection."""
        fc = OSM(
            **{**osm_kwargs(), "variables": ["overpass:hospitals", "overpass:roads"]}
        ).download()
        # each query returns the same fixture result (3 features) -> 6 combined.
        assert len(fc) == 6

    def test_overpass_and_ohsome_combined(
        self, osm_kwargs, fake_overpy, fake_overpass_post, fake_ohsome
    ):
        """An overpass + an ohsome query combine, unioning their disjoint columns."""
        fc = OSM(
            **{
                **osm_kwargs(),
                "variables": ["overpass:hospitals", "ohsome:buildings"],
                "start": "2020-01-01",
            }
        ).download()
        assert len(fc) == 5  # 3 overpass + 2 ohsome fixture rows
        assert {"osm_id", "osm_type", "@osmId"} <= set(fc.columns)
        assert fc.crs.to_epsg() == 4326

    def test_unknown_query_id_raises(self, osm_kwargs):
        """An unknown named-query id raises ValueError before any fetch."""
        with pytest.raises(ValueError, match="not in the OSM query catalog"):
            OSM(**{**osm_kwargs(), "variables": ["overpass:nope"]}).download()

    def test_whole_earth_bbox_rejected(self, osm_kwargs):
        """A whole-Earth bbox exceeds the area cap and is rejected before fetch."""
        with pytest.raises(ValueError, match="too large for a live OSM query"):
            OSM(
                **{**osm_kwargs(), "lat_lim": [-90, 90], "lon_lim": [-180, 180]}
            ).download()

    def test_thin_globe_spanning_bbox_rejected(self, osm_kwargs):
        """A thin box under the area cap but globe-spanning on one axis is rejected."""
        with pytest.raises(ValueError, match="too large"):
            OSM(
                **{**osm_kwargs(), "lat_lim": [0.0, 0.1], "lon_lim": [-180, 180]}
            ).download()

    def test_max_bbox_override_allows_large_box(
        self, osm_kwargs, fake_overpy, fake_overpass_post
    ):
        """A raised max_bbox_deg2 lets a larger box through."""
        fc = OSM(
            **{
                **osm_kwargs(),
                "lat_lim": [40.0, 50.0],
                "lon_lim": [0.0, 20.0],  # 200 deg2, over the 100 default
                "max_bbox_deg2": 1000.0,
            }
        ).download()
        assert len(fc) == 3


class TestLazyImports:
    """The SDKs are imported lazily; a missing one is a friendly ImportError."""

    def test_construction_without_overpy(self, osm_kwargs, monkeypatch):
        """Constructing the backend needs neither SDK installed."""
        monkeypatch.setitem(sys.modules, "overpy", None)
        monkeypatch.setitem(sys.modules, "ohsome", None)
        osm = OSM(**osm_kwargs())  # must not raise
        assert osm.OUTPUT_KIND == "vector"

    def test_missing_overpy_friendly_error(
        self, osm_kwargs, fake_overpass_post, monkeypatch
    ):
        """A missing overpy surfaces as an ImportError naming earthlens[osm]."""
        monkeypatch.setitem(sys.modules, "overpy", None)
        with pytest.raises(ImportError, match=r"earthlens\[osm\]"):
            OSM(**osm_kwargs()).download()

    def test_missing_ohsome_friendly_error(self, osm_kwargs, monkeypatch):
        """A missing ohsome surfaces as an ImportError naming earthlens[osm]."""
        monkeypatch.setitem(sys.modules, "ohsome", None)
        with pytest.raises(ImportError, match=r"earthlens\[osm\]"):
            OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            ).download()


def test_no_xarray_in_subpackage():
    """The osm subpackage never imports xarray (G7)."""
    import earthlens.osm as pkg

    root = Path(pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import xarray" not in text, path.name
        assert "xr." not in text, path.name


class TestLimitStopsTheWork:
    """A `limit=` must stop issuing queries, not trim the merged collection.

    Every query is a live Overpass / ohsome request against a rate-limited
    public endpoint, so a cap that only sliced the combined result would still
    spend the quota on every named query.
    """

    def _fake_products(self, backend, monkeypatch, fetched):
        """Point the backend at two queries whose fetch is recorded."""
        import geopandas as gpd
        from pyramids.feature.collection import FeatureCollection
        from shapely.geometry import Point

        def fake_fetch_product(product):
            fetched.append(product.id)
            frame = gpd.GeoDataFrame(
                {"name": ["a", "b", "c"]},
                geometry=[Point(8.68, 49.41)] * 3,
                crs="EPSG:4326",
            )
            return FeatureCollection(frame)

        monkeypatch.setattr(backend, "_fetch_product", fake_fetch_product)

    def test_queries_past_the_cap_are_never_issued(self, osm_kwargs, monkeypatch):
        """The second query is not run once the first fills the cap."""
        backend = OSM(
            **{
                **osm_kwargs(),
                "variables": ["overpass:hospitals", "overpass:schools"],
            }
        )
        fetched: list[str] = []
        self._fake_products(backend, monkeypatch, fetched)

        backend._limit = 2
        collections = backend._fetch(backend._search())

        assert fetched == ["overpass:hospitals"], (
            f"issued {fetched}; the second query was run even though the cap "
            f"was already met"
        )
        assert sum(len(fc) for fc in collections) == 2

    def test_no_limit_issues_every_query(self, osm_kwargs, monkeypatch):
        """Without a cap every named query still runs."""
        backend = OSM(
            **{
                **osm_kwargs(),
                "variables": ["overpass:hospitals", "overpass:schools"],
            }
        )
        fetched: list[str] = []
        self._fake_products(backend, monkeypatch, fetched)

        backend._limit = None
        backend._fetch(backend._search())

        assert fetched == ["overpass:hospitals", "overpass:schools"]

    def test_a_zero_limit_is_refused_before_any_query(self, osm_kwargs, monkeypatch):
        """`limit=0` is caught before the first request goes out."""
        backend = OSM(**osm_kwargs())
        monkeypatch.setattr(
            backend,
            "_fetch_product",
            lambda product: pytest.fail("a rejected cap must not reach the network"),
        )
        with pytest.raises(ValueError):
            backend.download(progress_bar=False, limit=0)


class TestRateLimitActuallyPaces:
    """`MIN_REQUEST_INTERVAL` must pace successive queries, not just be declared.

    The interval is enforced from a `_last_request` timestamp held on the
    `HttpClient`. Building a client per query gave every request a fresh client
    with no history, so the declared 1.0 s floor produced zero sleeps against
    the shared public Overpass endpoint it exists to protect — declared,
    passed, and completely inert.
    """

    def test_the_client_is_reused_across_queries(self, osm_kwargs):
        """The same client instance serves every Overpass query."""
        backend = OSM(**osm_kwargs())
        assert backend._overpass_client() is backend._overpass_client()

    def test_successive_requests_are_spaced_by_the_interval(self, osm_kwargs):
        """With an injected clock, the second request sleeps the full interval."""
        backend = OSM(**osm_kwargs())
        client = backend._overpass_client()
        assert client.min_interval == OSM.MIN_REQUEST_INTERVAL

        now = [1000.0]
        slept: list[float] = []
        client._clock = lambda: now[0]
        client._sleep = lambda seconds: (
            slept.append(seconds),
            now.__setitem__(0, now[0] + seconds),
        )

        client._throttle()
        client._throttle()

        assert slept, (
            f"expected a pause between back-to-back requests, got none: {slept}"
        )
        assert slept[0] == pytest.approx(OSM.MIN_REQUEST_INTERVAL), (
            f"expected a {OSM.MIN_REQUEST_INTERVAL}s pause between back-to-back "
            f"requests, got {slept}"
        )
