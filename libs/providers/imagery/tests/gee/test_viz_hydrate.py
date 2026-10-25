"""Tests for capturing STAC `gee:visualizations` into catalog rows (Task G1b).

Entirely offline: the STAC read is behind `_stac_doc`, so the parser and the
stanza splicer are exercised against hand-built documents.
"""

from __future__ import annotations

import pytest

from earthlens.gee._viz_hydrate import (
    bulk_hydrate_visualizations,
    splice_visualizations,
    visualization_lines,
    visualizations_from_stac,
)
from earthlens.gee.catalog import Visualization


def _doc(*band_vis, names=None):
    """Build a STAC document carrying the given `band_vis` mappings."""
    labels = names or [f"V{i}" for i in range(len(band_vis))]
    return {
        "summaries": {
            "gee:visualizations": [
                {
                    "display_name": label,
                    "image_visualization": {"band_vis": bv},
                }
                for label, bv in zip(labels, band_vis, strict=True)
            ]
        }
    }


class TestVisualizationsFromStac:
    """Parsing the upstream shape, as upstream actually serves it."""

    def test_the_sentinel2_rgb_shape_parses(self):
        """One stretch broadcast over three bands, which is the common case."""
        doc = _doc({"bands": ["B4", "B3", "B2"], "min": [0], "max": [3000]})
        assert visualizations_from_stac(doc) == [
            {
                "name": "V0",
                "bands": ("B4", "B3", "B2"),
                "vmin": (0.0,),
                "vmax": (3000.0,),
            }
        ]

    def test_gamma_palette_and_gain_are_captured(self):
        """Every optional key upstream uses is read."""
        doc = _doc(
            {
                "bands": ["NDVI"],
                "min": [0],
                "max": [8000],
                "gamma": [1.5],
                "gain": [0.0001],
                "palette": ["ffffff", "004c00"],
            }
        )
        parsed = visualizations_from_stac(doc)[0]
        assert parsed["gamma"] == (1.5,)
        assert parsed["gain"] == (0.0001,)
        assert parsed["palette"] == ("ffffff", "004c00")

    def test_a_bands_only_entry_parses(self):
        """`ESA/WorldCover/v200` carries no stretch at all."""
        parsed = visualizations_from_stac(_doc({"bands": ["Map"]}))[0]
        assert parsed == {"name": "V0", "bands": ("Map",)}

    def test_several_renderings_keep_upstream_order(self):
        """Landsat offers three; the order is the publisher's."""
        doc = _doc(
            {"bands": ["B4"]},
            {"bands": ["B5"]},
            names=["True Color", "Near Infrared"],
        )
        assert [v["name"] for v in visualizations_from_stac(doc)] == [
            "True Color",
            "Near Infrared",
        ]

    def test_a_palette_hash_prefix_is_stripped(self):
        """Earth Engine writes bare hex; a `#` would not round-trip."""
        doc = _doc({"bands": ["b"], "palette": ["#ffffff"]})
        assert visualizations_from_stac(doc)[0]["palette"] == ("ffffff",)

    def test_a_css_colour_name_is_kept_as_written(self):
        """A minority of rows use names, which Earth Engine also accepts."""
        doc = _doc({"bands": ["b"], "palette": ["blue", "red"]})
        assert visualizations_from_stac(doc)[0]["palette"] == ("blue", "red")

    def test_a_rendering_with_no_bands_is_dropped(self):
        """It cannot be drawn, and `Visualization` requires bands."""
        assert visualizations_from_stac(_doc({"min": [0]})) == []

    def test_a_missing_display_name_gets_a_positional_one(self):
        """A rendering stays selectable even with no upstream label."""
        doc = {
            "summaries": {
                "gee:visualizations": [
                    {"image_visualization": {"band_vis": {"bands": ["b"]}}}
                ]
            }
        }
        assert visualizations_from_stac(doc)[0]["name"] == "Visualization 1"

    @pytest.mark.parametrize("doc", [{}, {"summaries": {}}, {"summaries": None}])
    def test_a_document_with_no_renderings_yields_nothing(self, doc):
        """An asset the publisher never styled is not an error."""
        assert visualizations_from_stac(doc) == []

    def test_a_non_numeric_stretch_entry_is_skipped_not_fatal(self):
        """A stray null must not lose the whole asset's rendering."""
        doc = _doc({"bands": ["b"], "min": [0, None], "max": [1, 2]})
        assert visualizations_from_stac(doc)[0]["vmin"] == (0.0,)

    def test_a_scalar_stretch_is_accepted(self):
        """Upstream occasionally writes a bare number rather than a list."""
        doc = _doc({"bands": ["b"], "min": 0, "max": 100})
        assert visualizations_from_stac(doc)[0]["vmax"] == (100.0,)

    def test_the_parsed_kwargs_build_a_visualization(self):
        """The parser's output is exactly the model's constructor input."""
        doc = _doc({"bands": ["B4", "B3", "B2"], "min": [0], "max": [3000]})
        vis = Visualization(**visualizations_from_stac(doc)[0])
        assert vis.limits == ((0.0, 3000.0),) * 3


