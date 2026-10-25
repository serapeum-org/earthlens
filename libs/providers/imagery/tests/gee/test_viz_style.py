"""Tests for the GEE catalog to digitalearth style translator (Task H3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.catalog import Band, Catalog, Dataset, Extent, Visualization
from earthlens.gee.viz._style import (
    DEFAULT_CMAP,
    StyleHints,
    cmap_for,
    style_for,
)


def _row(**kwargs) -> Dataset:
    """Build a minimal catalog row for a style test."""
    base = {
        "id": "T/EST",
        "title": "Test",
        "extent": Extent(start_date="2000-01-01", bbox=[-180, -90, 180, 90]),
        "bands": {"b1": Band(id="b1", min=0, max=100)},
    }
    return Dataset(**{**base, **kwargs})


def _catalog(row: Dataset) -> Catalog:
    """Wrap one row in a catalog, bypassing the YAML loader."""
    cat = Catalog()
    object.__setattr__(cat, "datasets", {row.id: row})
    return cat


class TestCmapFor:
    """The heuristic colour-map table."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("NDVI", "YlGn"),
            ("ndwi", "Blues"),
            ("landcover", "tab20"),
            ("t2m", "coolwarm"),
            ("precipitation", "Blues"),
            ("elevation", "terrain"),
            ("VV", "gray"),
            ("cloud_fraction", "bone"),
        ],
    )
    def test_known_concepts_take_their_ramp(self, text, expected):
        """Each concept in the table resolves to its own colour map."""
        assert cmap_for(text) == expected

    def test_an_unmatched_band_falls_back_to_the_digitalearth_default(self):
        """Nothing matched gives viridis, which is digitalearth's own default."""
        assert cmap_for("xyzzy") == DEFAULT_CMAP

    def test_the_description_is_consulted_when_the_id_says_nothing(self):
        """A meaningless band id still styles from its description."""
        assert cmap_for("b1", "Total precipitation") == "Blues"

    def test_matching_is_case_insensitive(self):
        """Upper and lower spellings of a concept agree."""
        assert cmap_for("ELEVATION") == cmap_for("elevation")


class TestStyleHints:
    """The value object and its digitalearth keyword translation."""

    def test_rgb_kwargs_pass_per_band_limits_not_band_names(self):
        """`rgb_composite` reads band order, so names are not passed as `bands`."""
        hints = StyleHints(
            bands=("a", "b", "c"),
            limits=((0.0, 1.0), (0.0, 1.0), (0.0, 1.0)),
            source="catalog",
        )
        kwargs = hints.rgb_kwargs()
        assert hints.is_rgb
        assert "bands" not in kwargs
        assert kwargs["limits"] == [(0.0, 1.0), (0.0, 1.0), (0.0, 1.0)]

    def test_field_kwargs_are_vmin_vmax_not_limits(self):
        """The single-band draw takes a scalar stretch, which it calls vmin/vmax."""
        hints = StyleHints(bands=("b1",), limits=((0.0, 9.0),), source="heuristic")
        assert hints.field_kwargs() == {"vmin": 0.0, "vmax": 9.0}

    def test_absent_hints_are_omitted_not_passed_as_none(self):
        """digitalearth's own defaults survive a hint earthlens cannot supply."""
        assert StyleHints(bands=("b1",), source="heuristic").field_kwargs() == {}
        assert StyleHints(bands=("b1",), source="heuristic").rgb_kwargs() == {}

    def test_gamma_is_a_scalar_for_a_field_and_a_list_for_a_composite(self):
        """Each draw takes gamma in its own shape."""
        single = StyleHints(bands=("b1",), gamma=(1.5,), source="catalog")
        assert single.field_kwargs()["gamma"] == 1.5
        rgb = StyleHints(
            bands=("a", "b", "c"), gamma=(1.5, 1.5, 1.5), source="catalog"
        )
        assert rgb.rgb_kwargs()["gamma"] == [1.5, 1.5, 1.5]

    @pytest.mark.parametrize("field", ["gamma", "gain"])
    def test_a_bool_is_refused_where_a_number_belongs(self, field):
        """`bool` is an `int` subclass, so it is refused before coercion."""
        with pytest.raises(ValidationError):
            StyleHints(bands=("b1",), source="catalog", **{field: (True,)})

    def test_an_unknown_field_is_refused(self):
        """A typo cannot read as an absent hint."""
        with pytest.raises(ValidationError):
            StyleHints(bands=("b1",), source="catalog", cmaps="viridis")


