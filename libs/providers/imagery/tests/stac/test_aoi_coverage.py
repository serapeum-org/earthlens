"""Tests for screening STAC candidates by footprint before applying max_items.

A STAC search can only filter on the item bbox. For a rotated footprint the bbox
is the envelope of a tilted quadrilateral, so an item can match the bbox while
covering none of the AOI; these cover the geometry screen that drops those.
"""

from __future__ import annotations

import pytest
from shapely.geometry import box, shape

from earthlens.stac import STAC
from earthlens.stac.backend import (
    _DEFAULT_MAX_CANDIDATES,
    _MIN_OVERLAP_FRACTION,
    _covers_aoi,
    _normalise_longitudes,
    _overlaps,
)

from .conftest import make_bbox_geometry, make_item

# The AOI every `_build_stac` test uses: lon -4..-3, lat 40..41 (area 1.0 deg^2).
_AOI = box(-4.0, 40.0, -3.0, 41.0)
_COVERING = make_bbox_geometry(-4.5, 39.5, -2.5, 41.5)
_MISSING = make_bbox_geometry(10.0, 10.0, 11.0, 11.0)

# The case the API cannot filter out and this screen exists for: a footprint at an
# angle to the lon/lat grid, placed so its bbox covers 81% of the AOI while the
# footprint itself covers none of it. A STAC search matches it on the bbox; only
# the geometry shows it is useless. A square rotated a full 45 degrees is the
# extreme of that shape (half its bbox); a live `landsat-c2l2-sr` footprint
# measures 0.68 of its bbox, and Sentinel-2's MGRS tiles are near axis-aligned in
# lon/lat, so this is a deliberately exaggerated fixture, not a typical scene.
_ROTATED_BBOX_HIT_GEOMETRY_MISS = {
    "type": "Polygon",
    "coordinates": [
        [(-3.9, 42.0), (-2.0, 43.9), (-0.1, 42.0), (-2.0, 40.1), (-3.9, 42.0)]
    ],
}


def _raise_api_error(**kwargs):
    """Stand in for a client whose search fails part-way through."""
    raise RuntimeError("API down")


def _build_stac(tmp_path, **kwargs):
    """Construct a STAC backend over the standard test AOI."""
    return STAC(
        start="2024-01-01",
        end="2024-01-31",
        variables=kwargs.pop("variables", None) or {"sentinel-2-l2a": ["B04"]},
        lat_lim=kwargs.pop("lat_lim", None) or [40.0, 41.0],
        lon_lim=kwargs.pop("lon_lim", None) or [-4.0, -3.0],
        path=str(tmp_path),
        endpoint="earth-search",
        **kwargs,
    )


