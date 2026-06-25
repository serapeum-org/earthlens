"""Unit tests for the shared biodiversity-cluster helpers."""

from __future__ import annotations

import warnings

import pandas as pd
import pytest
from shapely.geometry import Point

from earthlens.base import SpatialExtent
from earthlens.biodiversity import (
    IUCN_LICENSE,
    LicenseWarning,
    WDPA_LICENSE,
    occurrences_to_fc,
    warn_license,
    wkt_from_bbox,
)

COLUMNS = {"id": "string", "name": "string", "lat": "float64", "lon": "float64"}


def _fc(records):
    """Build a FeatureCollection from records using the cluster column map."""
    return occurrences_to_fc(records, lat_field="lat", lon_field="lon", columns=COLUMNS)


@pytest.mark.biodiversity
class TestWktFromBbox:
    """`wkt_from_bbox` turns a SpatialExtent's bbox into a WKT polygon."""

    def test_corners_match_bbox(self):
        """The WKT spans exactly the extent's west/south/east/north edges."""
        extent = SpatialExtent.from_pairs(lat_lim=(10.0, 20.0), lon_lim=(0.0, 5.0))
        assert wkt_from_bbox(extent) == "POLYGON ((5 10, 5 20, 0 20, 0 10, 5 10))"

    def test_is_polygon_wkt(self):
        """The result is a POLYGON WKT string."""
        extent = SpatialExtent.from_pairs(lat_lim=(-1.0, 1.0), lon_lim=(-2.0, 2.0))
        assert wkt_from_bbox(extent).startswith("POLYGON ((")


@pytest.mark.biodiversity
class TestOccurrencesToFc:
    """`occurrences_to_fc` maps occurrence rows to a points FeatureCollection."""

    def test_one_row_point(self):
        """A single record becomes a one-feature EPSG:4326 Point collection."""
        fc = _fc([{"id": "a", "name": "x", "lat": 12.0, "lon": 3.0}])
        assert len(fc) == 1
        assert fc.crs.to_epsg() == 4326
        assert fc.geometry.iloc[0] == Point(3.0, 12.0)

    def test_missing_coordinate_null_geometry(self):
        """A record with a missing coordinate gets a null geometry, not POINT(nan nan)."""
        fc = _fc([{"id": "a", "name": "x", "lat": None, "lon": 4.0}])
        assert fc.geometry.iloc[0] is None

    def test_empty_records_keep_schema(self):
        """An empty input yields an empty FeatureCollection carrying exactly the columns."""
        fc = _fc([])
        assert len(fc) == 0
        assert list(fc.columns) == [*COLUMNS, "geometry"]
        assert fc.crs.to_epsg() == 4326

    def test_dataframe_input_matches_list_input(self):
        """A DataFrame input (OBIS shape) produces the same FC as the equivalent list."""
        records = [{"id": "a", "name": "x", "lat": 12.0, "lon": 3.0}]
        from_list = _fc(records)
        from_frame = _fc(pd.DataFrame(records))
        assert isinstance(from_frame.geometry.iloc[0], Point)
        assert len(from_frame) == len(from_list) == 1
        assert from_frame.geometry.iloc[0] == from_list.geometry.iloc[0]

    def test_empty_dataframe_keeps_schema(self):
        """An empty DataFrame input yields the schema-correct empty FC."""
        fc = _fc(pd.DataFrame(columns=list(COLUMNS)))
        assert len(fc) == 0
        assert list(fc.columns) == [*COLUMNS, "geometry"]

    def test_nondefault_index_keeps_geometry(self):
        """A DataFrame with a non-default index keeps its point geometries."""
        frame = pd.DataFrame(
            [
                {"id": "a", "name": "x", "lat": 1.0, "lon": 2.0},
                {"id": "b", "name": "y", "lat": 3.0, "lon": 4.0},
            ],
            index=[5, 7],
        )
        fc = _fc(frame)
        assert isinstance(fc.geometry.iloc[0], Point)
        assert fc.geometry.iloc[0] == Point(2.0, 1.0)
        assert fc.geometry.notna().all()

    def test_columns_restricted_and_ordered(self):
        """Extra record keys are dropped and columns come out in the declared order."""
        fc = _fc([{"id": "a", "name": "x", "lat": 1.0, "lon": 2.0, "extra": "drop"}])
        assert list(fc.columns) == [*COLUMNS, "geometry"]


