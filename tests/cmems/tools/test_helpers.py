"""Tests for tools/cmems/_helpers.py."""

from __future__ import annotations

import re

import pytest

import _helpers  # noqa: E402 (sys.path injection happens in conftest.py)
from tests.cmems.tools.conftest import (
    FakeDataset,
    FakeProduct,
    FakeVariable,
    make_dataset,
)


class TestCadenceForDatasetId:
    """Suffix -> earthlens cadence mapping."""

    @pytest.mark.parametrize(
        "dataset_id, expected",
        [
            ("cmems_mod_glo_phy_my_0.083deg_P1D-m", "daily"),
            ("cmems_mod_glo_phy_my_0.083deg_P1M-m", "monthly"),
            ("cmems_mod_glo_phy_my_0.083deg_PT1H-i", "hourly"),
            ("cmems_mod_glo_phy_my_0.083deg_P1Y-m", "annual"),
            ("cmems_mod_glo_phy_my_0.083deg-climatology_P1M-m", "climatology"),
            ("cmems_mod_glo_phy_my_2.5km_static", "irregular"),
            ("cmems_mod_glo_wav_my_0.2deg_PT6H-i", "6hourly"),
        ],
    )
    def test_known_suffix(self, dataset_id: str, expected: str) -> None:
        """Each documented suffix maps to its canonical cadence label."""
        assert _helpers.cadence_for_dataset_id(dataset_id) == expected, (
            f"{dataset_id!r} should map to {expected!r}"
        )

    def test_unknown_suffix_defaults_to_irregular(self) -> None:
        """No suffix match falls through to irregular."""
        assert _helpers.cadence_for_dataset_id("weird-id-no-suffix") == "irregular"

    def test_climatology_beats_monthly(self) -> None:
        """`-climatology_P1M-m` resolves to climatology, not monthly."""
        result = _helpers.cadence_for_dataset_id(
            "cmems_mod_glo_phy_my_0.083deg-climatology_P1M-m"
        )
        assert result == "climatology", f"got {result!r}"


class TestDomainForProductId:
    """Product-id prefix -> domain mapping."""

    @pytest.mark.parametrize(
        "product_id, expected",
        [
            ("GLOBAL_MULTIYEAR_PHY_001_030", "global"),
            ("MEDSEA_MULTIYEAR_PHY_006_004", "mediterranean"),
            ("BLKSEA_MULTIYEAR_PHY_007_004", "black-sea"),
            ("BALTICSEA_MULTIYEAR_PHY_003_011", "baltic-sea"),
            ("ARCTIC_MULTIYEAR_PHY_002_003", "arctic"),
            ("IBI_MULTIYEAR_PHY_005_002", "ibi"),
            ("NWSHELF_MULTIYEAR_PHY_004_009", "nw-shelf"),
            ("SEAICE_GLO_PHY_L4_NRT_011_014", "polar"),
            ("OMI_GLO_INDICATOR_X", "indicator"),
            ("INSITU_GLO_PHY_TS_OBS_013_001", "global"),
            ("SST_GLO_SST_L4_NRT_OBSERVATIONS_010_001", "global"),
            ("SST_MED_SST_L4_NRT_OBS", "mediterranean"),
            ("OCEANCOLOUR_BAL_BGC", "baltic-sea"),
        ],
    )
    def test_prefix_mapping(self, product_id: str, expected: str) -> None:
        """Each documented prefix routes to its domain."""
        assert _helpers.domain_for_product_id(product_id) == expected

    def test_longest_prefix_wins(self) -> None:
        """A `SST_GLO_*` id should hit the thematic-region prefix, not `SST_`."""
        assert _helpers.domain_for_product_id("SST_GLO_SST_L4_REP") == "global"
        assert _helpers.domain_for_product_id("SST_NWS_SST_L3") == "nw-shelf"

    def test_unknown_prefix_defaults_to_global(self) -> None:
        """Unrecognised product_id falls through to global."""
        assert _helpers.domain_for_product_id("ZZZ_UNKNOWN") == "global"


