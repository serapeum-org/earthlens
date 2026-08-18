"""Unit + integration tests for `earthlens.overture.backend`."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from loguru import logger

from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent
from earthlens.overture import Overture
from earthlens.overture._helpers import ODBL, LicenseWarning
from earthlens.overture.backend import _require_overturemaps, _stream_to_geodataframe
from earthlens.overture.releases import ReleaseLookupError

from .conftest import FAKE_RELEASE, OSM_SOURCES, PERMISSIVE_SOURCES


def _make_backend(tmp_path: Path, **overrides) -> Overture:
    """Construct an Overture backend with a small, guarded-safe default bbox."""
    params: dict[str, object] = dict(
        variables={"places": []},
        lat_lim=[40.757, 40.759],
        lon_lim=[-73.987, -73.984],
        path=str(tmp_path),
    )
    params.update(overrides)
    return Overture(**params)


def _record_releases(seen: list, gdf):
    """Build a `query_overture` stand-in that records each release it is given."""

    def _query(theme, otype, release, *_args, **_kwargs):
        seen.append(release)
        return gdf

    return _query


def _record_query(seen: dict, gdf):
    """Build a `query_overture` stand-in that records its call and returns `gdf`."""

    def _query(theme, otype, release, bbox, **kwargs):
        seen.update(theme=theme, otype=otype, release=release, bbox=bbox, **kwargs)
        return gdf

    return _query


def _stac(monkeypatch, document: dict) -> None:
    """Serve `document` as Overture's STAC catalog for one test."""
    monkeypatch.setattr(
        "earthlens.overture.releases.stac_catalog", lambda *_a, **_k: document
    )


def _boom() -> str:
    """Stand in for a live release lookup that cannot reach Overture."""
    raise ReleaseLookupError("could not read Overture's STAC catalog (no route)")


def _missing_sdk() -> str:
    """Stand in for a release lookup whose SDK entry point is gone."""
    raise ImportError("cannot import name 'STAC_CATALOG_URL'")


def _renamed_sdk() -> str:
    """Stand in for a release lookup whose SDK internals moved."""
    raise AttributeError("'module' object has no attribute 'STAC_CATALOG_URL'")


@pytest.mark.overture
class TestOvertureConstruction:
    """`__init__` wiring and validation."""

    def test_output_kind_is_vector(self):
        """Overture declares vector output."""
        assert Overture.OUTPUT_KIND == "vector"

    def test_no_auth_client(self, tmp_path: Path):
        """No auth: the backend has no `client` attribute."""
        backend = _make_backend(tmp_path)
        assert not hasattr(backend, "client")

    def test_space_captured(self, tmp_path: Path):
        """The bbox lands on `self.space` as a SpatialExtent."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.space, SpatialExtent)
        assert backend.space.west == -73.987

    def test_time_is_static_sentinel(self, tmp_path: Path):
        """Resolution is the 'all' sentinel; dates are empty."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.time, TemporalExtent)
        assert backend.time.resolution == "all"
        assert len(backend.time.dates) == 0

    def test_dates_parsed_when_supplied(self, tmp_path: Path):
        """Supplied start/end are parsed but otherwise ignored."""
        backend = _make_backend(tmp_path, start="2026-01-01", end="2026-02-01")
        assert backend.time.start_date is not None
        assert backend.time.end_date is not None

    def test_dates_none_by_default(self, tmp_path: Path):
        """With no dates, start/end stay None (static snapshot)."""
        backend = _make_backend(tmp_path)
        assert backend.time.start_date is None
        assert backend.time.end_date is None

    def test_only_start_supplied(self, tmp_path: Path):
        """A start with no end parses start and leaves end None."""
        backend = _make_backend(tmp_path, start="2026-01-01")
        assert backend.time.start_date is not None
        assert backend.time.end_date is None

    def test_only_end_supplied(self, tmp_path: Path):
        """An end with no start parses end and leaves start None."""
        backend = _make_backend(tmp_path, end="2026-02-01")
        assert backend.time.start_date is None
        assert backend.time.end_date is not None

    def test_variables_list_rejected(self, tmp_path: Path):
        """A list `variables` (the GDACS shape) is a TypeError here."""
        with pytest.raises(TypeError, match=r"mapping of theme"):
            _make_backend(tmp_path, variables=["place"])

    def test_empty_variables_rejected(self, tmp_path: Path):
        """An empty `variables` mapping is rejected."""
        with pytest.raises(ValueError, match=r"`variables` is empty"):
            _make_backend(tmp_path, variables={})

    def test_bad_file_format_rejected(self, tmp_path: Path):
        """An unsupported file_format is rejected."""
        with pytest.raises(ValueError, match=r"file_format must be one of"):
            _make_backend(tmp_path, file_format="shp")

    def test_release_and_max_features_stored(self, tmp_path: Path):
        """`release` / `max_features` are captured for the fetch."""
        backend = _make_backend(tmp_path, release="2026-05-20.0", max_features=10)
        assert backend._release == "2026-05-20.0"
        assert backend._max_features == 10

    def test_temporal_resolution_pinned_to_all(self, tmp_path: Path):
        """`temporal_resolution` is pinned to the static sentinel even if 'daily' is passed."""
        backend = _make_backend(tmp_path, temporal_resolution="daily")
        assert backend.temporal_resolution == "all"
        assert backend.time.resolution == "all"