class TestVisualizationLines:
    """The YAML the hydrator writes."""

    def test_band_ids_are_quoted(self):
        """A band id is not always a YAML string: `1` must not become an int."""
        lines = visualization_lines([{"name": "V", "bands": ("1",)}])
        assert '        bands: ["1"]' in lines

    def test_an_integral_stretch_is_written_without_a_decimal_point(self):
        """`0` rather than `0.0`, matching how the catalog writes numbers."""
        lines = visualization_lines(
            [{"name": "V", "bands": ("b",), "vmin": (0.0,), "vmax": (3000.0,)}]
        )
        assert "        vmin: [0]" in lines
        assert "        vmax: [3000]" in lines

    def test_a_fractional_stretch_keeps_its_precision(self):
        """A real gain like 0.0001 must survive the round trip."""
        lines = visualization_lines([{"name": "V", "bands": ("b",), "gain": (0.0001,)}])
        assert "0.0001" in " ".join(lines)

    def test_no_entries_writes_nothing(self):
        """An asset with no renderings gets no empty block."""
        assert visualization_lines([]) == []

    def test_the_name_is_quoted(self):
        """Display names carry colons and parentheses."""
        lines = visualization_lines(
            [{"name": "Precipitation (mm/hr)", "bands": ("b",)}]
        )
        assert '- name: "Precipitation (mm/hr)"' in lines[1]


_STANZA = '  A/B:\n    title: t\n    bands:\n      b1:\n        min: 0\n'


class TestSpliceVisualizations:
    """Inserting the block into a real stanza shape."""

    def test_the_block_lands_immediately_before_bands(self):
        """Dataset-level fields stay together, with the band map last."""
        out = splice_visualizations(_STANZA, [{"name": "V", "bands": ("b1",)}])
        assert out.index("visualizations:") < out.index("    bands:")

    def test_the_result_is_valid_yaml_that_round_trips(self):
        """The written block parses back to what was written."""
        import yaml

        out = splice_visualizations(
            _STANZA,
            [{"name": "V", "bands": ("b1",), "vmin": (0.0,), "vmax": (1.0,)}],
        )
        parsed = yaml.safe_load(out)["A/B"]["visualizations"][0]
        assert parsed == {"name": "V", "bands": ["b1"], "vmin": [0], "vmax": [1]}

    def test_re_running_replaces_rather_than_duplicating(self):
        """The hydration is idempotent, so it can be re-run after an update."""
        once = splice_visualizations(_STANZA, [{"name": "A", "bands": ("b1",)}])
        twice = splice_visualizations(once, [{"name": "B", "bands": ("b1",)}])
        assert twice.count("visualizations:") == 1
        assert 'name: "B"' in twice

    def test_no_entries_leaves_a_clean_stanza_untouched(self):
        """Nothing to write changes nothing."""
        assert splice_visualizations(_STANZA, []) == _STANZA

    def test_no_entries_removes_an_existing_block(self):
        """An upstream rendering that disappeared is removed, not stranded."""
        once = splice_visualizations(_STANZA, [{"name": "A", "bands": ("b1",)}])
        assert splice_visualizations(once, []) == _STANZA

    def test_the_band_map_survives_the_splice(self):
        """The hydration must not disturb the rows around it."""
        out = splice_visualizations(_STANZA, [{"name": "V", "bands": ("b1",)}])
        assert "      b1:\n        min: 0\n" in out

    def test_a_stanza_with_no_bands_block_still_gets_the_block(self):
        """An unhydrated row appends rather than silently skipping."""
        stanza = "  A/B:\n    title: t\n"
        out = splice_visualizations(stanza, [{"name": "V", "bands": ("b1",)}])
        assert "visualizations:" in out


class TestBulkHydrate:
    """The driver, with the STAC read faked."""

    def test_an_unserved_asset_is_counted_not_fatal(self, monkeypatch):
        """Community `projects/...` ids have no public STAC document."""
        monkeypatch.setattr(
            "earthlens.gee._viz_hydrate._stac_doc", lambda asset_id: None
        )
        report = bulk_hydrate_visualizations(limit=3)
        assert report["unserved"] == 3
        assert report["with_visualizations"] == 0

    def test_an_asset_with_no_renderings_is_counted_separately(self, monkeypatch):
        """Served-but-unstyled is a different fact from unserved."""
        monkeypatch.setattr(
            "earthlens.gee._viz_hydrate._stac_doc",
            lambda asset_id: {"summaries": {}},
        )
        report = bulk_hydrate_visualizations(limit=2)
        assert report["without"] == 2
        assert report["unserved"] == 0

    def test_a_dry_run_writes_nothing(self, monkeypatch):
        """The default reports what would change without touching the tree."""
        monkeypatch.setattr(
            "earthlens.gee._viz_hydrate._stac_doc",
            lambda asset_id: _doc({"bands": ["x"], "min": [0], "max": [1]}),
        )
        assert bulk_hydrate_visualizations(limit=1)["wrote"] is False

    def test_the_limit_bounds_the_work(self, monkeypatch):
        """A smoke run is possible before committing to ~1100 reads."""
        monkeypatch.setattr(
            "earthlens.gee._viz_hydrate._stac_doc", lambda asset_id: None
        )
        assert bulk_hydrate_visualizations(limit=5)["considered"] == 5
