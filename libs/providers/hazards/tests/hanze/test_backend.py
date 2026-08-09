"""Tests for the HANZE backend (filtering, download, geometry, contracts)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.hanze import HANZE
from earthlens.hanze.backend import _as_list, _normalize_country, _normalize_region

from .conftest import EVENTS_NAME, REGIONS_NAME, FakeHttpClient


def _tabular(hanze_root: Path, **kwargs: object) -> HANZE:
    """Construct a tabular HANZE bound to the pre-seeded output dir."""
    return HANZE(path=str(hanze_root), **kwargs)


@pytest.mark.hanze
class TestHelpers:
    """The module-level selector helpers."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, []), ("DE", ["DE"]), (["DE", "NL"], ["DE", "NL"])],
    )
    def test_as_list(self, value: object, expected: list[str]) -> None:
        """A scalar becomes a one-item list; None becomes empty."""
        assert _as_list(value) == expected

    def test_normalize_country_upper(self) -> None:
        """Country codes are upper-cased into a set."""
        assert _normalize_country(["de", "nl"]) == {"DE", "NL"}

    @pytest.mark.parametrize("bad", ["DEU", "D", "12"])
    def test_normalize_country_rejects_non_iso2(self, bad: str) -> None:
        """A non-2-letter code is rejected rather than silently matching nothing."""
        with pytest.raises(ValueError, match="ISO2"):
            _normalize_country([bad])

    def test_normalize_region_upper(self) -> None:
        """NUTS-3 codes are upper-cased into a set."""
        assert _normalize_region(["de300", "nl414"]) == {"DE300", "NL414"}

    @pytest.mark.parametrize("bad", ["DE30", "DE3000", "12345", "D3400"])
    def test_normalize_region_rejects_malformed(self, bad: str) -> None:
        """A code that is not a 5-char NUTS-3 code is rejected."""
        with pytest.raises(ValueError, match="NUTS-3"):
            _normalize_region([bad])


@pytest.mark.hanze
class TestConstruction:
    """Constructor validation and per-instance output kind."""

    def test_default_output_kind_tabular(self, hanze_root: Path) -> None:
        """A plain instance is tabular."""
        assert _tabular(hanze_root).OUTPUT_KIND == "tabular"

    def test_with_geometry_output_kind_vector(self, hanze_root: Path) -> None:
        """`with_geometry=True` makes the instance vector."""
        assert _tabular(hanze_root, with_geometry=True).OUTPUT_KIND == "vector"

    def test_flood_type_case_insensitive(self, hanze_root: Path) -> None:
        """A lower-case flood type resolves to its canonical spelling."""
        assert _tabular(hanze_root, flood_type="river")._flood_types == ["River"]

    def test_unknown_flood_type_raises(self, hanze_root: Path) -> None:
        """An unknown flood type is rejected with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'River'"):
            _tabular(hanze_root, flood_type="Rivers")

    def test_malformed_catalog_guarded(
        self, hanze_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A catalog missing its record/geometry block fails construction."""

        class _Broken:
            record = None
            geometry = None
            columns: dict[str, str] = {}

            def flood_types(self) -> list[str]:
                return []

        monkeypatch.setattr("earthlens.hanze.backend.Catalog", _Broken)
        with pytest.raises(ValueError, match="failed to load"):
            _tabular(hanze_root)


@pytest.mark.hanze
class TestDates:
    """`_check_input_dates` and the derived year range."""

    def test_year_range_from_window(self, hanze_root: Path) -> None:
        """A window exposes its inclusive year bounds."""
        backend = _tabular(hanze_root, start="1950", end="2000")
        assert backend._year_range == (1950, 2000)

    def test_no_window_is_open(self, hanze_root: Path) -> None:
        """An omitted window yields open (None, None) bounds."""
        assert _tabular(hanze_root)._year_range == (None, None)

    def test_inverted_window_raises(self, hanze_root: Path) -> None:
        """A start after the end is rejected."""
        with pytest.raises(ValueError):
            _tabular(hanze_root, start="2010", end="1990")