@pytest.mark.overture
class TestResolvePlan:
    """`_resolve_plan` theme/type expansion."""

    def test_default_type_expansion(self, tmp_path: Path):
        """An empty type list expands to the theme's default type."""
        backend = _make_backend(tmp_path, variables={"buildings": []})
        plan = backend._resolve_plan()
        assert [(n, t) for n, _theme, t in plan] == [("buildings", "building")]

    def test_multiple_themes_and_types(self, tmp_path: Path):
        """Several themes/types expand in order."""
        backend = _make_backend(
            tmp_path,
            variables={"transportation": ["segment", "connector"], "places": []},
            lat_lim=[40.757, 40.759],
            lon_lim=[-73.987, -73.984],
        )
        plan = backend._resolve_plan()
        assert [t for _n, _theme, t in plan] == ["segment", "connector", "place"]

    def test_base_default_type_is_land(self, tmp_path: Path):
        """The `base` theme's empty type list resolves to `land`."""
        backend = _make_backend(tmp_path, variables={"base": []})
        plan = backend._resolve_plan()
        assert [(n, t) for n, _theme, t in plan] == [("base", "land")]

    def test_addresses_default_type_is_address(self, tmp_path: Path):
        """The `addresses` theme's empty type list resolves to `address`."""
        backend = _make_backend(tmp_path, variables={"addresses": []})
        plan = backend._resolve_plan()
        assert [(n, t) for n, _theme, t in plan] == [("addresses", "address")]

    def test_base_explicit_types(self, tmp_path: Path):
        """Explicit `base` types (water, bathymetry) resolve in order."""
        backend = _make_backend(tmp_path, variables={"base": ["water", "bathymetry"]})
        plan = backend._resolve_plan()
        assert [t for _n, _theme, t in plan] == ["water", "bathymetry"]

    def test_unknown_theme_raises(self, tmp_path: Path):
        """An unknown theme raises with a did-you-mean hint."""
        backend = _make_backend(tmp_path, variables={"building": []})
        with pytest.raises(ValueError, match=r"Did you mean 'buildings'\?"):
            backend._resolve_plan()

    def test_unknown_type_raises(self, tmp_path: Path):
        """An unknown type for a known theme raises."""
        backend = _make_backend(tmp_path, variables={"places": ["poi"]})
        with pytest.raises(ValueError, match=r"not valid types"):
            backend._resolve_plan()

    def test_duplicate_types_collapsed(self, tmp_path: Path):
        """A repeated type resolves to a single fetch (no self-overwrite)."""
        backend = _make_backend(
            tmp_path, variables={"buildings": ["building", "building"]}
        )
        plan = backend._resolve_plan()
        assert [t for _n, _theme, t in plan] == ["building"]


