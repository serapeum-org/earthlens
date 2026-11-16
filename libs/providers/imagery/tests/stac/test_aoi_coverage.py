"""Tests for screening STAC candidates by footprint before applying max_items.

A STAC search can only filter on the item bbox. For a rotated footprint the bbox
is the envelope of a tilted quadrilateral, so an item can match the bbox while
covering none of the AOI; these cover the geometry screen that drops those.
"""

from __future__ import annotations

import pytest
from shapely.geometry import box, shape

from earthlens.stac import STAC
from earthlens.stac.backend import _MIN_OVERLAP_FRACTION, _covers_aoi

from .conftest import make_bbox_geometry, make_item

# The AOI every `_build_stac` test uses: lon -4..-3, lat 40..41 (area 1.0 deg^2).
_AOI = box(-4.0, 40.0, -3.0, 41.0)
_COVERING = make_bbox_geometry(-4.5, 39.5, -2.5, 41.5)
_MISSING = make_bbox_geometry(10.0, 10.0, 11.0, 11.0)

# The case the API cannot filter out and this screen exists for: a square rotated
# 45 degrees (what every Landsat/Sentinel scene looks like), placed so its bbox
# covers 81% of the AOI while the footprint itself covers none of it. A STAC
# search matches it on the bbox; only the geometry shows it is useless.
_ROTATED_BBOX_HIT_GEOMETRY_MISS = {
    "type": "Polygon",
    "coordinates": [
        [(-3.9, 42.0), (-2.0, 43.9), (-0.1, 42.0), (-2.0, 40.1), (-3.9, 42.0)]
    ],
}


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
        continental = box(-180.0, -60.0, 180.0, 60.0)
        # a real scene (500 deg^2) that merely grazes the AOI's eastern edge, so
        # the overlap is negligible relative to the scene as well as to the AOI
        grazing = make_bbox_geometry(179.999995, -50.0, 185.0, 50.0)
        item = make_item("a", "2024-01-05", {}, geometry=grazing)
        assert not _covers_aoi(item, continental)

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

    def test_degenerate_zero_area_aoi_keeps_everything(self):
        """A zero-area AOI cannot be covered by any share, so nothing is dropped."""
        point_aoi = box(-4.0, 40.0, -4.0, 40.0)
        assert point_aoi.area == 0
        assert _covers_aoi(
            make_item("a", "2024-01-05", {}, geometry=_MISSING), point_aoi
        )

    def test_empty_footprint_is_kept(self):
        """A syntactically valid but empty footprint is kept, not dropped."""
        empty = {"type": "Polygon", "coordinates": []}
        assert shape(empty).is_empty
        assert _covers_aoi(make_item("a", "2024-01-05", {}, geometry=empty), _AOI)

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

    def test_overlap_exactly_at_the_floor_is_kept(self):
        """The floor is inclusive: an overlap equal to it still counts."""
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

    def test_max_items_is_not_delegated_to_the_api(self, fake_pyramids, tmp_path):
        """The cap is applied after screening, so the API is not asked to truncate."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "hit", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_COVERING
            )
        ]
        _build_stac(tmp_path, max_items=1)._search()
        assert fake_pyramids.client.search_calls[0].get("max_items") is None

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

    def test_all_dropped_raises_naming_the_bbox_mismatch(self, fake_pyramids, tmp_path):
        """When every candidate misses the AOI the error says so, not 'no pixels'."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "miss", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            )
        ]
        with pytest.raises(ValueError, match="matched the search bbox but none covers"):
            _build_stac(tmp_path, max_items=1)._search()

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

    def test_the_error_names_how_many_candidates_were_dropped(
        self, fake_pyramids, tmp_path
    ):
        """The all-dropped error reports the number of candidates it examined."""
        fake_pyramids.items_by_collection["sentinel-2-c1-l2a"] = [
            make_item(
                "m1", "2024-01-05", {"B04": "https://h/1.tif"}, geometry=_MISSING
            ),
            make_item(
                "m2", "2024-01-06", {"B04": "https://h/2.tif"}, geometry=_MISSING
            ),
        ]
        with pytest.raises(ValueError, match=r"^2 item\(s\) matched"):
            _build_stac(tmp_path)._search()

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
