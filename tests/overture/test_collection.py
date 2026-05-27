"""Unit tests for `earthlens.overture.collection`."""

from __future__ import annotations

import pytest

from earthlens.overture._helpers import CDLA_PERMISSIVE, ODBL, LicenseWarning
from earthlens.overture.collection import DEFAULT_LICENSE, empty_fc, to_feature_collection

from .conftest import OSM_SOURCES, PERMISSIVE_SOURCES


@pytest.mark.overture
class TestToFeatureCollection:
    """`to_feature_collection` CRS tagging, licensing, capping."""

    def test_sets_crs_when_missing(self, make_gdf):
        """A CRS-less SDK frame is tagged EPSG:4326."""
        gdf = make_gdf([PERMISSIVE_SOURCES], set_crs=False)
        assert gdf.crs is None
        fc = to_feature_collection(gdf, label="places/place")
        assert fc.crs.to_epsg() == 4326

    def test_adds_license_id_column(self, make_gdf):
        """A `license_id` column is added per row."""
        gdf = make_gdf([PERMISSIVE_SOURCES, OSM_SOURCES])
        fc = to_feature_collection(gdf, label="places/place")
        assert "license_id" in fc.columns
        assert list(fc["license_id"]) == ["Apache-2.0; CDLA-Permissive-2.0", ODBL]

    def test_warns_on_odbl_rows(self, make_gdf):
        """An ODbL row triggers a `LicenseWarning`."""
        gdf = make_gdf([OSM_SOURCES])
        with pytest.warns(LicenseWarning):
            to_feature_collection(gdf, label="buildings/building")

    def test_no_warning_for_permissive_only(self, make_gdf, recwarn):
        """A permissive-only frame emits no `LicenseWarning`."""
        gdf = make_gdf([PERMISSIVE_SOURCES])
        to_feature_collection(gdf, label="places/place")
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]

    def test_max_features_caps_and_warns(self, make_gdf):
        """`max_features` truncates the frame and logs a warning."""
        gdf = make_gdf([PERMISSIVE_SOURCES] * 5)
        fc = to_feature_collection(gdf, label="places/place", max_features=2)
        assert len(fc) == 2

    def test_max_features_above_count_keeps_all(self, make_gdf):
        """A cap above the row count keeps every row."""
        gdf = make_gdf([PERMISSIVE_SOURCES] * 3)
        fc = to_feature_collection(gdf, label="places/place", max_features=10)
        assert len(fc) == 3

    def test_empty_input_returns_empty_fc(self, make_gdf):
        """A zero-row input yields a schema-correct empty collection."""
        gdf = make_gdf([])
        fc = to_feature_collection(gdf, label="places/place")
        assert len(fc) == 0
        assert "license_id" in fc.columns

    def test_none_input_returns_empty_fc(self):
        """A `None` input yields the empty collection."""
        fc = to_feature_collection(None, label="places/place")
        assert len(fc) == 0

    def test_does_not_mutate_input_frame(self, make_gdf):
        """The input gdf is not mutated (no `license_id` leaks back onto it)."""
        gdf = make_gdf([PERMISSIVE_SOURCES])
        to_feature_collection(gdf, label="places/place")
        assert "license_id" not in gdf.columns


@pytest.mark.overture
class TestEmptyFc:
    """`empty_fc` schema and CRS."""

    def test_schema_and_crs(self):
        """The empty collection has the minimal schema and EPSG:4326."""
        fc = empty_fc()
        assert len(fc) == 0
        assert {"id", "license_id"}.issubset(fc.columns)
        assert "geometry" in fc.columns
        assert fc.crs.to_epsg() == 4326

    def test_default_license_constant(self):
        """`DEFAULT_LICENSE` re-exports the CDLA-Permissive fallback."""
        assert DEFAULT_LICENSE == CDLA_PERMISSIVE