@pytest.mark.hanze
class TestBbox:
    """The `_bbox` property collapses a world-wide request to None."""

    def test_global_is_none(self, hanze_root: Path) -> None:
        """A whole-globe request carries no bbox restriction."""
        assert _tabular(hanze_root)._bbox is None

    def test_regional_bbox_tuple(self, hanze_root: Path) -> None:
        """A regional request exposes its (min_lon, min_lat, max_lon, max_lat)."""
        backend = _tabular(hanze_root, lat_lim=[51.0, 53.0], lon_lim=[4.0, 6.0])
        assert backend._bbox == (4.0, 51.0, 6.0, 53.0)


@pytest.mark.hanze
class TestFilterEvents:
    """The country / region / type / year / bbox filters."""

    def test_country_filter(self, hanze_root: Path) -> None:
        """`country=` keeps only that country's events."""
        df = _tabular(hanze_root, country="DE").download(progress_bar=False)
        assert sorted(df["Country code"].unique()) == ["DE"]
        assert len(df) == 3

    def test_multi_country_filter(self, hanze_root: Path) -> None:
        """A list of countries keeps every listed one."""
        df = _tabular(hanze_root, country=["DE", "NL"]).download(progress_bar=False)
        assert sorted(df["Country code"].unique()) == ["DE", "NL"]
        assert len(df) == 5

    def test_type_filter(self, hanze_root: Path) -> None:
        """`flood_type=` keeps only that type."""
        df = _tabular(hanze_root, flood_type="Coastal").download(progress_bar=False)
        assert sorted(df["Type"].unique()) == ["Coastal"]

    def test_year_window_filter(self, hanze_root: Path) -> None:
        """A date window keeps events whose year is inside it."""
        df = _tabular(hanze_root, start="1960", end="2005").download(progress_bar=False)
        assert df["Year"].between(1960, 2005).all()
        assert 1875 not in set(df["Year"])

    def test_region_filter(self, hanze_root: Path) -> None:
        """`region=` keeps events referencing that NUTS-3 code."""
        df = _tabular(hanze_root, region="DE711").download(progress_bar=False)
        assert set(df["ID"]) == {1, 2}

    def test_bbox_filter(self, hanze_root: Path) -> None:
        """A bbox keeps events whose affected regions intersect it."""
        df = _tabular(hanze_root, lat_lim=[51.0, 53.0], lon_lim=[4.0, 6.0]).download(
            progress_bar=False
        )
        assert sorted(df["Country code"].unique()) == ["NL"]

    def test_combined_filters(self, hanze_root: Path) -> None:
        """Country and type filters compose (AND)."""
        df = _tabular(hanze_root, country="DE", flood_type="River").download(
            progress_bar=False
        )
        assert set(df["ID"]) == {1, 2}

    def test_region_and_bbox_compose_with_and(self, hanze_root: Path) -> None:
        """`region=` and a disjoint bbox intersect to nothing (AND, not OR)."""
        # NL414 events, but a bbox covering only the DE300 box — the two code
        # restrictions share no event, so a true AND yields an empty result.
        df = _tabular(
            hanze_root,
            region="NL414",
            lat_lim=[52.3, 52.7],
            lon_lim=[13.0, 13.8],
        ).download(progress_bar=False)
        assert len(df) == 0

    def test_empty_bbox_set_drops_every_event(self, hanze_root: Path) -> None:
        """A bbox over open water resolves to no regions and drops every event."""
        df = _tabular(hanze_root, lat_lim=[0.0, 1.0], lon_lim=[-30.0, -29.0]).download(
            progress_bar=False
        )
        assert len(df) == 0


@pytest.mark.hanze
class TestDownloadTabular:
    """Tabular `download` returns and writes the events DataFrame."""

    def test_returns_dataframe(self, hanze_root: Path) -> None:
        """The default download returns a pandas DataFrame."""
        df = _tabular(hanze_root, country="NL").download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)

    def test_writes_csv(self, hanze_root: Path) -> None:
        """A filtered download writes a digest-named CSV under the path."""
        _tabular(hanze_root, country="NL").download(progress_bar=False)
        assert list(hanze_root.glob("hanze_events-*.csv"))

    def test_unfiltered_plain_name(self, hanze_root: Path) -> None:
        """An unfiltered download writes the plain-named CSV."""
        _tabular(hanze_root).download(progress_bar=False)
        assert (hanze_root / "hanze_events.csv").is_file()

    def test_no_match_is_empty(self, hanze_root: Path) -> None:
        """A country with no events returns an empty DataFrame."""
        df = _tabular(hanze_root, country="ES").download(progress_bar=False)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