@pytest.mark.stac
class TestCoversAoi:
    """`_covers_aoi` judges a footprint against the AOI, not against its bbox."""

    def test_overlapping_footprint_is_kept(self):
        """A footprint spanning the AOI counts as covering it."""
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=_COVERING), _AOI)

    def test_disjoint_footprint_is_dropped(self):
        """A footprint far from the AOI does not count, bbox match notwithstanding."""
        assert not _covers_aoi(
            make_item("a", "2024-01-05", {}, geometry=_MISSING), _AOI
        )

    def test_hairline_overlap_is_dropped(self):
        """An overlap below the coverage floor is treated as no coverage."""
        sliver = make_bbox_geometry(-3.00001, 40.0, -2.0, 41.0)
        assert not _covers_aoi(make_item("a", "2024-01-05", {}, geometry=sliver), _AOI)

    def test_overlap_just_above_the_floor_is_kept(self):
        """An overlap above the coverage floor still counts."""
        # 1% of the AOI's width over its full height -> 1% of its area.
        wide = make_bbox_geometry(-3.01, 40.0, -2.0, 41.0)
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=wide), _AOI)

    def test_item_without_geometry_is_kept(self):
        """An item publishing no footprint is kept rather than silently dropped."""
        assert _covers_aoi(make_item("a", "2024-01-05", {}), _AOI)

    @pytest.mark.parametrize(
        "geometry",
        [
            {"type": "Nope"},  # unknown type -> shapely GeometryTypeError
            {"type": "Polygon"},  # no coordinates -> KeyError
            {"coordinates": []},  # no type -> AttributeError
            "junk",  # not a mapping at all
        ],
    )
    def test_unparseable_geometry_is_kept(self, geometry):
        """A malformed footprint is kept rather than dropped on a parse quirk."""
        bad = make_item("a", "2024-01-05", {}, geometry=geometry)
        assert _covers_aoi(bad, _AOI)

    def test_rotated_footprint_matching_only_on_bbox_is_dropped(self):
        """A rotated scene whose bbox overlaps the AOI but whose geometry misses it."""
        item = make_item(
            "a", "2024-01-05", {}, geometry=_ROTATED_BBOX_HIT_GEOMETRY_MISS
        )
        footprint = shape(_ROTATED_BBOX_HIT_GEOMETRY_MISS)
        # the premise: the API would match this item, since its bbox covers 81%
        # of the AOI -- while the footprint itself covers none of it
        assert box(*footprint.bounds).intersection(_AOI).area / _AOI.area > 0.8
        assert footprint.intersection(_AOI).area == 0
        assert not _covers_aoi(item, _AOI)

    def test_a_tile_is_judged_on_its_own_overlap_not_its_share_of_the_aoi(self):
        """A whole tile inside a continental AOI is kept however small its share."""
        continental = box(-180.0, -60.0, 180.0, 60.0)
        tile = make_bbox_geometry(-0.5, -0.5, 0.5, 0.5)
        item = make_item("a", "2024-01-05", {}, geometry=tile)
        # the tile is 1 deg^2 of an 86400 deg^2 AOI -- 1.2e-05 of it, far under
        # the floor, yet it covers itself completely and carries real pixels
        assert _covers_aoi(item, continental)

    def test_a_hairline_is_still_dropped_against_a_large_aoi(self):
        """Scaling to the smaller geometry must not stop edge slivers being dropped."""
        # a hemispheric AOI, so the grazing scene stays inside [-180, 180]
        hemisphere = box(-180.0, -60.0, 0.0, 60.0)
        # a real scene (5000 deg^2) that merely grazes the AOI's eastern edge, so
        # the overlap is negligible relative to the scene as well as to the AOI
        grazing = make_bbox_geometry(-0.000005, -50.0, 50.0, 50.0)
        item = make_item("a", "2024-01-05", {}, geometry=grazing)
        assert not _covers_aoi(item, hemisphere)

    def test_a_footprint_smaller_than_the_floor_is_kept_when_wholly_inside(self):
        """A genuinely tiny scene is kept: its whole extent is all the data there is."""
        continental = box(-180.0, -60.0, 180.0, 60.0)
        tiny = make_bbox_geometry(0.0, 0.0, 0.001, 0.001)
        item = make_item("a", "2024-01-05", {}, geometry=tiny)
        assert _covers_aoi(item, continental)

    def test_a_self_intersecting_footprint_overlapping_the_aoi_is_kept(self):
        """An invalid ring is repaired and judged, not allowed to raise."""
        bowtie = {
            "type": "Polygon",
            "coordinates": [
                [(-4.5, 39.5), (-2.5, 41.5), (-4.5, 41.5), (-2.5, 39.5), (-4.5, 39.5)]
            ],
        }
        assert not shape(bowtie).is_valid
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=bowtie), _AOI)

    def test_an_invalid_footprint_grazing_the_aoi_is_dropped_after_repair(self):
        """The repair is what judges this one; without it the item would be kept.

        An invalid ring's area cancels to 0, so unrepaired it reaches the
        zero-area rule, `intersects` is True and the scene is kept. Repaired it
        has a real area of 1.0 and an overlap of 5e-11, far under the floor.
        """
        grazing_bowtie = {
            "type": "Polygon",
            "coordinates": [
                [
                    (-3.00001, 40.0),
                    (-1.0, 41.0),
                    (-3.00001, 41.0),
                    (-1.0, 40.0),
                    (-3.00001, 40.0),
                ]
            ],
        }
        raw = shape(grazing_bowtie)
        assert not raw.is_valid
        assert raw.area == 0
        assert raw.intersects(_AOI)
        item = make_item("a", "2024-01-05", {}, geometry=grazing_bowtie)
        assert not _covers_aoi(item, _AOI)

    def test_a_self_intersecting_footprint_away_from_the_aoi_is_dropped(self):
        """Repairing an invalid ring still lets a non-overlapping one be dropped."""
        far_bowtie = {
            "type": "Polygon",
            "coordinates": [
                [(10.0, 10.0), (12.0, 12.0), (10.0, 12.0), (12.0, 10.0), (10.0, 10.0)]
            ],
        }
        assert not shape(far_bowtie).is_valid
        assert not _covers_aoi(
            make_item("a", "2024-01-05", {}, geometry=far_bowtie), _AOI
        )

    def test_an_out_of_range_footprint_is_shifted_into_range_and_kept(self):
        """A 0-360-convention footprint over the AOI is normalised, then kept."""
        # 180.5..181.5 is -179.5..-178.5, which is inside this antimeridian half
        half = box(-180.0, -1.0, -170.0, 1.0)
        item = make_item(
            "a", "2024-01-05", {}, geometry=make_bbox_geometry(180.5, -0.5, 181.5, 0.5)
        )
        assert _covers_aoi(item, half)

    def test_an_out_of_range_footprint_away_from_the_aoi_is_dropped(self):
        """Normalising is not an exemption: a Pacific scene loses a European AOI.

        Exempting out-of-range footprints from the screen instead would keep this
        one, let it consume the item cap and have it fetched for nothing.
        """
        pacific = make_bbox_geometry(200.0, 39.0, 210.0, 42.0)
        item = make_item("a", "2024-01-05", {}, geometry=pacific)
        assert not _covers_aoi(item, _AOI)

    def test_a_shifted_footprint_that_still_misses_is_dropped(self):
        """A shift that lands the footprint elsewhere does not keep it."""
        # -181.5..-180.5 shifts to 178.5..179.5, which misses this half entirely
        half = box(-180.0, -1.0, -170.0, 1.0)
        item = make_item(
            "a",
            "2024-01-05",
            {},
            geometry=make_bbox_geometry(-181.5, -0.5, -180.5, 0.5),
        )
        assert not _covers_aoi(item, half)

    def test_a_footprint_straddling_180_is_judged_where_it_lies(self):
        """A straddling footprint is matched by the half its in-range part covers."""
        straddling = make_bbox_geometry(178.5, -0.5, 181.5, 0.5)
        item = make_item("a", "2024-01-05", {}, geometry=straddling)
        # its in-range part (178.5..180) covers the eastern half
        assert _covers_aoi(item, box(170.0, -1.0, 180.0, 1.0))
        # the western half is served by that same item through the eastern bbox,
        # so no shift is attempted for a footprint that is partly in range
        assert not _covers_aoi(item, box(-180.0, -1.0, -170.0, 1.0))

    @pytest.mark.parametrize(
        ("name", "geometry"),
        [
            ("far point", {"type": "Point", "coordinates": (10.0, 10.0)}),
            (
                "far line",
                {"type": "LineString", "coordinates": [(10.0, 10.0), (12.0, 12.0)]},
            ),
            ("far polygon", _MISSING),
            ("no geometry", None),
        ],
    )
    def test_a_zero_floor_keeps_everything(self, name, geometry):
        """min_overlap_fraction=0 is an off-switch for every footprint shape."""
        item = make_item("a", "2024-01-05", {}, geometry=geometry)
        assert _covers_aoi(item, _AOI, 0.0)

    def test_coverage_floor_is_a_small_fraction(self):
        """The default floor excludes slivers without excluding partial tiles."""
        assert 0 < _MIN_OVERLAP_FRACTION < 0.01

    def test_geometry_is_read_from_a_mapping_item(self):
        """An item carrying its geometry as a mapping key is judged the same way."""
        assert _covers_aoi({"geometry": _COVERING}, _AOI)
        assert not _covers_aoi({"geometry": _MISSING}, _AOI)

    def test_mapping_item_without_geometry_is_kept(self):
        """A mapping item with no geometry key is kept, like an object without one."""
        assert _covers_aoi({"id": "a"}, _AOI)

    @pytest.mark.parametrize(
        ("name", "geometry", "expected"),
        [
            ("point inside", {"type": "Point", "coordinates": (-3.5, 40.5)}, True),
            ("point outside", {"type": "Point", "coordinates": (10.0, 10.0)}, False),
            (
                "line across",
                {"type": "LineString", "coordinates": [(-4.5, 40.5), (-2.5, 40.5)]},
                True,
            ),
            (
                "line outside",
                {"type": "LineString", "coordinates": [(10.0, 10.0), (12.0, 12.0)]},
                False,
            ),
        ],
    )
    def test_zero_area_footprints_fall_back_to_intersection(
        self, name, geometry, expected
    ):
        """A Point or LineString footprint is judged topologically, not by area."""
        assert shape(geometry).area == 0
        item = make_item("a", "2024-01-05", {}, geometry=geometry)
        assert _covers_aoi(item, _AOI) is expected

    def test_degenerate_zero_area_aoi_is_judged_topologically(self):
        """A zero-area AOI keeps a footprint containing it and drops one that misses."""
        point_aoi = box(-4.0, 40.0, -4.0, 40.0)
        assert point_aoi.area == 0
        assert _covers_aoi(
            make_item("a", "2024-01-05", {}, geometry=_COVERING), point_aoi
        )
        assert not _covers_aoi(
            make_item("b", "2024-01-05", {}, geometry=_MISSING), point_aoi
        )

    def test_empty_footprint_is_kept(self):
        """A syntactically valid but empty footprint is kept, not dropped."""
        empty = {"type": "Polygon", "coordinates": []}
        assert shape(empty).is_empty
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=empty), _AOI)

    def test_an_empty_footprint_survives_normalisation_and_is_kept(self):
        """An empty geometry's nan bounds shift nowhere, and _overlaps keeps it."""
        empty = shape({"type": "Polygon", "coordinates": []})
        assert _normalise_longitudes(empty) is empty
        assert _overlaps(empty, _AOI, _MIN_OVERLAP_FRACTION)

    def test_multipolygon_footprint_is_measured_as_a_whole(self):
        """A split footprint counts the total area it shares with the AOI."""
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                make_bbox_geometry(-3.9, 40.1, -3.8, 40.9)["coordinates"][0],
                make_bbox_geometry(10.0, 10.0, 11.0, 11.0)["coordinates"][0],
            ],
        }
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=multi), _AOI)

    @pytest.mark.parametrize(
        ("min_fraction", "expected"),
        [(0.05, True), (0.2, False)],
    )
    def test_min_fraction_is_honoured(self, min_fraction, expected):
        """An explicit floor decides the same 10% overlap either way."""
        # a tenth of the AOI's width over its full height -> 10% of its area
        tile = make_bbox_geometry(-3.1, 40.0, -2.0, 41.0)
        item = make_item("a", "2024-01-05", {}, geometry=tile)
        assert _covers_aoi(item, _AOI, min_fraction=min_fraction) is expected

    def test_an_overlap_exactly_at_the_floor_is_kept(self):
        """The comparison is inclusive at the floor itself, not just above it."""
        # the AOI is 1 deg^2 and is the smaller geometry here, so an overlap of
        # exactly _MIN_OVERLAP_FRACTION deg^2 sits precisely on the floor
        exact = make_bbox_geometry(-3.0 - _MIN_OVERLAP_FRACTION, 40.0, -2.0, 41.0)
        footprint = shape(exact)
        assert footprint.intersection(_AOI).area == pytest.approx(
            _MIN_OVERLAP_FRACTION * min(_AOI.area, footprint.area), rel=1e-9
        )
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=exact), _AOI)

    def test_full_containment_satisfies_any_demand(self):
        """A footprint containing the AOI passes even a 100% demand."""
        item = make_item("a", "2024-01-05", {}, geometry=_COVERING)
        assert _covers_aoi(item, _AOI, min_fraction=1.0)


