"""Tests for the HANZE event -> region-geometry join helpers."""

from __future__ import annotations

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.hanze.geometry import (
    REGION_COLUMNS,
    empty_region_fc,
    event_region_counts,
    join_events_to_regions,
    split_nuts3,
)

_REGIONS_COLUMN = "Regions affected (NUTS 3)"


def _read_regions(region_zip, tmp_path) -> FeatureCollection:
    """Extract and read the fixture region shapefile as a FeatureCollection."""
    from earthlens.base.archive import extract_members

    members = extract_members(
        region_zip, tmp_path / "ex", include=(".shp", ".shx", ".dbf", ".prj", ".cpg")
    )
    shp = next(m for m in members if m.suffix.lower() == ".shp")
    return FeatureCollection.read_file(str(shp))


@pytest.mark.hanze
class TestSplitNuts3:
    """`split_nuts3` splits a semicolon list, dropping blanks and non-strings."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("AL011;AL012;AL013", ["AL011", "AL012", "AL013"]),
            ("AL011; AL012 ;;AL013", ["AL011", "AL012", "AL013"]),
            ("  DE300  ", ["DE300"]),
            ("", []),
            (";;", []),
            (None, []),
            (12345, []),
        ],
    )
    def test_split(self, value: object, expected: list[str]) -> None:
        """A cell splits into its codes; blanks and non-strings yield nothing."""
        assert split_nuts3(value) == expected


@pytest.mark.hanze
class TestEventRegionCounts:
    """`event_region_counts` counts events per NUTS-3 code."""

    def test_counts_per_code(self) -> None:
        """Each code is counted once per event that lists it."""
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300;DE711", "DE300", "NL414"]})
        counts = event_region_counts(events, _REGIONS_COLUMN)
        assert counts == {"DE300": 2, "DE711": 1, "NL414": 1}

    def test_dedupes_within_event(self) -> None:
        """A code repeated within one event still counts once for it."""
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300;DE300"]})
        assert event_region_counts(events, _REGIONS_COLUMN) == {"DE300": 1}

    def test_missing_column_is_empty(self) -> None:
        """A frame without the regions column yields no counts."""
        assert event_region_counts(pd.DataFrame({"x": [1]}), _REGIONS_COLUMN) == {}


@pytest.mark.hanze
class TestEmptyRegionFc:
    """`empty_region_fc` carries the canonical schema at zero rows."""

    def test_schema_and_crs(self) -> None:
        """It has the region columns, a geometry column and CRS 4326."""
        fc = empty_region_fc()
        assert len(fc) == 0
        assert set(REGION_COLUMNS).issubset(fc.columns)
        assert "geometry" in fc.columns
        assert fc.crs.to_epsg() == 4326


@pytest.mark.hanze
class TestJoinEventsToRegions:
    """`join_events_to_regions` joins affected codes to region polygons."""

    def test_happy_path_counts_and_crs(self, region_zip, tmp_path) -> None:
        """Affected regions come back with event counts, reprojected to 4326."""
        regions = _read_regions(region_zip, tmp_path)
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300;DE711", "DE300"]})
        fc = join_events_to_regions(
            events,
            regions,
            regions_column=_REGIONS_COLUMN,
            join_field="Code",
            name_field="Name",
        )
        assert fc.crs.to_epsg() == 4326
        counts = dict(zip(fc["nuts3_code"], fc["n_events"]))
        assert counts == {"DE300": 2, "DE711": 1}
        assert set(fc.columns) == {"nuts3_code", "region_name", "n_events", "geometry"}

    def test_unmatched_code_dropped(self, region_zip, tmp_path) -> None:
        """A code absent from the boundary file contributes no feature."""
        regions = _read_regions(region_zip, tmp_path)
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300;ITX99"]})
        fc = join_events_to_regions(
            events,
            regions,
            regions_column=_REGIONS_COLUMN,
            join_field="Code",
            name_field="Name",
        )
        assert list(fc["nuts3_code"]) == ["DE300"]

    def test_no_matching_codes_is_empty(self, region_zip, tmp_path) -> None:
        """Events referencing no known region yield the empty schema FC."""
        regions = _read_regions(region_zip, tmp_path)
        events = pd.DataFrame({_REGIONS_COLUMN: ["ZZ999"]})
        fc = join_events_to_regions(
            events,
            regions,
            regions_column=_REGIONS_COLUMN,
            join_field="Code",
            name_field="Name",
        )
        assert len(fc) == 0
        assert fc.crs.to_epsg() == 4326

    def test_missing_join_field_is_empty(self, region_zip, tmp_path) -> None:
        """A join field absent from the boundary file yields the empty FC."""
        regions = _read_regions(region_zip, tmp_path)
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300"]})
        fc = join_events_to_regions(
            events,
            regions,
            regions_column=_REGIONS_COLUMN,
            join_field="Missing",
            name_field="Name",
        )
        assert len(fc) == 0

    def test_missing_name_field_becomes_na(self, region_zip, tmp_path) -> None:
        """A name field absent from the boundary file yields null names."""
        regions = _read_regions(region_zip, tmp_path)
        events = pd.DataFrame({_REGIONS_COLUMN: ["DE300"]})
        fc = join_events_to_regions(
            events,
            regions,
            regions_column=_REGIONS_COLUMN,
            join_field="Code",
            name_field="Nope",
        )
        assert fc["region_name"].isna().all()