class TestHumanizeStandardName:
    """Snake_case CF standard_name -> capitalised long_name."""

    def test_underscore_to_space(self) -> None:
        """Underscores become spaces."""
        out = _helpers.humanize_standard_name("sea_water_potential_temperature")
        assert out == "Sea water potential temperature"

    def test_empty_string_returns_empty(self) -> None:
        """Falsy input is preserved."""
        assert _helpers.humanize_standard_name("") == ""

    def test_none_returns_empty(self) -> None:
        """None input maps to empty string, not raising."""
        assert _helpers.humanize_standard_name(None) == ""

    def test_single_word(self) -> None:
        """A single-word standard_name is just capitalised."""
        assert _helpers.humanize_standard_name("chlorophyll") == "Chlorophyll"


class TestYamlScalar:
    """`_yaml_scalar` quoting policy via yaml.safe_dump round-trip."""

    @pytest.mark.parametrize(
        "value, parsed",
        [
            ("degrees_C", "degrees_C"),
            ("1e-3", "1e-3"),
            ("%", "%"),
            ("1", "1"),
            ("mg m-3", "mg m-3"),
        ],
    )
    def test_round_trip_via_yaml(self, value: str, parsed: str) -> None:
        """Emitted scalar parses back to the input string via yaml.safe_load."""
        import yaml

        rendered = _helpers._yaml_scalar(value)
        loaded = yaml.safe_load(f"v: {rendered}")
        assert loaded["v"] == parsed, (
            f"{value!r} rendered as {rendered!r} round-trips to {loaded['v']!r}"
        )

    def test_empty_string(self) -> None:
        """Empty input renders as empty quoted scalar."""
        rendered = _helpers._yaml_scalar("")
        assert "'" in rendered or '"' in rendered, (
            f"empty string should be quoted to survive YAML parsing: {rendered!r}"
        )

    def test_none_input_renders_empty(self) -> None:
        """`None` is rendered as empty string."""
        rendered = _helpers._yaml_scalar(None)
        assert rendered in ("''", '""'), f"got {rendered!r}"


class TestWalkVariables:
    """Service traversal."""

    def test_first_service_with_variables(self) -> None:
        """Picks the first non-empty service; sorts by short_name."""
        dataset = make_dataset(
            "ds-1",
            [
                FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature"),
                FakeVariable("so", "1e-3", "sea_water_salinity"),
                FakeVariable("zos", "m", "sea_surface_height_above_geoid"),
            ],
        )
        vars_ = _helpers.walk_variables(dataset)
        assert [v.short_name for v in vars_] == ["so", "thetao", "zos"]

    def test_skips_empty_services_to_first_populated(self) -> None:
        """Empty leading services don't shadow a populated trailing one."""
        from tests.cmems.tools.conftest import (
            FakePart,
            FakeService,
            FakeVersion,
        )

        empty = FakeService([])
        populated = FakeService(
            [FakeVariable("chl", "mg m-3", "mass_concentration_of_chlorophyll")]
        )
        dataset = FakeDataset(
            "ds-2",
            [FakeVersion([FakePart([empty, populated])])],
        )
        vars_ = _helpers.walk_variables(dataset)
        assert [v.short_name for v in vars_] == ["chl"]

    def test_no_services_returns_empty(self) -> None:
        """All-empty service tree yields an empty list, not an exception."""
        from tests.cmems.tools.conftest import (
            FakePart,
            FakeService,
            FakeVersion,
        )

        dataset = FakeDataset("ds-3", [FakeVersion([FakePart([FakeService([])])])])
        assert _helpers.walk_variables(dataset) == []