@pytest.mark.stac
class TestSearchScreensByFootprint:
    """`_search` drops non-covering candidates before `max_items` is applied."""

    def test_non_covering_items_are_dropped(self, fake_pyramids, tmp_path):
        """Only the candidate whose footprint covers the AOI becomes a product."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            ),
            make_item(
                "hit", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            ),
        ]
        products = _build_stac(tmp_path)._search()
        assert [p.id for p in products] == ["hit"]

    def test_max_items_counts_covering_items(self, fake_pyramids, tmp_path):
        """max_items=1 selects the first covering item, not the first returned."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            ),
            make_item(
                "hit", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            ),
        ]
        products = _build_stac(tmp_path, max_items=1)._search()
        assert [p.id for p in products] == ["hit"]

    def test_the_item_cap_never_reaches_the_api(self, fake_pyramids, tmp_path):
        """The cap stays client-side; only the candidate budget bounds the search."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "hit", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            )
        ]
        _build_stac(tmp_path, max_items=1)._search()
        assert fake_pyramids.client.search_calls[0].get("max_items") != 1

    def test_the_floor_is_reachable_from_the_constructor(self, fake_pyramids, tmp_path):
        """min_overlap_fraction=0 turns the screen off for a caller who needs that."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        kept = _build_stac(tmp_path, min_overlap_fraction=0.0)._search()
        assert [p.id for p in kept] == ["miss"]
        assert _build_stac(tmp_path)._search() == []

    def test_a_tighter_floor_from_the_constructor_is_applied(
        self, fake_pyramids, tmp_path
    ):
        """A caller can demand more coverage than the default floor."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "partial",
                "2024-01-05",
                {"B04": "https://h/1.tif"},
                geometry=make_bbox_geometry(-3.1, 40.0, -2.0, 41.0),
            )
        ]
        # the tile overlaps a tenth of the AOI, so a 50% demand rejects it
        assert _build_stac(tmp_path, min_overlap_fraction=0.5)._search() == []
        assert len(_build_stac(tmp_path, min_overlap_fraction=0.05)._search()) == 1

    @pytest.mark.parametrize("fraction", [-0.1, 1.5])
    def test_a_floor_outside_the_unit_range_is_rejected(
        self, fake_pyramids, tmp_path, fraction
    ):
        """A fraction must be a fraction."""
        with pytest.raises(ValueError, match="min_overlap_fraction"):
            _build_stac(tmp_path, min_overlap_fraction=fraction)

    def test_a_covering_item_beyond_the_pool_is_still_found(
        self, fake_pyramids, tmp_path
    ):
        """A cap must not shrink the pool to where a covering scene is unreachable.

        This is #1414's own failure mode: letting the request's item cap decide how
        many candidates are examined truncates the list before the screen runs.
        """
        misses = [
            make_item(
                f"m{n}", "2024-01-05", {"B04": "https://h/x.tif"}, geometry=_MISSING
            )
            for n in range(150)
        ]
        hit = make_item(
            "hit", "2024-01-06", {"B04": "https://h/h.tif"}, geometry=_COVERING
        )
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [*misses, hit]
        products = _build_stac(tmp_path, max_items=1)._search()
        assert [p.id for p in products] == ["hit"]

    def test_pool_exhaustion_is_reported_as_itself_not_as_no_coverage(
        self, fake_pyramids, tmp_path, warnings_buffer
    ):
        """Hitting the candidate budget is a different outcome from nothing covering."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"m{n}", "2024-01-05", {"B04": "https://h/x.tif"}, geometry=_MISSING
            )
            for n in range(12)
        ]
        stac = _build_stac(tmp_path, max_items=1, max_candidates=10)
        assert stac._search() == []
        warned = "".join(warnings_buffer)
        assert "max_candidates" in warned
        with pytest.raises(ValueError, match="max_candidates"):
            stac.download()

    def test_the_candidate_budget_does_not_follow_the_item_cap(
        self, fake_pyramids, tmp_path
    ):
        """The budget is its own knob, so a small cap does not shrink it."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = []
        _build_stac(tmp_path, max_items=1)._search()
        _build_stac(tmp_path, max_items=50)._search()
        requested = [c.get("max_items") for c in fake_pyramids.client.search_calls]
        assert requested == [_DEFAULT_MAX_CANDIDATES + 1] * 2

    def test_the_budget_actually_bounds_what_is_consumed(self, fake_pyramids, tmp_path):
        """The bound is observed by the search, not merely requested."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"m{n}", "2024-01-05", {"B04": "https://h/x.tif"}, geometry=_MISSING
            )
            for n in range(40)
        ]
        _build_stac(tmp_path, max_items=1, max_candidates=10)._search()
        assert fake_pyramids.client.searches[0].yielded == 11

    def test_an_unbounded_budget_asks_for_everything(self, fake_pyramids, tmp_path):
        """max_candidates=None leaves the search unbounded."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = []
        _build_stac(tmp_path, max_items=1, max_candidates=None)._search()
        assert fake_pyramids.client.search_calls[0].get("max_items") is None

    def test_a_collection_ending_exactly_at_the_budget_does_not_warn(
        self, fake_pyramids, tmp_path, warnings_buffer
    ):
        """Genuine exhaustion is not reported as truncation."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"m{n}", "2024-01-05", {"B04": "https://h/x.tif"}, geometry=_MISSING
            )
            for n in range(10)
        ]
        _build_stac(tmp_path, max_items=5, max_candidates=10)._search()
        assert "budget" not in "".join(warnings_buffer)

    @pytest.mark.parametrize("cap", [0, -1])
    def test_a_non_positive_cap_is_rejected(self, fake_pyramids, tmp_path, cap):
        """max_items must be a usable count, not 0 or negative."""
        with pytest.raises(ValueError, match="max_items"):
            _build_stac(tmp_path, max_items=cap)

    @pytest.mark.parametrize("budget", [0, -1, True, 2.5])
    def test_a_bad_candidate_budget_is_rejected(self, fake_pyramids, tmp_path, budget):
        """The budget must be a positive int or None."""
        with pytest.raises(ValueError, match="max_candidates"):
            _build_stac(tmp_path, max_candidates=budget)

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"max_items": True}, "max_items"),
            ({"max_items": 2.5}, "max_items"),
            ({"min_overlap_fraction": True}, "min_overlap_fraction"),
            ({"min_overlap_fraction": "half"}, "min_overlap_fraction"),
        ],
    )
    def test_non_numeric_knobs_are_rejected(
        self, fake_pyramids, tmp_path, kwargs, match
    ):
        """A bool or a float is not silently coerced into a count."""
        with pytest.raises(ValueError, match=match):
            _build_stac(tmp_path, **kwargs)

    def test_cap_stops_once_enough_covering_items_are_found(
        self, fake_pyramids, tmp_path
    ):
        """Screening stops at the cap rather than walking the whole result set."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"hit{n}", "2024-01-06", {"B04": "https://h/x.tif"}, geometry=_COVERING
            )
            for n in range(5)
        ]
        products = _build_stac(tmp_path, max_items=2)._search()
        assert [p.id for p in products] == ["hit0", "hit1"]

    def test_all_dropped_returns_empty_rather_than_raising(
        self, fake_pyramids, tmp_path
    ):
        """_search keeps its empty-result contract, so count() can still answer 0."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        stac = _build_stac(tmp_path, max_items=1)
        assert stac._search() == []
        assert stac._count() == 0

    def test_download_raises_when_every_candidate_missed_the_aoi(
        self, fake_pyramids, tmp_path
    ):
        """A download that can write nothing because of the screen fails loudly."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        stac = _build_stac(tmp_path, max_items=1)
        with pytest.raises(ValueError, match="none covers the requested AOI"):
            stac.download()

    def test_download_returns_empty_when_the_api_matched_nothing(
        self, fake_pyramids, tmp_path
    ):
        """A genuinely empty search still returns an empty list from download."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = []
        assert _build_stac(tmp_path).download() == []

    def test_rotated_bbox_only_match_is_dropped_by_the_search(
        self, fake_pyramids, tmp_path
    ):
        """A bbox-only match loses to a later covering scene under max_items=1."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "rotated",
                "2024-01-05",
                {"B04": "https://h/1.tif"},
                geometry=_ROTATED_BBOX_HIT_GEOMETRY_MISS,
            ),
            make_item(
                "covering", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            ),
        ]
        products = _build_stac(tmp_path, max_items=1)._search()
        assert [p.id for p in products] == ["covering"]

    def test_a_continental_aoi_keeps_its_tiles(self, fake_pyramids, tmp_path):
        """A tiled mosaic over a continental AOI survives the screen."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"t{n}",
                "2024-01-05",
                {"B04": f"https://h/{n}.tif"},
                geometry=make_bbox_geometry(n, 0.0, n + 1.0, 1.0),
            )
            for n in range(5)
        ]
        products = _build_stac(
            tmp_path, lat_lim=[-60.0, 60.0], lon_lim=[-180.0, 180.0]
        )._search()
        assert [p.id for p in products] == ["t0", "t1", "t2", "t3", "t4"]

    def test_the_fetch_path_runs_with_a_real_footprint(self, fake_pyramids, tmp_path):
        """A full search-and-fetch works on an item carrying a genuine footprint.

        The older fetch-path tests all build geometry-less items, so without this
        the whole write path is only ever exercised through the keep-on-missing
        branch rather than through a footprint the screen actually measures.
        """
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "with-footprint",
                "2024-01-05",
                {"B04": "https://h/1.tif"},
                geometry=_COVERING,
            )
        ]
        stac = _build_stac(tmp_path)
        written = stac._fetch(stac._search())
        assert len(written) == 1
        assert fake_pyramids.merge_calls[0][0] == ["https://h/1.tif"]

    def test_an_out_of_range_footprint_does_not_starve_the_covering_scene(
        self, fake_pyramids, tmp_path
    ):
        """A Pacific scene must not consume max_items=1 over a European AOI."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "pacific",
                "2024-01-05",
                {"B04": "https://h/p.tif"},
                geometry=make_bbox_geometry(200.0, 39.0, 210.0, 42.0),
            ),
            make_item(
                "covering", "2024-01-06", {"B04": "https://h/c.tif"}, geometry=_COVERING
            ),
        ]
        products = _build_stac(tmp_path, max_items=1)._search()
        assert [p.id for p in products] == ["covering"]

    def test_budget_exhaustion_is_not_recorded_as_nothing_covering(
        self, fake_pyramids, tmp_path
    ):
        """A truncated search never established that nothing covers the AOI."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                f"m{n}", "2024-01-05", {"B04": "https://h/x.tif"}, geometry=_MISSING
            )
            for n in range(12)
        ]
        stac = _build_stac(tmp_path, max_items=1, max_candidates=10)
        stac._search()
        assert stac._budget_exhausted
        assert stac._screened_out is False

    def test_a_failed_search_leaves_no_stale_verdict(self, fake_pyramids, tmp_path):
        """A search that raises part-way must not keep the previous call's answer."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        stac = _build_stac(tmp_path)
        stac._search()
        assert stac._screened_out is True
        fake_pyramids.client.search = _raise_api_error
        with pytest.raises(RuntimeError, match="API down"):
            stac._search()
        assert stac._screened_out is False
        assert stac._budget_exhausted == []

    def test_an_empty_api_result_does_not_raise(self, fake_pyramids, tmp_path):
        """A search the API matched nothing for returns no products without raising."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = []
        assert _build_stac(tmp_path)._search() == []

    def test_without_a_cap_every_covering_item_is_kept(self, fake_pyramids, tmp_path):
        """max_items=None keeps all covering items and still drops the misses."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            ),
            make_item(
                "hit1", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            ),
            make_item(
                "hit2", "2024-01-07", {"B04": "https://h/3.tif"}, geometry=_COVERING
            ),
        ]
        products = _build_stac(tmp_path)._search()
        assert [p.id for p in products] == ["hit1", "hit2"]

    def test_items_without_geometry_survive_the_search(self, fake_pyramids, tmp_path):
        """An item publishing no footprint still becomes a product."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("a", "2024-01-05", {"B04": "https://h/1.tif"})
        ]
        assert [p.id for p in _build_stac(tmp_path)._search()] == ["a"]

    def test_one_covered_collection_does_not_raise_for_another(
        self, fake_pyramids, tmp_path
    ):
        """An all-dropped collection is tolerated while another yields coverage."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "hit", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            )
        ]
        fake_pyramids.items_by_collection["landsat-c2-l2"] = [
            make_item(
                "miss", "2024-01-05", {"red": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        products = _build_stac(
            tmp_path,
            variables={
                "sentinel-2-l2a": ["B04"],
                "earth-search/landsat-c2-l2": ["red"],
            },
        )._search()
        assert [p.id for p in products] == ["hit"]

    def test_an_all_dropped_pair_warns_naming_the_counts_and_the_collection(
        self, fake_pyramids, tmp_path, warnings_buffer
    ):
        """The all-dropped case is reported loudly rather than returning none quietly."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "m1", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            ),
            make_item(
                "m2", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_MISSING
            ),
        ]
        _build_stac(tmp_path)._search()
        warned = "".join(warnings_buffer)
        assert "2 candidate" in warned
        assert "sentinel-2-l2a" in warned

    def test_a_dropped_antimeridian_half_is_reported_on_its_own(
        self, fake_pyramids, tmp_path, warnings_buffer
    ):
        """A half that drops everything warns even though the other half succeeded."""
        east = make_bbox_geometry(170.5, -1.0, 179.5, 1.0)
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("east", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=east),
            make_item(
                "miss", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_MISSING
            ),
        ]
        products = _build_stac(
            tmp_path, lat_lim=[-1.0, 1.0], lon_lim=[170.0, -170.0]
        )._search()
        assert [p.id for p in products] == ["east"]
        # the western half matched one candidate and kept none -- the partial case
        # the old global check stayed silent about
        assert "(-180.0, -1.0, -170.0, 1.0)" in "".join(warnings_buffer)

    def test_each_split_bbox_is_screened_against_its_own_half(
        self, fake_pyramids, tmp_path
    ):
        """An antimeridian AOI screens each half against that half, not the whole."""
        # lon 170..-170 splits into (170, 180) and (-180, -170)
        east = make_bbox_geometry(170.5, -1.0, 179.5, 1.0)
        west = make_bbox_geometry(-179.5, -1.0, -170.5, 1.0)
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item("east", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=east),
            make_item("west", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=west),
        ]
        products = _build_stac(
            tmp_path, lat_lim=[-1.0, 1.0], lon_lim=[170.0, -170.0]
        )._search()
        assert len(fake_pyramids.client.search_calls) == 2
        # each half keeps only the item that falls in it, so both survive overall
        by_bbox = {p.metadata["bbox"][0]: p.id for p in products}
        assert by_bbox == {170.0: "east", -180.0: "west"}