class TestStyleForCatalogPath:
    """A publisher rendering on the row wins over the heuristics."""

    def test_the_publisher_rendering_is_used_when_the_bands_match(self):
        """A matching `gee:visualizations` entry supplies bands and stretch."""
        row = _row(
            bands={
                "B4": Band(id="B4", min=0, max=10000),
                "B3": Band(id="B3", min=0, max=10000),
                "B2": Band(id="B2", min=0, max=10000),
            },
            visualizations=(
                Visualization(
                    name="RGB", bands=("B4", "B3", "B2"), vmin=(0.0,), vmax=(3000.0,)
                ),
            ),
        )
        hints = style_for("T/EST", ("B4", "B3", "B2"), catalog=_catalog(row))
        assert hints.source == "catalog"
        assert hints.limits == ((0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0))

    def test_the_band_metadata_is_not_used_when_the_publisher_disagrees(self):
        """The publisher's 0-3000 beats the band's declared 0-10000."""
        row = _row(
            bands={"b1": Band(id="b1", min=0, max=10000)},
            visualizations=(
                Visualization(name="Grey", bands=("b1",), vmin=(0.0,), vmax=(3000.0,)),
            ),
        )
        hints = style_for("T/EST", ("b1",), catalog=_catalog(row))
        assert hints.limits == ((0.0, 3000.0),)

    def test_no_bands_requested_takes_the_publishers_own_selection(self):
        """`bands=None` renders what Earth Engine's catalog page draws."""
        row = _row(
            bands={
                "B4": Band(id="B4"),
                "B3": Band(id="B3"),
                "B2": Band(id="B2"),
            },
            visualizations=(Visualization(name="RGB", bands=("B4", "B3", "B2")),),
        )
        hints = style_for("T/EST", None, catalog=_catalog(row))
        assert hints.bands == ("B4", "B3", "B2")
        assert hints.source == "catalog"

    def test_a_named_rendering_is_selectable(self):
        """A dataset offering several renderings can be asked for one."""
        row = _row(
            bands={"b1": Band(id="b1"), "b2": Band(id="b2")},
            visualizations=(
                Visualization(name="True Color", bands=("b1",)),
                Visualization(name="Infrared", bands=("b2",)),
            ),
        )
        hints = style_for("T/EST", None, name="Infrared", catalog=_catalog(row))
        assert hints.bands == ("b2",)

    def test_an_unknown_rendering_name_is_refused_with_the_choices(self):
        """A misnamed rendering names what the row does offer."""
        row = _row(visualizations=(Visualization(name="RGB", bands=("b1",)),))
        with pytest.raises(ValueError, match="is not a rendering of"):
            style_for("T/EST", None, name="Infra", catalog=_catalog(row))

    def test_a_rendering_for_other_bands_does_not_hijack_the_request(self):
        """Asking for one band does not get the publisher's three-band RGB."""
        row = _row(
            bands={"b1": Band(id="b1", min=0, max=100), "b2": Band(id="b2")},
            visualizations=(Visualization(name="RGB", bands=("b2",)),),
        )
        hints = style_for("T/EST", ("b1",), catalog=_catalog(row))
        assert hints.source == "heuristic"
        assert hints.bands == ("b1",)


class TestStyleForHeuristicPath:
    """Band metadata and the match table, when the row has no rendering."""

    def test_limits_come_from_the_bands_declared_range(self):
        """`Band.min`/`max` become the stretch."""
        hints = style_for("T/EST", ("b1",), catalog=_catalog(_row()))
        assert hints.limits == ((0.0, 100.0),)
        assert hints.source == "heuristic"

    def test_a_band_with_no_range_yields_no_limits_rather_than_half_a_stretch(self):
        """Half a stretch is not usable, so neither side is guessed."""
        row = _row(bands={"b1": Band(id="b1", min=0)})
        assert style_for("T/EST", ("b1",), catalog=_catalog(row)).limits == ()

    def test_a_partially_ranged_composite_yields_no_limits(self):
        """One band missing a range drops the whole composite's stretch."""
        row = _row(
            bands={
                "a": Band(id="a", min=0, max=1),
                "b": Band(id="b", min=0, max=1),
                "c": Band(id="c"),
            }
        )
        assert style_for("T/EST", ("a", "b", "c"), catalog=_catalog(row)).limits == ()

    def test_a_composite_gets_no_cmap(self):
        """A colour map is meaningless for a three-band composite."""
        row = _row(
            bands={
                "a": Band(id="a", min=0, max=1),
                "b": Band(id="b", min=0, max=1),
                "c": Band(id="c", min=0, max=1),
            }
        )
        assert style_for("T/EST", ("a", "b", "c"), catalog=_catalog(row)).cmap is None

    def test_a_single_band_gets_a_cmap_from_the_match_table(self):
        """The ramp is picked from the band's own id."""
        row = _row(bands={"elevation": Band(id="elevation", min=0, max=100)})
        hints = style_for("T/EST", ("elevation",), catalog=_catalog(row))
        assert hints.cmap == "terrain"

    def test_no_bands_requested_falls_back_to_the_rows_first_band(self):
        """A row with no rendering still has a sensible default band."""
        hints = style_for("T/EST", None, catalog=_catalog(_row()))
        assert hints.bands == ("b1",)


