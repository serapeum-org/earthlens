"""Tests for the EM-DAT backend."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.emdat import EMDAT
from earthlens.emdat.backend import LARGE_DOWNLOAD_MB

from .conftest import FakeEarthaccess, FakeGranule, FakeHttp

_GDIS_LINK = (
    "https://data.earthdata.nasa.gov/nasa-earth/human-dimensions/sedac-root/"
    "downloads/data/pend/pend-gdis-1960-2018/"
    "pend-gdis-1960-2018-disasterlocations-csv.zip"
)


def _events_backend(tmp_path: Path, http: FakeHttp, **overrides: Any) -> EMDAT:
    """Build an events backend with its HTTP client already stubbed."""
    params: dict[str, Any] = dict(variables=["emdat:events"], path=str(tmp_path))
    params.update(overrides)
    backend = EMDAT(**params)
    backend._http = http
    return backend


def _points_backend(tmp_path: Path, **overrides: Any) -> EMDAT:
    """Build a `gdis:points` backend that skips the real login."""
    params: dict[str, Any] = dict(variables=["gdis:points"], path=str(tmp_path))
    params.update(overrides)
    backend = EMDAT(**params)
    backend._auth = None
    return backend


@pytest.mark.emdat
class TestConstruction:
    """Validation and per-instance wiring at construction time."""

    @pytest.mark.parametrize(
        ("dataset_id", "kind"),
        [
            ("emdat:events", "tabular"),
            ("gdis:points", "vector"),
            ("gdis:polygons", "vector"),
        ],
    )
    def test_output_kind_is_per_instance(
        self, tmp_path: Path, dataset_id: str, kind: str
    ) -> None:
        """The resolved row's output kind lands on the instance."""
        backend = EMDAT(variables=[dataset_id], path=str(tmp_path))
        assert backend.OUTPUT_KIND == kind

    def test_class_default_is_unchanged_by_an_instance(self, tmp_path: Path) -> None:
        """Setting the instance attribute does not mutate the class."""
        EMDAT(variables=["gdis:points"], path=str(tmp_path))
        assert EMDAT.OUTPUT_KIND == "tabular"

    def test_mapping_variables_rejected(self, tmp_path: Path) -> None:
        """A mapping is the other backends' shape and is refused here."""
        with pytest.raises(TypeError, match="not a mapping"):
            EMDAT(variables={"emdat:events": []}, path=str(tmp_path))

    @pytest.mark.parametrize("variables", [None, [], ["emdat:events", "gdis:points"]])
    def test_exactly_one_dataset_required(
        self, tmp_path: Path, variables: list[str] | None
    ) -> None:
        """Zero or several ids cannot resolve one output kind."""
        with pytest.raises(ValueError, match="exactly one dataset id"):
            EMDAT(variables=variables, path=str(tmp_path))

    def test_duplicate_ids_collapse(self, tmp_path: Path) -> None:
        """The same id twice is still one dataset."""
        backend = EMDAT(variables=["gdis:points", "gdis:points"], path=str(tmp_path))
        assert backend._dataset.id == "gdis:points"

    def test_hazard_string_is_normalised(self, tmp_path: Path) -> None:
        """A single hazard string becomes a canonical one-element list."""
        backend = EMDAT(
            variables=["gdis:points"], hazard="  FLOOD ", path=str(tmp_path)
        )
        assert backend._hazards == ["flood"]

    def test_hazard_list_is_normalised(self, tmp_path: Path) -> None:
        """Every hazard in a list is normalised."""
        backend = EMDAT(
            variables=["gdis:points"],
            hazard=["Flood", "extreme temperature "],
            path=str(tmp_path),
        )
        assert backend._hazards == ["flood", "extreme temperature"]

    def test_unknown_hazard_rejected(self, tmp_path: Path) -> None:
        """An unknown hazard fails at construction, not mid-download."""
        with pytest.raises(ValueError, match="not a disaster type"):
            EMDAT(variables=["gdis:points"], hazard="floods", path=str(tmp_path))

    @pytest.mark.parametrize(
        "hazard", ["wildfire", "epidemic", "industrial accident (general)"]
    )
    def test_archive_accepts_types_gdis_never_geocoded(
        self, tmp_path: Path, hazard: str
    ) -> None:
        """The archive covers technological and other types GDIS lacks."""
        backend = EMDAT(variables=["emdat:events"], hazard=hazard, path=str(tmp_path))
        assert backend._hazards == [hazard]

    @pytest.mark.parametrize("hazard", ["wildfire", "epidemic"])
    def test_gdis_still_rejects_those_types(self, tmp_path: Path, hazard: str) -> None:
        """The same type is refused on GDIS, whose data does not contain it."""
        with pytest.raises(ValueError, match="gdis:points"):
            EMDAT(variables=["gdis:points"], hazard=hazard, path=str(tmp_path))

    @pytest.mark.parametrize("country", ["BG", "BANGLA", "B1D", ""])
    def test_bad_country_code_rejected(self, tmp_path: Path, country: str) -> None:
        """A malformed ISO3 fails loudly rather than filtering everything away."""
        with pytest.raises(ValueError, match="3-letter ISO3"):
            EMDAT(variables=["emdat:events"], country=country, path=str(tmp_path))

    def test_country_is_optional(self, tmp_path: Path) -> None:
        """Omitting the country keeps every country."""
        backend = EMDAT(variables=["emdat:events"], path=str(tmp_path))
        assert backend._country is None

    def test_events_builds_no_auth(self, tmp_path: Path) -> None:
        """The Dataverse route is anonymous, so no auth object is built."""
        assert EMDAT(variables=["emdat:events"], path=str(tmp_path))._auth is None

    def test_gdis_builds_auth(self, tmp_path: Path) -> None:
        """The GDIS route needs Earthdata Login, so it builds an auth object."""
        assert EMDAT(variables=["gdis:points"], path=str(tmp_path))._auth is not None

    def test_authenticate_is_a_noop_without_auth(self, tmp_path: Path) -> None:
        """Authenticating an anonymous request does nothing and returns self."""
        backend = EMDAT(variables=["emdat:events"], path=str(tmp_path))
        assert backend.authenticate() is backend

    def test_authenticate_configures_gdis_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GDIS request configures its Earthdata Login on authenticate."""
        backend = EMDAT(variables=["gdis:points"], path=str(tmp_path))
        called: list[bool] = []
        monkeypatch.setattr(backend._auth, "configure", lambda: called.append(True))
        backend.authenticate()
        assert called == [True]

    def test_http_client_is_built_once(self, tmp_path: Path) -> None:
        """The pooled client is created lazily and then reused."""
        backend = EMDAT(variables=["emdat:events"], path=str(tmp_path))
        assert backend._http is None
        first = backend._client()
        assert backend._client() is first


@pytest.mark.emdat
class TestRequestWindows:
    """How the request's time and space bounds are interpreted."""

    def test_no_window_is_open_ended(self, tmp_path: Path) -> None:
        """Omitting both dates means the whole record."""
        backend = EMDAT(variables=["emdat:events"], path=str(tmp_path))
        assert backend._year_range == (None, None)

    def test_only_years_matter(self, tmp_path: Path) -> None:
        """The window collapses to inclusive year bounds."""
        backend = EMDAT(
            variables=["emdat:events"],
            start="1990-06-15",
            end="2000-02-01",
            path=str(tmp_path),
        )
        assert backend._year_range == (1990, 2000)

    def test_global_request_has_no_bbox(self, tmp_path: Path) -> None:
        """A world-wide request applies no bbox, so uncoordinated rows survive."""
        backend = EMDAT(variables=["emdat:events"], path=str(tmp_path))
        assert backend._bbox is None

    def test_explicit_bounds_become_a_bbox(self, tmp_path: Path) -> None:
        """Explicit limits become a `(min_lon, min_lat, max_lon, max_lat)` box."""
        backend = EMDAT(
            variables=["emdat:events"],
            lat_lim=[20.5, 26.7],
            lon_lim=[88.0, 92.7],
            path=str(tmp_path),
        )
        assert backend._bbox == (88.0, 20.5, 92.7, 26.7)

    def test_start_after_end_rejected(self, tmp_path: Path) -> None:
        """A backwards window is refused."""
        with pytest.raises(ValueError):
            EMDAT(
                variables=["emdat:events"],
                start="2010-01-01",
                end="2000-01-01",
                path=str(tmp_path),
            )


