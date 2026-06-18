"""Unit tests for the OBIS occurrence backend."""

from __future__ import annotations

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import Point

from earthlens.biodiversity import LicenseWarning
from earthlens.obis import OBIS, OBIS_COLUMNS


def _backend(tmp_path, variables=None, **kwargs):
    """Build an OBIS backend over a small bbox and a multi-year window."""
    return OBIS(
        start="2015-01-01",
        end="2020-12-31",
        variables=variables if variables is not None else ["common-dolphin"],
        lat_lim=[30.0, 45.0],
        lon_lim=[-10.0, 5.0],
        path=str(tmp_path),
        **kwargs,
    )


@pytest.mark.obis
class TestConstruction:
    """Construction validates `variables` and `file_format`."""

    def test_output_kind_is_vector(self, tmp_path):
        """The backend declares vector output."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"blue-whale": 1})

    def test_empty_variables_rejected(self, tmp_path):
        """An empty `variables` raises a ValueError."""
        with pytest.raises(ValueError, match="at least one species"):
            _backend(tmp_path, variables=[])

    def test_unknown_file_format_rejected(self, tmp_path):
        """An unknown file_format raises a ValueError."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _backend(tmp_path, file_format="shp")


@pytest.mark.obis
class TestPlanSearch:
    """`_plan_search` builds the `occurrences.search` kwargs."""

    def test_objective_kwargs(self, tmp_path):
        """The planned kwargs match the objective request key by key."""
        params = _backend(tmp_path)._plan_search()
        assert params["scientificname"] == "Delphinus delphis"
        assert params["startdate"] == "2015-01-01"
        assert params["enddate"] == "2020-12-31"
        assert params["geometry"] == "POLYGON ((5 30, 5 45, -10 45, -10 30, 5 30))"
        assert params["size"] == 10_000


@pytest.mark.obis
class TestFetchAndDownload:
    """`download` consumes the 1.x DataFrame return and maps it to an FC."""

    def test_consumes_execute_dataframe(self, tmp_path, fake_obis):
        """The backend reads the `.execute()` DataFrame, not a results dict."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(id="a"), fake_obis.row(id="b")])
        )
        fc = _backend(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 2
        assert fc.crs.to_epsg() == 4326
        assert isinstance(fc.geometry.iloc[0], Point)

    def test_null_coordinate_null_geometry(self, tmp_path, fake_obis):
        """A record missing a latitude gets a null geometry."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(decimalLatitude=None)])
        )
        fc = _backend(tmp_path).download()
        assert fc.geometry.iloc[0] is None

    def test_cc_by_nc_warns(self, tmp_path, fake_obis):
        """A CC-BY-NC row raises a LicenseWarning."""
        fake_obis.occurrences.set_frame(
            fake_obis.frame([fake_obis.row(license="CC-BY-NC")])
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path).download()

    def test_cc_by_no_warning(self, tmp_path, fake_obis, recwarn):
        """A plain CC-BY-4.0 batch raises no LicenseWarning."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path).download()
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]

    def test_empty_result_keeps_schema(self, tmp_path, fake_obis):
        """An empty DataFrame yields an empty FC carrying exactly OBIS_COLUMNS."""
        fc = _backend(tmp_path).download()
        assert len(fc) == 0
        assert list(fc.columns) == [*OBIS_COLUMNS, "geometry"]

    def test_download_writes_geoparquet(self, tmp_path, fake_obis):
        """A non-empty result is written to a GeoParquet file under path."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path).download()
        assert (tmp_path / "obis_occurrences.parquet").exists()

    def test_multiple_species_unioned(self, tmp_path, fake_obis):
        """Several species each issue a search and their rows are concatenated."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        fc = _backend(tmp_path, variables=["blue-whale", "common-dolphin"]).download()
        names = [c["scientificname"] for c in fake_obis.occurrences.calls]
        assert names == ["Balaenoptera musculus", "Delphinus delphis"]
        assert len(fc) == 2

    def test_aggregate_rejected(self, tmp_path, fake_obis):
        """A non-None aggregate raises NotImplementedError mentioning vector."""
        with pytest.raises(NotImplementedError, match="vector"):
            _backend(tmp_path).download(aggregate=object())

    def test_api_returns_collection(self, tmp_path, fake_obis):
        """`_api` returns the occurrence FeatureCollection."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        assert len(_backend(tmp_path)._api()) == 1

    def test_geojson_write(self, tmp_path, fake_obis):
        """A non-parquet file_format writes via the OGR driver."""
        fake_obis.occurrences.set_frame(fake_obis.frame([fake_obis.row()]))
        _backend(tmp_path, file_format="geojson").download()
        assert (tmp_path / "obis_occurrences.geojson").exists()