class TestEmitDatasetStanza:
    """Full per-dataset YAML stanza rendering."""

    def test_emits_canonical_shape(self, fake_product: FakeProduct) -> None:
        """Output has every key the schema expects, in the canonical order."""
        dataset = fake_product.datasets[0]
        stanza = _helpers.emit_dataset_stanza(fake_product, dataset)
        lines = stanza.splitlines()
        assert lines[0] == f"  {dataset.dataset_id}:"
        assert "    product: GLOBAL_MULTIYEAR_PHY_001_030" in lines
        assert "    cadence: daily" in lines
        assert "    domain: global" in lines
        assert any(line.startswith("    title:") for line in lines)
        assert "    temporal:" in lines
        assert "    variables:" in lines

    def test_humanises_standard_name(self, fake_product: FakeProduct) -> None:
        """Variable rows carry humanised long_name lines."""
        stanza = _helpers.emit_dataset_stanza(
            fake_product, fake_product.datasets[0]
        )
        assert "long_name: Sea water potential temperature" in stanza
        assert "long_name: Sea water salinity" in stanza

    def test_quotes_unit_strings_that_yaml_would_misparse(
        self, fake_product: FakeProduct
    ) -> None:
        """Units like `1e-3` survive yaml round-trip as strings, not floats."""
        import yaml

        stanza = _helpers.emit_dataset_stanza(
            fake_product, fake_product.datasets[0]
        )
        wrapped = "datasets:\n" + stanza
        loaded = yaml.safe_load(wrapped)
        so = loaded["datasets"][fake_product.datasets[0].dataset_id]["variables"]["so"]
        assert so["units"] == "1e-3", f"got {so['units']!r} ({type(so['units']).__name__})"
        assert isinstance(so["units"], str)

    def test_no_variables_emits_todo_marker(self) -> None:
        """An empty-variables dataset still renders a stanza with a TODO marker."""
        from tests.cmems.tools.conftest import (
            FakePart,
            FakeService,
            FakeVersion,
        )

        dataset = FakeDataset(
            "empty-ds",
            [FakeVersion([FakePart([FakeService([])])])],
        )
        product = FakeProduct("EMPTY_PROD", [dataset])
        stanza = _helpers.emit_dataset_stanza(product, dataset)
        assert "variables: {}" in stanza
        assert "TODO" in stanza

    def test_variable_with_missing_standard_name_emits_todo(self) -> None:
        """An empty standard_name lands as `long_name: ''` with a TODO comment."""
        dataset = make_dataset(
            "synth-ds",
            [FakeVariable("climatology_bounds", "hours since 1950-01-01", None)],
        )
        product = FakeProduct("SYNTH", [dataset])
        stanza = _helpers.emit_dataset_stanza(product, dataset)
        assert "long_name: ''" in stanza
        assert "TODO: standard_name missing" in stanza

    def test_uses_dataset_name_for_title(self, fake_product: FakeProduct) -> None:
        """`dataset_name` wins over `product.title` when present."""
        stanza = _helpers.emit_dataset_stanza(
            fake_product, fake_product.datasets[0]
        )
        assert "title: GLORYS12 daily mean" in stanza

    def test_falls_back_to_product_title(self, fake_product: FakeProduct) -> None:
        """No dataset_name -> product.title; no product.title -> dataset_id."""
        dataset = make_dataset(
            "fallback-ds", [FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature")]
        )
        product = FakeProduct("PROD", [dataset], title="My Product Title")
        stanza = _helpers.emit_dataset_stanza(product, dataset)
        assert "title: My Product Title" in stanza


class TestRenderAvailableProductsBlock:
    """`available_products:` block formatter."""

    def test_basic_block(self) -> None:
        """Three ids render as a sorted bullet list with a header."""
        block = _helpers.render_available_products_block(
            ["GLOBAL_X", "ARCTIC_Y", "MEDSEA_Z"]
        )
        assert block.startswith("available_products:\n")
        assert block.endswith("\n")
        body = block.splitlines()[1:]
        assert body == ["  - ARCTIC_Y", "  - GLOBAL_X", "  - MEDSEA_Z"]

    def test_deduplicates(self) -> None:
        """Duplicate ids are collapsed."""
        block = _helpers.render_available_products_block(
            ["A", "A", "B", "A"]
        )
        assert block.splitlines()[1:] == ["  - A", "  - B"]

    def test_empty_input(self) -> None:
        """No products -> header line only."""
        assert _helpers.render_available_products_block([]) == "available_products:\n"


class TestSpliceAvailableProducts:
    """In-place rewrite of the `available_products:` block."""

    def test_replaces_existing_block(self) -> None:
        """The old block is removed and the new one substituted in."""
        original = (
            "# header\n"
            "available_products:\n"
            "  - OLD_A\n"
            "  - OLD_B\n"
            "\n"
            "datasets:\n"
            "  ds-1:\n"
            "    product: P\n"
        )
        new_block = "available_products:\n  - NEW_A\n  - NEW_B\n"
        rewritten = _helpers.splice_available_products(original, new_block)
        assert "OLD_A" not in rewritten
        assert "NEW_A" in rewritten
        assert "datasets:\n  ds-1:\n    product: P\n" in rewritten

    def test_missing_block_raises(self) -> None:
        """A YAML without an available_products header errors clearly."""
        with pytest.raises(ValueError, match="available_products"):
            _helpers.splice_available_products("datasets:\n  ds-1: {}\n", "")


class TestFindDatasetStanzaSpan:
    """Locate one dataset stanza in catalog text."""

    YAML = (
        "datasets:\n"
        "  ds-alpha:\n"
        "    product: A\n"
        "    title: alpha\n"
        "  ds-beta:\n"
        "    product: B\n"
        "    title: beta\n"
    )

    def test_finds_first(self) -> None:
        """`ds-alpha` returns offsets bounded by `ds-beta`'s head."""
        span = _helpers.find_dataset_stanza_span(self.YAML, "ds-alpha")
        assert span is not None
        start, end = span
        chunk = self.YAML[start:end]
        assert chunk.startswith("  ds-alpha:")
        assert "ds-beta" not in chunk

    def test_finds_last(self) -> None:
        """`ds-beta` extends to end of input."""
        span = _helpers.find_dataset_stanza_span(self.YAML, "ds-beta")
        assert span is not None
        start, end = span
        assert end == len(self.YAML)
        assert self.YAML[start:end].startswith("  ds-beta:")

    def test_missing_id(self) -> None:
        """An unknown id returns None."""
        assert _helpers.find_dataset_stanza_span(self.YAML, "not-there") is None


class TestAppendStanzasToDatasetsBlock:
    """Appending new stanzas to the curated block."""

    def test_appends_with_blank_separator(self) -> None:
        """One blank line ends up between the old tail and the new stanza."""
        original = "datasets:\n  old:\n    product: P\n"
        new = "  new:\n    product: Q\n"
        out = _helpers.append_stanzas_to_datasets_block(original, new)
        assert out.endswith("\n")
        assert "old:" in out
        assert "new:" in out
        idx_old = out.index("old:")
        idx_new = out.index("new:")
        assert idx_old < idx_new

    def test_missing_datasets_header_raises(self) -> None:
        """No `datasets:` -> ValueError, not silent append."""
        with pytest.raises(ValueError, match="datasets:"):
            _helpers.append_stanzas_to_datasets_block(
                "available_products: []\n", "  new: {}\n"
            )


class TestCompactText:
    """Normalisation of multi-stanza emitted output."""

    def test_strips_scratch_markers(self) -> None:
        """`# ----` and `# product:` annotation lines are dropped."""
        raw = (
            "# ---- paste under `datasets:` ----\n"
            "# product: GLOBAL_X (4 datasets)\n"
            "  ds-1:\n"
            "    product: P\n"
        )
        out = _helpers.compact_text(raw)
        assert "# ---- paste" not in out
        assert "# product:" not in out
        assert "  ds-1:" in out

    def test_normalises_crlf(self) -> None:
        """CRLF line endings collapse to LF."""
        raw = "  ds-1:\r\n    product: P\r\n"
        out = _helpers.compact_text(raw)
        assert "\r" not in out

    def test_collapses_multiple_blank_lines(self) -> None:
        """Three+ blank lines collapse to a single blank line."""
        raw = "  a:\n    x: 1\n\n\n\n  b:\n    x: 2\n"
        out = _helpers.compact_text(raw)
        assert not re.search(r"\n{3,}", out)
        assert "  a:" in out and "  b:" in out

    def test_strips_trailing_whitespace_per_line(self) -> None:
        """Trailing whitespace on each line is stripped."""
        raw = "  ds:\n    product: P   \n"
        out = _helpers.compact_text(raw)
        assert "    product: P\n" in out