@pytest.mark.emdat
class TestEventsRoute:
    """The anonymous Dataverse route end to end, with stubbed transport."""

    def test_returns_a_dataframe(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """A tabular dataset resolves to a DataFrame."""
        http = FakeHttp(dataverse_listing, events_workbook)
        result = _events_backend(tmp_path, http).download()
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 5

    def test_resolves_the_file_by_pattern(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """The download targets the archive's numeric id, found by pattern."""
        http = FakeHttp(dataverse_listing, events_workbook)
        _events_backend(tmp_path, http).download()
        assert http.calls[-1][1].endswith("/api/access/datafile/1")

    def test_filters_are_applied(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """Hazard, country and window narrow the returned rows."""
        http = FakeHttp(dataverse_listing, events_workbook)
        backend = _events_backend(
            tmp_path,
            http,
            hazard="flood",
            country="TST",
            start="2009-01-01",
            end="2009-12-31",
        )
        result = backend.download()
        assert result["DisNo."].tolist() == ["2009-0001-TST"]

    def test_result_is_written_to_the_output_directory(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """A tabular result is also written as CSV for the caller."""
        http = FakeHttp(dataverse_listing, events_workbook)
        _events_backend(tmp_path, http).download()
        assert (tmp_path / "emdat_events.csv").is_file()

    def test_source_file_is_reused(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """A second call reuses the already-fetched workbook."""
        http = FakeHttp(dataverse_listing, events_workbook)
        backend = _events_backend(tmp_path, http)
        backend.download()
        backend.download()
        downloads = [call for call in http.calls if call[0] == "download"]
        assert len(downloads) == 1

    def test_license_warning_is_raised(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """The restricted-use archive warns about who may use it."""
        http = FakeHttp(dataverse_listing, events_workbook)
        with pytest.warns(LicenseWarning, match="academic organisations"):
            _events_backend(tmp_path, http).download()

    def test_license_warning_names_the_terms(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """The warning links the terms rather than only naming the licence."""
        http = FakeHttp(dataverse_listing, events_workbook)
        with pytest.warns(LicenseWarning, match="doc.emdat.be"):
            _events_backend(tmp_path, http).download()

    def test_empty_result_is_still_a_frame(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """A request matching nothing yields an empty frame, not an error."""
        http = FakeHttp(dataverse_listing, events_workbook)
        backend = _events_backend(tmp_path, http, country="ZZZ")
        result = backend.download()
        assert isinstance(result, pd.DataFrame)
        assert result.empty


@pytest.mark.emdat
class TestGdisPointsRoute:
    """The GDIS centroid route, with `earthaccess` stubbed."""

    def test_returns_a_feature_collection(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vector dataset resolves to a point FeatureCollection."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        result = _points_backend(tmp_path).download()
        assert set(result.geometry.geom_type) == {"Point"}
        assert result.crs == "EPSG:4326"

    def test_searches_the_catalogued_collection(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CMR query uses the catalog's collection short name."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        _points_backend(tmp_path).download()
        assert fake.searched[0]["short_name"] == "CIESIN_SEDAC_PEND_GDIS"

    def test_filters_are_applied(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hazard and window narrow the returned features."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        backend = _points_backend(
            tmp_path, hazard="flood", start="2009-01-01", end="2009-12-31"
        )
        result = backend.download()
        assert result["disasterno"].tolist() == ["2009-0001"]

    def test_no_license_warning_for_cc_by(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GDIS is CC-BY-4.0, so it raises no restricted-use warning."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _points_backend(tmp_path).download()
        assert not [w for w in caught if issubclass(w.category, LicenseWarning)]

    def test_granule_is_reused(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-downloaded granule is not fetched again."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        backend = _points_backend(tmp_path)
        backend.download()
        backend.download()
        assert len(fake.searched) == 1

    def test_empty_download_result_is_reported(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download that produced no file names the EULA step in its error."""
        fake = FakeEarthaccess([FakeGranule(_GDIS_LINK)], gdis_csv_zip)
        monkeypatch.setattr(fake, "download", lambda *a, **k: [])
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        with pytest.raises(OSError, match="unaccepted_eulas"):
            _points_backend(tmp_path).download()

    def test_missing_granule_is_reported(
        self, tmp_path: Path, gdis_csv_zip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A collection without the catalogued granule names what it did have."""
        fake = FakeEarthaccess([FakeGranule("https://example.invalid/other.zip")])
        monkeypatch.setitem(sys.modules, "earthaccess", fake)
        with pytest.raises(ValueError, match="is not in CMR collection"):
            _points_backend(tmp_path).download()


@pytest.mark.emdat
class TestGdisPolygonsRoute:
    """Reading the GDIS footprint GeoPackage."""

    def test_reads_polygons(self, tmp_path: Path, gdis_gpkg: Path) -> None:
        """The GeoPackage layer resolves to polygon features."""
        backend = EMDAT(variables=["gdis:polygons"], path=str(tmp_path))
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert set(result.geometry.geom_type) == {"Polygon"}
        assert len(result) == 4

    def test_hazard_push_down(self, tmp_path: Path, gdis_gpkg: Path) -> None:
        """The hazard filter is applied by the driver."""
        backend = EMDAT(variables=["gdis:polygons"], hazard="flood", path=str(tmp_path))
        assert len(backend._read_gdis_gpkg(gdis_gpkg)) == 2

    def test_trailing_space_value_is_matched(
        self, tmp_path: Path, gdis_gpkg: Path
    ) -> None:
        """The canonical name matches the file's space-padded spelling."""
        backend = EMDAT(
            variables=["gdis:polygons"],
            hazard="extreme temperature",
            path=str(tmp_path),
        )
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert result["disastertype"].tolist() == ["extreme temperature "]

    def test_country_push_down(self, tmp_path: Path, gdis_gpkg: Path) -> None:
        """The ISO3 filter reaches the driver too."""
        backend = EMDAT(variables=["gdis:polygons"], country="oth", path=str(tmp_path))
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert result["iso3"].tolist() == ["OTH"]

    def test_year_comes_from_the_id_prefix(
        self, tmp_path: Path, gdis_gpkg: Path
    ) -> None:
        """With no year column the window is applied via the disasterno prefix."""
        backend = EMDAT(
            variables=["gdis:polygons"],
            hazard="flood",
            start="2005-01-01",
            end="2015-12-31",
            path=str(tmp_path),
        )
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert result["disasterno"].tolist() == ["2009-0001"]

    def test_open_ended_window_skips_the_year_filter(
        self, tmp_path: Path, gdis_gpkg: Path
    ) -> None:
        """No window means no year filtering at all."""
        backend = EMDAT(variables=["gdis:polygons"], path=str(tmp_path))
        assert len(backend._read_gdis_gpkg(gdis_gpkg)) == 4

    def test_open_start_window(self, tmp_path: Path, gdis_gpkg: Path) -> None:
        """A `None` lower bound keeps everything up to the upper bound."""
        backend = EMDAT(
            variables=["gdis:polygons"],
            hazard="flood",
            end="2000-12-31",
            path=str(tmp_path),
        )
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert result["disasterno"].tolist() == ["1995-0002"]

    def test_bbox_push_down(self, tmp_path: Path, gdis_gpkg: Path) -> None:
        """The spatial filter is applied by the driver."""
        backend = EMDAT(
            variables=["gdis:polygons"],
            lat_lim=[1.5, 4.0],
            lon_lim=[1.5, 4.0],
            path=str(tmp_path),
        )
        result = backend._read_gdis_gpkg(gdis_gpkg)
        assert result["disasterno"].tolist() == ["1995-0002"]

    def test_format_routes_to_the_geopackage_reader(
        self, tmp_path: Path, gdis_gpkg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `gpkg` row is dispatched to the GeoPackage reader, not the CSV one."""
        backend = EMDAT(variables=["gdis:polygons"], path=str(tmp_path))
        monkeypatch.setattr(backend, "_download_granule", lambda: gdis_gpkg)
        monkeypatch.setattr(
            "earthlens.emdat._helpers.extract_member",
            lambda archive, member, dest: gdis_gpkg,
        )
        result = backend._fetch_gdis()
        assert set(result.geometry.geom_type) == {"Polygon"}


@pytest.mark.emdat
class TestLargeDownloadWarning:
    """The guard in front of the multi-gigabyte GeoPackage."""

    def test_polygons_warn(self, tmp_path: Path, warning_messages: list[str]) -> None:
        """The 2.2 GB granule announces itself before being fetched."""
        backend = EMDAT(variables=["gdis:polygons"], path=str(tmp_path))
        backend._warn_if_large()
        assert any("GB" in message for message in warning_messages)

    def test_warning_points_at_the_cheap_alternative(
        self, tmp_path: Path, warning_messages: list[str]
    ) -> None:
        """The warning names the 1 MB source the caller probably wanted."""
        EMDAT(variables=["gdis:polygons"], path=str(tmp_path))._warn_if_large()
        assert any("gdis:points" in message for message in warning_messages)

    def test_points_do_not_warn(
        self, tmp_path: Path, warning_messages: list[str]
    ) -> None:
        """The 1 MB granule is fetched without ceremony."""
        backend = EMDAT(variables=["gdis:points"], path=str(tmp_path))
        backend._warn_if_large()
        assert warning_messages == []

    def test_threshold_sits_between_the_two(self, tmp_path: Path) -> None:
        """The threshold separates the CSV from the GeoPackage."""
        points = EMDAT(variables=["gdis:points"], path=str(tmp_path))
        polygons = EMDAT(variables=["gdis:polygons"], path=str(tmp_path))
        assert points._dataset.download_mb < LARGE_DOWNLOAD_MB
        assert polygons._dataset.download_mb > LARGE_DOWNLOAD_MB


@pytest.mark.emdat
class TestNoGriddedDependency:
    """The backend must not pull in a gridded-array stack."""

    def test_modules_do_not_import_xarray(self) -> None:
        """No EM-DAT module names xarray."""
        import earthlens.emdat

        root = Path(earthlens.emdat.__file__).parent
        sources = list(root.glob("*.py"))
        assert sources
        assert not [
            path
            for path in sources
            if "import xarray" in path.read_text(encoding="utf-8")
        ]

    def test_xarray_is_not_imported_by_a_download(
        self, tmp_path: Path, dataverse_listing: dict[str, Any], events_workbook: Path
    ) -> None:
        """A completed events download leaves no xarray import behind it."""
        sys.modules.pop("xarray", None)
        http = FakeHttp(dataverse_listing, events_workbook)
        _events_backend(tmp_path, http).download()
        assert "xarray" not in sys.modules
