"""Unit + integration tests for the OSM backend (faked overpy / ohsome)."""

from __future__ import annotations

import sys
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
        assert "index" not in fc.columns and len(fc) == 1


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
        assert "import xarray" not in text and "xr." not in text, path.name
