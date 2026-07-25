"""Unit tests for the GBIF occurrence backend."""

from __future__ import annotations

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import Point

from earthlens.biodiversity import LicenseWarning
from earthlens.gbif import GBIF, GBIF_COLUMNS


def _backend(tmp_path, variables=None, **kwargs):
    """Build a GBIF backend over a small bbox and one-year window."""
    return GBIF(
        start="2020-01-01",
        end="2020-12-31",
        variables=variables if variables is not None else ["birds"],
        lat_lim=[0.0, 10.0],
        lon_lim=[0.0, 10.0],
        path=str(tmp_path),
        **kwargs,
    )


@pytest.mark.gbif
class TestConstruction:
    """Construction validates `variables` and `file_format`."""

    def test_output_kind_is_vector(self, tmp_path):
        """The backend declares vector output."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"birds": 1})

    def test_unknown_file_format_rejected(self, tmp_path):
        """An unknown file_format raises a ValueError."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _backend(tmp_path, file_format="shp")


@pytest.mark.gbif
class TestPlanSearch:
    """`_plan_search` builds the `occ.search` kwargs."""

    def test_objective_kwargs(self, tmp_path):
        """The planned kwargs match the objective request key by key."""
        params = _backend(tmp_path)._plan_search()
        assert params["taxonKey"] == 212
        assert params["eventDate"] == "2020-01-01,2020-12-31"
        assert params["hasCoordinate"] is True
        assert params["geometry"] == "POLYGON ((10 0, 10 10, 0 10, 0 0, 10 0))"

    def test_multiple_variables_become_list(self, tmp_path):
        """Several taxa resolve to a list of taxonKeys."""
        params = _backend(tmp_path, variables=["birds", "mammals"])._plan_search()
        assert params["taxonKey"] == [212, 359]


@pytest.mark.gbif
class TestPaging:
    """`_page` loops pages and honours `max_records`."""

    def test_concatenates_two_pages(self, tmp_path, fake_gbif):
        """Two pages (endOfRecords False then True) concatenate both results."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(key=1)],
                    "count": 2,
                    "endOfRecords": False,
                },
                {
                    "results": [fake_gbif.record(key=2)],
                    "count": 2,
                    "endOfRecords": True,
                },
            ]
        )
        rows = _backend(tmp_path)._page(fake_gbif.occurrences, {"taxonKey": 212})
        assert [r["key"] for r in rows] == [1, 2]

    def test_max_records_caps_and_logs(self, tmp_path, fake_gbif, log_messages):
        """`max_records=1` stops after one row and logs the upstream count once."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(key=1), fake_gbif.record(key=2)],
                    "count": 57,
                    "endOfRecords": False,
                }
            ]
        )
        rows = _backend(tmp_path, max_records=1)._page(
            fake_gbif.occurrences, {"taxonKey": 212}
        )
        assert len(rows) == 1
        capped = [m for m in log_messages if "57" in m and "capped" in m]
        assert len(capped) == 1


@pytest.mark.gbif
class TestFetchAndDownload:
    """`download` maps occurrences to a FeatureCollection and writes a file."""

    def test_download_returns_points(self, tmp_path, fake_gbif):
        """A two-record result becomes a two-feature EPSG:4326 Point collection."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(key=1), fake_gbif.record(key=2)],
                    "count": 2,
                    "endOfRecords": True,
                }
            ]
        )
        fc = _backend(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 2
        assert fc.crs.to_epsg() == 4326
        assert isinstance(fc.geometry.iloc[0], Point)

    def test_null_coordinate_null_geometry(self, tmp_path, fake_gbif):
        """A record missing a latitude gets a null geometry."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(decimalLatitude=None)],
                    "count": 1,
                    "endOfRecords": True,
                }
            ]
        )
        fc = _backend(tmp_path).download()
        assert fc.geometry.iloc[0] is None

    def test_cc_by_nc_warns(self, tmp_path, fake_gbif):
        """A CC-BY-NC record raises a LicenseWarning."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(license="CC_BY_NC_4_0")],
                    "count": 1,
                    "endOfRecords": True,
                }
            ]
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path).download()

    def test_all_cc0_no_warning(self, tmp_path, fake_gbif, recwarn):
        """An all-CC0 batch raises no LicenseWarning."""
        fake_gbif.occurrences.set_pages(
            [
                {
                    "results": [fake_gbif.record(license="CC0_1_0")],
                    "count": 1,
                    "endOfRecords": True,
                }
            ]
        )
        _backend(tmp_path).download()
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]

    def test_empty_result_keeps_schema(self, tmp_path, fake_gbif):
        """An empty upstream yields an empty FC carrying exactly GBIF_COLUMNS."""
        fc = _backend(tmp_path).download()
        assert len(fc) == 0
        assert list(fc.columns) == [*GBIF_COLUMNS, "geometry"]

    def test_download_writes_geoparquet(self, tmp_path, fake_gbif):
        """A non-empty result is written to a GeoParquet file under path."""
        fake_gbif.occurrences.set_pages(
            [{"results": [fake_gbif.record()], "count": 1, "endOfRecords": True}]
        )
        _backend(tmp_path).download()
        assert (tmp_path / "gbif_occurrences.parquet").exists()

    def test_aggregate_rejected(self, tmp_path, fake_gbif):
        """A non-None aggregate raises NotImplementedError mentioning vector."""
        with pytest.raises(NotImplementedError, match="vector"):
            _backend(tmp_path).download(aggregate=object())

    def test_api_returns_collection(self, tmp_path, fake_gbif):
        """`_api` returns the occurrence FeatureCollection."""
        fake_gbif.occurrences.set_pages(
            [{"results": [fake_gbif.record()], "count": 1, "endOfRecords": True}]
        )
        assert len(_backend(tmp_path)._api()) == 1

    def test_geojson_write(self, tmp_path, fake_gbif):
        """A non-parquet file_format writes via the OGR driver."""
        fake_gbif.occurrences.set_pages(
            [{"results": [fake_gbif.record()], "count": 1, "endOfRecords": True}]
        )
        _backend(tmp_path, file_format="geojson").download()
        assert (tmp_path / "gbif_occurrences.geojson").exists()

    def test_empty_path_opts_out_of_writing(self, tmp_path, fake_gbif):
        """`path=""` returns the in-memory FC but writes no file."""
        fake_gbif.occurrences.set_pages(
            [{"results": [fake_gbif.record()], "count": 1, "endOfRecords": True}]
        )
        backend = GBIF(
            start="2020-01-01",
            end="2020-12-31",
            variables=["birds"],
            lat_lim=[0.0, 10.0],
            lon_lim=[0.0, 10.0],
            path="",
        )
        fc = backend.download()
        assert len(fc) == 1
        # The parent class still resolves an absolute path under cwd; nothing
        # should be written there with our user-supplied path being empty.
        assert not any(p.exists() for p in (tmp_path.glob("*.parquet")))