@pytest.mark.hanze
class TestDownloadVector:
    """`with_geometry` download returns and writes the region FeatureCollection."""

    def test_returns_feature_collection(self, hanze_root: Path) -> None:
        """The geometry download returns a FeatureCollection in WGS84."""
        fc = _tabular(hanze_root, country="DE", with_geometry=True).download(
            progress_bar=False
        )
        assert isinstance(fc, FeatureCollection)
        assert fc.crs.to_epsg() == 4326

    def test_region_counts(self, hanze_root: Path) -> None:
        """DE events resolve to their two affected regions with counts."""
        fc = _tabular(hanze_root, country="DE", with_geometry=True).download(
            progress_bar=False
        )
        counts = dict(zip(fc["nuts3_code"], fc["n_events"]))
        assert counts == {"DE300": 2, "DE711": 2}

    def test_writes_gpkg(self, hanze_root: Path) -> None:
        """A non-empty geometry download writes a GeoPackage."""
        _tabular(hanze_root, country="DE", with_geometry=True).download(
            progress_bar=False
        )
        assert list(hanze_root.glob("hanze_regions-*.gpkg"))

    def test_empty_geometry(self, hanze_root: Path) -> None:
        """A request matching no events returns the empty region FC."""
        fc = _tabular(hanze_root, country="ES", with_geometry=True).download(
            progress_bar=False
        )
        assert len(fc) == 0

    def test_bbox_clips_out_of_box_regions(self, hanze_root: Path) -> None:
        """A bbox drops affected regions that lie outside it (cross-border event)."""
        backend = _tabular(
            hanze_root, with_geometry=True, lat_lim=[51.0, 53.0], lon_lim=[4.0, 6.0]
        )
        # A single event touching an in-box region (NL414) and an out-of-box one
        # (DE300); only the in-box region should survive the clip.
        crafted = pd.DataFrame({"Regions affected (NUTS 3)": ["NL414;DE300"]})
        fc = backend._build_region_collection(crafted)
        assert list(fc["nuts3_code"]) == ["NL414"]


@pytest.mark.hanze
class TestAggregateRejection:
    """Both output kinds refuse `aggregate=` through the base guard."""

    def test_tabular_rejects_aggregate(self, hanze_root: Path) -> None:
        """A tabular download refuses a non-None aggregate."""
        with pytest.raises(NotImplementedError, match="aggregate="):
            _tabular(hanze_root).download(aggregate=object())

    def test_vector_rejects_aggregate(self, hanze_root: Path) -> None:
        """A vector download refuses a non-None aggregate too."""
        with pytest.raises(NotImplementedError):
            _tabular(hanze_root, with_geometry=True).download(aggregate=object())


@pytest.mark.hanze
class TestDownloadAndCache:
    """The download branch fetches through the client and reuses the cache."""

    def test_fetches_when_absent(
        self, tmp_path: Path, fake_http: FakeHttpClient
    ) -> None:
        """A missing events file is fetched once through the client."""
        backend = HANZE(path=str(tmp_path), country="DE")
        backend._http = fake_http  # type: ignore[assignment]
        backend.download(progress_bar=False)
        assert any(EVENTS_NAME in url for url in fake_http.calls)

    def test_reuses_cached_file(
        self, tmp_path: Path, fake_http: FakeHttpClient
    ) -> None:
        """A second download reuses the cached events file, no second fetch."""
        backend = HANZE(path=str(tmp_path), country="DE")
        backend._http = fake_http  # type: ignore[assignment]
        backend.download(progress_bar=False)
        HANZE(path=str(tmp_path), country="DE").download(progress_bar=False)
        assert sum(EVENTS_NAME in url for url in fake_http.calls) == 1

    def test_geometry_download_fetches_regions(
        self, tmp_path: Path, fake_http: FakeHttpClient
    ) -> None:
        """A geometry download fetches the region zip through the client."""
        backend = HANZE(path=str(tmp_path), country="DE", with_geometry=True)
        backend._http = fake_http  # type: ignore[assignment]
        backend.download(progress_bar=False)
        assert any(REGIONS_NAME in url for url in fake_http.calls)

    def test_html_error_page_rejected(self, tmp_path: Path) -> None:
        """An HTML error page served for the events CSV is rejected, not cached."""
        bad = tmp_path / "_src_bad.html"
        bad.write_text("<html>503 Service Unavailable</html>", encoding="utf-8")
        backend = HANZE(path=str(tmp_path), country="DE")
        backend._http = FakeHttpClient({EVENTS_NAME: bad})  # type: ignore[assignment]
        with pytest.raises(ValueError, match="does not start with"):
            backend.download(progress_bar=False)