class TestStyleForRefusals:
    """What is refused, and with what message."""

    def test_an_unknown_dataset_is_refused(self):
        """A dataset outside the curated set has no style to resolve."""
        with pytest.raises(ValueError, match="is not a curated GEE dataset"):
            style_for("NO/SUCH", ("b1",), catalog=_catalog(_row()))

    def test_an_unknown_band_is_refused_through_the_catalogs_own_check(self):
        """The message is `Dataset.get_band`'s, so the wording cannot drift."""
        with pytest.raises(ValueError, match=r"is not a band of 'T/EST'"):
            style_for("T/EST", ("b2",), catalog=_catalog(_row()))

    def test_a_near_miss_band_carries_the_closest_match_suggestion(self):
        """A plausible typo gets the same did-you-mean the catalog gives."""
        row = _row(bands={"elevation": Band(id="elevation", min=0, max=1)})
        with pytest.raises(ValueError, match="Did you mean 'elevation'"):
            style_for("T/EST", ("elevashun",), catalog=_catalog(row))

    def test_an_empty_band_tuple_is_a_mistake_not_a_default(self):
        """`bands=()` is refused rather than silently meaning "the default"."""
        with pytest.raises(ValueError, match="names no band to style"):
            style_for("T/EST", (), catalog=_catalog(_row()))

    def test_a_row_with_no_bands_is_refused(self):
        """An unhydrated row has nothing to draw."""
        row = _row(bands={})
        with pytest.raises(ValueError, match="carries no bands"):
            style_for("T/EST", None, catalog=_catalog(row))


class TestAgainstTheRealCatalog:
    """The bundled catalog answers for real assets, not just synthetic rows."""

    def test_a_real_single_band_dataset_resolves_to_a_one_band_stretch(self):
        """SRTM elevation resolves to a single `(min, max)` pair.

        It carries a publisher rendering after the `gee:visualizations`
        hydration, so `source` is `"catalog"` and the exact numbers are the
        publisher's rather than the band's declared range — asserted as shape,
        not value, so a future re-hydration does not break the test.
        """
        hints = style_for("USGS/SRTMGL1_003", ("elevation",))
        assert len(hints.limits) == 1
        assert hints.source == "catalog"

    def test_a_dataset_with_no_publisher_rendering_uses_the_heuristic(self):
        """A row the STAC gives no rendering falls back to band metadata.

        `MODIS/006/MOD44W` (or any unhydrated row) has no `visualizations`, so
        the result is `source="heuristic"`; the test discovers one from the
        live catalog rather than pinning an id that a re-hydration might fill.
        """
        from earthlens.gee.catalog import Catalog

        catalog = Catalog()
        bare = next(
            (
                asset_id
                for asset_id, row in catalog.datasets.items()
                if not row.visualizations
                and row.bands
                and any(b.min is not None and b.max is not None for b in row.bands.values())
            ),
            None,
        )
        assert bare is not None, "expected at least one row with no rendering"
        band = next(
            b.id
            for b in catalog.datasets[bare].bands.values()
            if b.min is not None and b.max is not None
        )
        hints = style_for(bare, (band,), catalog=catalog)
        assert hints.source == "heuristic"

    def test_a_real_three_band_request_is_a_composite(self):
        """A Sentinel-2 true-colour request renders as RGB."""
        hints = style_for("COPERNICUS/S2_SR_HARMONIZED", ("B4", "B3", "B2"))
        assert hints.is_rgb
        assert hints.bands == ("B4", "B3", "B2")