@pytest.mark.overture
class TestGuardBbox:
    """`_guard_bbox` size guard for the large themes."""

    def test_small_bbox_passes(self, tmp_path: Path):
        """A small bbox on buildings is allowed."""
        backend = _make_backend(tmp_path, variables={"buildings": []})
        backend._guard_bbox(["buildings"])

    def test_whole_earth_buildings_rejected(self, tmp_path: Path):
        """A whole-Earth bbox on buildings is rejected."""
        backend = _make_backend(
            tmp_path,
            variables={"buildings": []},
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
        )
        with pytest.raises(ValueError, match=r"square-degree cap for the 'buildings'"):
            backend._guard_bbox(["buildings"])

    def test_oversized_places_rejected(self, tmp_path: Path):
        """A bbox above the places cap is rejected."""
        backend = _make_backend(
            tmp_path, variables={"places": []}, lat_lim=[0, 10], lon_lim=[0, 10]
        )
        with pytest.raises(ValueError, match=r"'places'"):
            backend._guard_bbox(["places"])

    def test_base_guarded(self, tmp_path: Path):
        """The `base` theme is guarded against an oversized bbox."""
        backend = _make_backend(
            tmp_path, variables={"base": []}, lat_lim=[0, 10], lon_lim=[0, 10]
        )
        with pytest.raises(ValueError, match=r"'base'"):
            backend._guard_bbox(["base"])

    def test_addresses_guarded(self, tmp_path: Path):
        """The `addresses` theme is guarded against an oversized bbox."""
        backend = _make_backend(
            tmp_path, variables={"addresses": []}, lat_lim=[0, 10], lon_lim=[0, 10]
        )
        with pytest.raises(ValueError, match=r"'addresses'"):
            backend._guard_bbox(["addresses"])

    def test_divisions_unguarded(self, tmp_path: Path):
        """Divisions are unguarded even over a large bbox."""
        backend = _make_backend(
            tmp_path,
            variables={"divisions": ["division_area"]},
            lat_lim=[0, 40],
            lon_lim=[0, 40],
        )
        backend._guard_bbox(["divisions"])

    def test_max_bbox_override(self, tmp_path: Path):
        """`max_bbox_deg2` overrides the per-theme cap."""
        backend = _make_backend(
            tmp_path,
            variables={"buildings": []},
            lat_lim=[0, 2],
            lon_lim=[0, 2],
            max_bbox_deg2=100.0,
        )
        backend._guard_bbox(["buildings"])


@pytest.mark.overture
class TestSearch:
    """`_search` planning and guard enforcement."""

    def test_one_product_per_type(self, tmp_path: Path):
        """`_search` yields one product per requested type."""
        backend = _make_backend(
            tmp_path,
            variables={"transportation": ["segment", "connector"]},
            lat_lim=[40.757, 40.759],
            lon_lim=[-73.987, -73.984],
        )
        products = backend._search()
        assert [p.id for p in products] == [
            "transportation/segment",
            "transportation/connector",
        ]
        assert all(isinstance(p, RemoteProduct) for p in products)

    def test_search_enforces_guard(self, tmp_path: Path):
        """`_search` raises when the bbox is too large for a guarded theme."""
        backend = _make_backend(
            tmp_path,
            variables={"buildings": []},
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
        )
        with pytest.raises(ValueError, match=r"square-degree cap"):
            backend._search()