@pytest.mark.biodiversity
class TestWarnLicense:
    """`warn_license` warns only for restrictive licenses."""

    def test_restrictive_license_warns(self):
        """A non-commercial CC license raises a LicenseWarning and returns True."""
        with pytest.warns(LicenseWarning):
            assert warn_license("CC_BY_NC_4_0", "gbif") is True

    def test_permissive_license_silent(self):
        """A CC0 license raises nothing and returns False."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", LicenseWarning)
            assert warn_license("CC0_1_0", "gbif") is False

    def test_wdpa_and_iucn_sentinels_warn(self):
        """The WDPA and IUCN sentinel labels always warn."""
        with pytest.warns(LicenseWarning):
            assert warn_license(WDPA_LICENSE, "wdpa") is True
        with pytest.warns(LicenseWarning):
            assert warn_license(IUCN_LICENSE, "iucn") is True

    def test_detail_appended_to_message(self):
        """A supplied detail is appended to the warning message."""
        with pytest.warns(LicenseWarning, match="commercial use needs permission"):
            warn_license(WDPA_LICENSE, "wdpa", detail="commercial use needs permission")


@pytest.mark.biodiversity
class TestLicenseWarningPromotion:
    """The promoted LicenseWarning is the same class Overture re-exports."""

    def test_identity_across_import_paths(self):
        """Overture's helper and public re-exports resolve to the shared class."""
        from earthlens.overture import LicenseWarning as overture_public
        from earthlens.overture._helpers import LicenseWarning as overture_helper

        assert overture_helper is LicenseWarning
        assert overture_public is LicenseWarning


@pytest.mark.biodiversity
class TestParseRetryAfter:
    """`parse_retry_after` covers both RFC 9110 §10.2.3 forms."""

    def test_seconds_integer(self):
        """An integer number of seconds parses to a float."""
        from earthlens.biodiversity import parse_retry_after

        assert parse_retry_after("7") == 7.0

    def test_seconds_float(self):
        """A float number of seconds parses too (RFC allows decimal)."""
        from earthlens.biodiversity import parse_retry_after

        assert parse_retry_after("1.5") == 1.5

    def test_none_and_empty(self):
        """`None` or an empty string yields `None`."""
        from earthlens.biodiversity import parse_retry_after

        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None

    def test_http_date_far_future(self):
        """An HTTP-date in the distant future yields a positive delta."""
        from earthlens.biodiversity import parse_retry_after

        wait = parse_retry_after("Fri, 31 Dec 2099 23:59:59 GMT")
        assert wait is not None and wait > 365 * 24 * 3600

    def test_http_date_past_clamps_to_zero(self):
        """An HTTP-date already past yields `0.0`, not a negative wait."""
        from earthlens.biodiversity import parse_retry_after

        assert parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT") == 0.0

    def test_malformed_value_is_none(self):
        """An unparseable value yields `None` so the caller can fall back."""
        from earthlens.biodiversity import parse_retry_after

        assert parse_retry_after("definitely not a date") is None

    def test_both_shims_reach_the_same_function(self):
        """The IUCN and WDPA shims re-export the shared helper as the same object."""
        from earthlens.biodiversity import parse_retry_after
        from earthlens.iucn._rest import _parse_retry_after as iucn_alias
        from earthlens.wdpa._rest import _parse_retry_after as wdpa_alias

        assert iucn_alias is parse_retry_after
        assert wdpa_alias is parse_retry_after