@pytest.mark.hanze
class TestResultStem:
    """`_result_stem` encodes the request's filters into the output name."""

    def test_unfiltered_plain(self, hanze_root: Path) -> None:
        """An unfiltered request keeps the plain stem."""
        assert _tabular(hanze_root)._result_stem("hanze_events") == "hanze_events"

    def test_filtered_has_digest(self, hanze_root: Path) -> None:
        """A filtered request appends a stable digest."""
        stem = _tabular(hanze_root, country="DE")._result_stem("hanze_events")
        assert stem.startswith("hanze_events-") and len(stem) > len("hanze_events-")

    def test_digest_order_insensitive(self, hanze_root: Path) -> None:
        """The digest is the same regardless of multi-value filter order."""
        one = _tabular(hanze_root, country=["DE", "NL"])._result_stem("x")
        two = _tabular(hanze_root, country=["NL", "DE"])._result_stem("x")
        assert one == two


@pytest.mark.hanze
class TestLoadRegionsGuard:
    """`_load_regions` fails clearly when the shapefile member is missing."""

    def test_missing_shp_raises(
        self, tmp_path: Path, fake_http: FakeHttpClient
    ) -> None:
        """A region zip without the expected .shp stem raises a clear error."""
        bad_zip = tmp_path / REGIONS_NAME
        with zipfile.ZipFile(bad_zip, "w") as archive:
            archive.writestr("other.shp", b"not a shapefile")
        backend = HANZE(path=str(tmp_path), with_geometry=True)
        with pytest.raises(ValueError, match="has no"):
            backend._load_regions()


@pytest.mark.hanze
class TestInternals:
    """Small internal branches: client construction, caching, empty result."""

    def test_client_is_httpclient_and_cached(self, hanze_root: Path) -> None:
        """`_client` builds an HttpClient once and returns the same instance."""
        from earthlens.base.http import HttpClient

        backend = _tabular(hanze_root)
        client = backend._client()
        assert isinstance(client, HttpClient)
        assert backend._client() is client

    def test_load_regions_cached(self, hanze_root: Path) -> None:
        """A second `_load_regions` returns the cached FeatureCollection."""
        backend = _tabular(hanze_root, with_geometry=True)
        first = backend._load_regions()
        assert backend._load_regions() is first

    def test_empty_result_matches_kind(self, hanze_root: Path) -> None:
        """`_empty_result` returns a DataFrame or empty region FC per kind."""
        assert isinstance(_tabular(hanze_root)._empty_result(), pd.DataFrame)
        assert len(_tabular(hanze_root, with_geometry=True)._empty_result()) == 0

    def test_log_citation_without_attribution(self, hanze_root: Path) -> None:
        """A record with no attribution logs nothing and does not error."""
        backend = _tabular(hanze_root)
        backend._record = backend._record.model_copy(update={"attribution": ""})
        backend._log_citation()


@pytest.mark.hanze
class TestNoXarray:
    """The backend never imports a gridded-array library."""

    @pytest.mark.parametrize("module_name", ["backend", "catalog", "geometry"])
    def test_source_has_no_xarray_import(self, module_name: str) -> None:
        """No HANZE source module imports xarray."""
        import earthlens.hanze

        source = (
            Path(earthlens.hanze.__file__).parent / f"{module_name}.py"
        ).read_text(encoding="utf-8")
        assert "import xarray" not in source