@pytest.mark.overture
class TestFetchAndDownload:
    """`_fetch` / `download` against the faked SDK."""

    def test_fetch_calls_sdk_with_type_bbox_release(
        self, tmp_path: Path, fake_overture
    ):
        """`_fetch` calls the SDK once per type with the type, bbox, release."""
        backend = _make_backend(
            tmp_path, variables={"places": []}, release="2026-05-20.0"
        )
        backend.download()
        assert len(fake_overture.calls) == 1
        call = fake_overture.calls[0]
        assert call["type"] == "place"
        assert call["bbox"] == (-73.987, 40.757, -73.984, 40.759)
        assert call["release"] == "2026-05-20.0"

    def test_download_writes_geoparquet_with_license_id(
        self, tmp_path: Path, fake_overture, make_gdf
    ):
        """A GeoParquet file lands with a `license_id` column."""
        fake_overture.set_gdf("place", make_gdf([PERMISSIVE_SOURCES, OSM_SOURCES]))
        backend = _make_backend(tmp_path, variables={"places": []})
        paths = backend.download()
        assert len(paths) == 1 and paths[0].suffix == ".parquet"
        gdf = gpd.read_parquet(paths[0])
        assert "license_id" in gdf.columns
        assert gdf.crs.to_epsg() == 4326

    def test_download_warns_on_odbl(self, tmp_path: Path, fake_overture, make_gdf):
        """An ODbL row in the fetched frame triggers a `LicenseWarning`."""
        fake_overture.set_gdf("building", make_gdf([OSM_SOURCES]))
        backend = _make_backend(tmp_path, variables={"buildings": []})
        with pytest.warns(LicenseWarning):
            backend.download()

    @pytest.mark.parametrize(
        "file_format, suffix",
        [("geoparquet", ".parquet"), ("gpkg", ".gpkg"), ("geojson", ".geojson")],
    )
    def test_write_formats(self, tmp_path: Path, fake_overture, file_format, suffix):
        """Each output format writes the expected extension."""
        backend = _make_backend(
            tmp_path, variables={"places": []}, file_format=file_format
        )
        paths = backend.download()
        assert paths[0].suffix == suffix
        assert paths[0].exists()

    def test_filename_embeds_theme_type_release(self, tmp_path: Path, fake_overture):
        """The written filename embeds theme, type, and release."""
        backend = _make_backend(
            tmp_path, variables={"places": []}, release="2026-05-20.0"
        )
        path = backend.download()[0]
        assert path.name == "overture_places_place_2026-05-20.0.parquet"

    def test_filename_uses_latest_when_release_none(
        self, tmp_path: Path, fake_overture
    ):
        """With no release the filename uses the 'latest' marker."""
        backend = _make_backend(tmp_path, variables={"places": []})
        path = backend.download()[0]
        assert path.name == "overture_places_place_latest.parquet"

    def test_max_features_truncates(self, tmp_path: Path, fake_overture, make_gdf):
        """`max_features` caps the written row count."""
        fake_overture.set_gdf("place", make_gdf([PERMISSIVE_SOURCES] * 6))
        backend = _make_backend(tmp_path, variables={"places": []}, max_features=2)
        gdf = gpd.read_parquet(backend.download()[0])
        assert len(gdf) == 2

    def test_empty_fetch_skips_write(self, tmp_path: Path, fake_overture, make_gdf):
        """A type matching no features writes nothing and yields no path."""
        fake_overture.set_gdf("place", make_gdf([]))
        backend = _make_backend(tmp_path, variables={"places": []})
        paths = backend.download()
        assert paths == []
        assert list(tmp_path.glob("overture_*")) == []

    @pytest.mark.parametrize("file_format", ["geoparquet", "gpkg", "geojson"])
    def test_write_nested_columns(
        self, tmp_path: Path, fake_overture, make_gdf, file_format
    ):
        """Nested struct columns (names/categories) round-trip through every format."""
        fake_overture.set_gdf(
            "place", make_gdf([PERMISSIVE_SOURCES, OSM_SOURCES], nested=True)
        )
        backend = _make_backend(
            tmp_path, variables={"places": []}, file_format=file_format
        )
        path = backend.download()[0]
        if file_format == "geoparquet":
            back = gpd.read_parquet(path)
        else:
            back = gpd.read_file(path)
        assert len(back) == 2
        assert "license_id" in back.columns

    def test_stream_uses_record_batch_reader(
        self, tmp_path: Path, fake_overture, make_gdf
    ):
        """`stream=True` reads via record_batch_reader, not geodataframe."""
        fake_overture.set_gdf("place", make_gdf([PERMISSIVE_SOURCES, OSM_SOURCES]))
        backend = _make_backend(tmp_path, variables={"places": []}, stream=True)
        gdf = gpd.read_parquet(backend.download()[0])
        assert len(fake_overture.reader_calls) == 1, "should stream"
        assert fake_overture.calls == [], (
            "geodataframe must not be called when streaming"
        )
        assert list(gdf["license_id"]) == ["Apache-2.0; CDLA-Permissive-2.0", ODBL]

    def test_max_features_streams_with_early_stop(
        self, tmp_path: Path, fake_overture, make_gdf
    ):
        """`max_features` routes through the streaming reader and caps the rows."""
        fake_overture.set_gdf("place", make_gdf([PERMISSIVE_SOURCES] * 6))
        backend = _make_backend(tmp_path, variables={"places": []}, max_features=2)
        gdf = gpd.read_parquet(backend.download()[0])
        assert len(gdf) == 2
        assert len(fake_overture.reader_calls) == 1
        assert fake_overture.calls == [], "max_features should stream, not materialise"

    def test_default_uses_geodataframe_not_reader(self, tmp_path: Path, fake_overture):
        """With neither stream nor max_features, the materialising path is used."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend.download()
        assert len(fake_overture.calls) == 1, "geodataframe materialise path"
        assert fake_overture.reader_calls == [], (
            "no streaming without stream/max_features"
        )

    def test_download_rejects_aggregate(self, tmp_path: Path):
        """A non-None aggregate is rejected at the backend."""
        backend = _make_backend(tmp_path)
        with pytest.raises(NotImplementedError, match=r"aggregate"):
            backend.download(aggregate=object())

    def test_multiple_types_write_multiple_paths(self, tmp_path: Path, fake_overture):
        """A two-type request writes one file per type, in order."""
        backend = _make_backend(
            tmp_path,
            variables={"transportation": ["segment", "connector"]},
            lat_lim=[40.757, 40.759],
            lon_lim=[-73.987, -73.984],
        )
        paths = backend.download()
        assert len(paths) == 2
        assert [p.name for p in paths] == [
            "overture_transportation_segment_latest.parquet",
            "overture_transportation_connector_latest.parquet",
        ]

    def test_api_returns_written_paths(self, tmp_path: Path, fake_overture):
        """`_api` returns the list of written paths via the search/fetch split."""
        backend = _make_backend(tmp_path, variables={"places": []})
        paths = backend._api()
        assert len(paths) == 1
        assert all(isinstance(p, Path) and p.exists() for p in paths)


@pytest.mark.overture
class TestDuckDBQueryPath:
    """`where=` / `columns=` route the fetch through the DuckDB query path."""

    def test_where_routes_to_duckdb_not_sdk(
        self, tmp_path: Path, fake_overture, make_gdf, monkeypatch
    ):
        """A `where=` predicate fetches via query_overture, not the SDK."""
        seen: dict = {}

        def fake_query(
            theme, otype, release, bbox, where=None, columns=None, limit=None
        ):
            seen.update(
                theme=theme,
                otype=otype,
                release=release,
                bbox=bbox,
                where=where,
                columns=columns,
                limit=limit,
            )
            return make_gdf([OSM_SOURCES])

        monkeypatch.setattr("earthlens.overture.query.query_overture", fake_query)
        backend = _make_backend(
            tmp_path,
            variables={"buildings": []},
            where="height > 10",
            release="2026-05-20.0",
        )
        with pytest.warns(LicenseWarning):
            paths = backend.download()
        assert seen["where"] == "height > 10"
        assert (seen["theme"], seen["otype"]) == ("buildings", "building")
        assert seen["release"] == "2026-05-20.0"
        assert fake_overture.calls == [], "geodataframe must not be used with where="
        assert fake_overture.reader_calls == [], "reader must not be used with where="
        assert "license_id" in gpd.read_parquet(paths[0]).columns

    def test_columns_and_limit_forwarded(
        self, tmp_path: Path, fake_overture, make_gdf, monkeypatch
    ):
        """`columns` and `max_features` reach query_overture (limit)."""
        seen: dict = {}
        monkeypatch.setattr(
            "earthlens.overture.query.query_overture",
            _record_query(seen, make_gdf([PERMISSIVE_SOURCES])),
        )
        backend = _make_backend(
            tmp_path,
            variables={"places": []},
            columns=["names"],
            max_features=5,
        )
        backend.download()
        assert seen["columns"] == ["names"]
        assert seen["limit"] == 5

    def test_resolved_release_reaches_the_query(
        self, tmp_path: Path, fake_overture, make_gdf, monkeypatch
    ):
        """The release the backend resolves is the one the S3 glob is built from."""
        seen: dict = {}
        monkeypatch.setattr(
            "earthlens.overture.query.query_overture",
            lambda theme, otype, release, bbox, **k: (
                seen.update(release=release) or make_gdf([PERMISSIVE_SOURCES])
            ),
        )
        fake_overture.latest = "2031-03-03.0"
        backend = _make_backend(
            tmp_path, variables={"places": []}, where="confidence > 0.9"
        )
        backend.download()
        assert seen["release"] == "2031-03-03.0", (
            "an unpinned DuckDB fetch must glob the live release, not the "
            "bundled index entry"
        )

    def test_fallback_to_the_index_is_warned_about(self, tmp_path: Path, monkeypatch):
        """Falling back to the bundled index says so, and says it may be stale."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = ["2020-01-01.0"]
        monkeypatch.setattr("earthlens.overture.releases.latest_release", _boom)
        messages: list[str] = []
        sink = logger.add(lambda record: messages.append(str(record)), level="WARNING")
        try:
            assert backend._resolve_release() == "2020-01-01.0"
        finally:
            logger.remove(sink)
        assert any("2020-01-01.0" in message for message in messages), messages
        assert any("may no longer exist" in message for message in messages), messages

    def test_duckdb_filename_records_the_resolved_release(
        self, tmp_path: Path, fake_overture, make_gdf, monkeypatch
    ):
        """An unpinned DuckDB write names the snapshot it read, not `latest`."""
        monkeypatch.setattr(
            "earthlens.overture.query.query_overture",
            lambda *a, **k: make_gdf([PERMISSIVE_SOURCES]),
        )
        backend = _make_backend(
            tmp_path, variables={"places": []}, where="confidence > 0.9"
        )
        paths = backend.download()
        assert paths[0].name == f"overture_places_place_{FAKE_RELEASE}.parquet"

    def test_release_is_resolved_once_per_download(
        self, tmp_path: Path, fake_overture, make_gdf, monkeypatch
    ):
        """Two requested types share one release lookup, so one download is one snapshot."""
        seen: list[str] = []
        monkeypatch.setattr(
            "earthlens.overture.query.query_overture",
            lambda theme, otype, release, *a, **k: (
                seen.append(release) or make_gdf([PERMISSIVE_SOURCES])
            ),
        )
        backend = _make_backend(
            tmp_path,
            variables={"buildings": ["building", "building_part"]},
            where="height > 10",
        )
        backend.download()
        assert fake_overture.latest_calls == 1, "one lookup for the whole download"
        assert seen == [FAKE_RELEASE, FAKE_RELEASE], "both types read one release"

    def test_default_path_never_resolves_a_release(self, tmp_path: Path, fake_overture):
        """Without where=/columns= the fetch stays offline and asks for no release."""
        _make_backend(tmp_path, variables={"places": []}).download()
        assert fake_overture.latest_calls == 0, (
            "the SDK path auto-targets latest itself; it must not look one up"
        )

    @pytest.mark.parametrize(
        "release", ["not-a-release", "2026-07-22", "2026-7-22.0", ""]
    )
    def test_release_must_be_shaped_like_a_release_id(self, tmp_path: Path, release):
        """A mistyped pin is rejected at construction, not by an opaque S3 miss."""
        with pytest.raises(ValueError, match=r"release must be an Overture release id"):
            _make_backend(tmp_path, variables={"places": []}, release=release)

    def test_resolve_release_explicit_skips_the_live_lookup(
        self, tmp_path: Path, monkeypatch
    ):
        """An explicit release is used without asking the SDK."""
        monkeypatch.setattr("earthlens.overture.releases.latest_release", _boom)
        backend = _make_backend(
            tmp_path, variables={"places": []}, release="2020-01-01.0"
        )
        assert backend._resolve_release() == "2020-01-01.0"

    def test_resolve_release_prefers_the_live_release(
        self, tmp_path: Path, monkeypatch
    ):
        """With no explicit release, the published release beats the bundled index."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = ["2020-01-01.0"]
        _stac(monkeypatch, {"latest": "2099-12-31.0"})
        assert backend._resolve_release() == "2099-12-31.0"

    def test_resolve_release_falls_back_to_index_when_live_fails(
        self, tmp_path: Path, monkeypatch
    ):
        """A failed live lookup falls back to the newest bundled release."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = ["2020-01-01.0", "2021-01-01.0"]
        monkeypatch.setattr("earthlens.overture.releases.latest_release", _boom)
        assert backend._resolve_release() == "2021-01-01.0"

    @pytest.mark.parametrize("live", [None, "", "https:", "2026-7-22.0"])
    def test_resolve_release_rejects_a_live_value_that_is_not_a_release(
        self, tmp_path: Path, monkeypatch, live
    ):
        """A live reply that is not release-shaped falls back like a failure."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = ["2020-01-01.0"]
        _stac(monkeypatch, {"latest": live})
        assert backend._resolve_release() == "2020-01-01.0", (
            f"a live {live!r} must not reach the S3 glob"
        )

    def test_resolve_release_raises_on_a_bad_live_value_with_no_index(
        self, tmp_path: Path, monkeypatch
    ):
        """A junk live reply and an empty index name the junk in the error."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = []
        _stac(monkeypatch, {"latest": "https:"})
        with pytest.raises(RuntimeError, match=r"not a release id"):
            backend._resolve_release()

    @pytest.mark.parametrize(
        "lookup, error",
        [(_missing_sdk, ImportError), (_renamed_sdk, AttributeError)],
    )
    def test_resolve_release_propagates_a_code_level_failure(
        self, tmp_path: Path, monkeypatch, lookup, error
    ):
        """A missing or renamed SDK entry point fails loudly, not into the index."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = ["2020-01-01.0"]
        monkeypatch.setattr("earthlens.overture.releases.latest_release", lookup)
        with pytest.raises(error):
            backend._resolve_release()

    def test_resolve_release_raises_when_live_fails_and_index_empty(
        self, tmp_path: Path, monkeypatch
    ):
        """No live release and no bundled one is an error naming the way out."""
        backend = _make_backend(tmp_path, variables={"places": []})
        backend._catalog.available_releases = []
        monkeypatch.setattr("earthlens.overture.releases.latest_release", _boom)
        with pytest.raises(RuntimeError, match=r"explicit release=") as excinfo:
            backend._resolve_release()
        assert isinstance(excinfo.value.__cause__, ReleaseLookupError), (
            "the live-lookup failure should be chained, not swallowed"
        )


@pytest.mark.overture
class TestStreamToGeodataframe:
    """`_stream_to_geodataframe` batch assembly + early stop."""

    def test_none_reader_returns_empty(self):
        """A `None` reader (no match) yields an empty GeoDataFrame."""
        assert len(_stream_to_geodataframe(None, max_features=None)) == 0

    def test_empty_reader_returns_empty(self, make_gdf):
        """A reader that yields no batches yields an empty GeoDataFrame."""
        import pyarrow as pa

        schema = pa.table(
            make_gdf([PERMISSIVE_SOURCES])
            .set_crs("EPSG:4326")
            .to_arrow(geometry_encoding="WKB")
        ).schema
        reader = pa.RecordBatchReader.from_batches(schema, [])
        assert len(_stream_to_geodataframe(reader, max_features=None)) == 0

    def test_early_stop_and_trim(self, make_gdf):
        """A cap that lands mid-batch trims the overshoot to exactly it."""
        import pyarrow as pa

        gdf = make_gdf([PERMISSIVE_SOURCES] * 5).set_crs("EPSG:4326")
        table = pa.table(gdf.to_arrow(geometry_encoding="WKB"))
        reader = pa.RecordBatchReader.from_batches(
            table.schema, table.to_batches(max_chunksize=2)
        )
        result = _stream_to_geodataframe(reader, max_features=3)
        assert len(result) == 3


@pytest.mark.overture
class TestRequireOverturemaps:
    """`_require_overturemaps` import guard."""

    def test_present_is_noop(self):
        """With the SDK installed the guard is a no-op."""
        assert _require_overturemaps() is None

    def test_missing_raises_friendly(self, monkeypatch: pytest.MonkeyPatch):
        """A missing SDK surfaces as an ImportError naming the extra."""
        monkeypatch.setitem(sys.modules, "overturemaps", None)
        with pytest.raises(ImportError, match=r"earthlens\[overture\]"):
            _require_overturemaps()
