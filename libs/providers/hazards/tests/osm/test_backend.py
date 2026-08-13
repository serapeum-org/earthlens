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
        """The client carries our UA, log=False, and a 429/5xx (not 403) retry."""
        from earthlens.osm.backend import USER_AGENT

        OSM(
            **{**osm_kwargs(), "variables": ["ohsome:buildings"], "start": "2020-01-01"}
        ).download()
        client_kwargs = fake_ohsome.client_kwargs
        assert client_kwargs["user_agent"] == USER_AGENT
        assert client_kwargs["log"] is False
        retry = client_kwargs["retry"]
        assert retry.total == 5
        assert 429 in retry.status_forcelist
        assert 403 not in retry.status_forcelist

    def test_forbidden_becomes_unavailable_error(self, osm_kwargs, fake_ohsome):
        """A 403 (leaked through the SDK's chain) surfaces as a typed skip signal."""
        import requests

        from earthlens.osm import OhsomeUnavailableError

        # Mirror the real leak: the SDK's HTML-403 handling raises a bare
        # JSONDecodeError whose originating HTTPError (status 403) is only in
        # the __context__ chain.
        http_error = requests.HTTPError("403 Forbidden")
        http_error.response = types.SimpleNamespace(status_code=403)
        leaked = ValueError("Expecting value: line 1 column 1 (char 0)")
        leaked.__context__ = http_error
        fake_ohsome.error = leaked

        with pytest.raises(OhsomeUnavailableError) as excinfo:
            OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            ).download()
        assert excinfo.value.status_code == 403
        assert "public" in str(excinfo.value)
        assert excinfo.value.__cause__ is leaked

    def test_rate_limited_becomes_unavailable_error(self, osm_kwargs, fake_ohsome):
        """A 429 outliving the retries surfaces as a typed skip signal."""
        from earthlens.osm import OhsomeUnavailableError

        ohsome_error = RuntimeError("Too Many Requests")
        ohsome_error.error_code = 429
        fake_ohsome.error = ohsome_error

        with pytest.raises(OhsomeUnavailableError) as excinfo:
            OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            ).download()
        assert excinfo.value.status_code == 429

    def test_non_throttle_error_propagates_unchanged(self, osm_kwargs, fake_ohsome):
        """An error with no throttle status propagates as-is (not masked)."""
        fake_ohsome.error = RuntimeError("some genuine bug")
        with pytest.raises(RuntimeError, match="some genuine bug"):
            OSM(
                **{
                    **osm_kwargs(),
                    "variables": ["ohsome:buildings"],
                    "start": "2020-01-01",
                }
            ).download()


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
