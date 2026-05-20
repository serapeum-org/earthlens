"""Tests for tools/cmems/_helpers.py."""

from __future__ import annotations

import re

import pytest

import _helpers  # noqa: E402 (sys.path injection happens in conftest.py)
from tests.cmems.tools.conftest import (
    FakeCoordinate,
    FakeDataset,
    FakePart,
    FakeProduct,
    FakeService,
    FakeVariable,
    FakeVersion,
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


class TestCatalogFileFor:
    """Per-file routing of a dataset by its product_id."""

    @pytest.mark.parametrize(
        "product_id, expected",
        [
            ("GLOBAL_MULTIYEAR_PHY_001_030", "global-physics"),
            ("GLOBAL_MULTIYEAR_BGC_001_029", "global-biogeochem"),
            ("OCEANCOLOUR_GLO_BGC_L4_MY_009_104", "global-biogeochem"),
            ("GLOBAL_ANALYSISFORECAST_WAV_001_027", "global-wave"),
            ("WIND_GLO_PHY_L4_NRT_012_004", "global-wind"),
            ("SST_GLO_SST_L4_NRT_OBSERVATIONS_010_001", "global-sst"),
            ("SEALEVEL_GLO_PHY_L4_MY_008_047", "global-sealevel"),
            ("INSITU_GLO_PHY_TS_OBS_013_001", "global-observations"),
            ("MULTIOBS_GLO_PHY_S_SURFACE_MYNRT_015_013", "global-observations"),
            ("MEDSEA_MULTIYEAR_PHY_006_004", "mediterranean"),
            ("ARCTIC_MULTIYEAR_PHY_002_003", "arctic"),
            ("SEAICE_GLO_PHY_L4_NRT_011_014", "polar"),
        ],
    )
    def test_routing(self, product_id: str, expected: str) -> None:
        """Global datasets route to a theme file; regional ones to their domain."""
        assert _helpers.catalog_file_for(product_id) == expected

    def test_unrecognised_global_falls_to_other(self) -> None:
        """A GLOBAL_ product with no theme token lands in global-other."""
        assert _helpers.catalog_file_for("GLOBAL_MYSTERY_DATASET_000") == "global-other"


class TestRenderAvailableDatasetsBlock:
    """`available_datasets:` block formatter."""

    def test_basic_block(self) -> None:
        """Three ids render as a sorted bullet list with a header."""
        block = _helpers.render_available_datasets_block(["ds_x", "ds_a", "ds_m"])
        assert block.startswith("available_datasets:\n")
        assert block.endswith("\n")
        body = block.splitlines()[1:]
        assert body == ["  - ds_a", "  - ds_m", "  - ds_x"]

    def test_deduplicates(self) -> None:
        """Duplicate ids are collapsed."""
        block = _helpers.render_available_datasets_block(["a", "a", "b", "a"])
        assert block.splitlines()[1:] == ["  - a", "  - b"]

    def test_empty_input(self) -> None:
        """No datasets -> header line only."""
        assert _helpers.render_available_datasets_block([]) == "available_datasets:\n"


class TestSpliceAvailableDatasets:
    """In-place rewrite of the `available_datasets:` block in _index.yaml."""

    def test_replaces_existing_block(self) -> None:
        """The old block is removed and the new one substituted in."""
        original = (
            "# header comment\n"
            "available_datasets:\n"
            "  - OLD_A\n"
            "  - OLD_B\n"
        )
        new_block = "available_datasets:\n  - NEW_A\n  - NEW_B\n"
        rewritten = _helpers.splice_available_datasets(original, new_block)
        assert "OLD_A" not in rewritten
        assert "NEW_A" in rewritten
        assert "# header comment" in rewritten

    def test_missing_block_raises(self) -> None:
        """Text without an available_datasets header errors clearly."""
        with pytest.raises(ValueError, match="available_datasets"):
            _helpers.splice_available_datasets("# just a comment\n", "")


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
                "available_datasets: []\n", "  new: {}\n"
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


class TestParseTimeUnit:
    """`_parse_time_unit` parses CF `<unit> since <date>` strings."""

    @pytest.mark.parametrize(
        "unit, expected_scale, expected_epoch_year",
        [
            ("milliseconds since 1970-01-01 00:00:00Z (no leap seconds)", 1e-3, 1970),
            ("hours since 1950-01-01", 3600.0, 1950),
            ("seconds since 2000-01-01", 1.0, 2000),
            ("days since 1900-01-01", 86400.0, 1900),
        ],
    )
    def test_known_units(self, unit, expected_scale, expected_epoch_year):
        """Each supported unit word + epoch parses to (seconds, epoch)."""
        parsed = _helpers._parse_time_unit(unit)
        assert parsed is not None, f"{unit!r} should parse"
        scale, epoch = parsed
        assert scale == expected_scale, f"scale for {unit!r}: expected {expected_scale}, got {scale}"
        assert epoch.year == expected_epoch_year, (
            f"epoch year for {unit!r}: expected {expected_epoch_year}, got {epoch.year}"
        )

    @pytest.mark.parametrize("unit", [None, "", "not a time unit", "parsecs since 1970-01-01"])
    def test_unparseable_returns_none(self, unit):
        """Empty, malformed, or unknown-unit strings return None."""
        assert _helpers._parse_time_unit(unit) is None, f"{unit!r} should not parse"


def _dataset_with_time_coord(coord: FakeCoordinate | None) -> FakeDataset:
    """Build a dataset whose arco service variable carries `coord` (or none)."""
    var = FakeVariable("thetao", "degrees_C", "sea_water_potential_temperature",
                       coordinates=[coord] if coord else [])
    return FakeDataset("ds-x", [FakeVersion([FakePart([FakeService([var])])])])


class TestTemporalBounds:
    """`temporal_bounds` reads start/end from the arco time coordinate."""

    def test_milliseconds_since_1970(self):
        """A ms-since-1970 time coord converts min/max to ISO dates."""
        # 1993-01-01 = 725846400000 ms; 2020-01-01 = 1577836800000 ms.
        coord = FakeCoordinate(
            "time",
            "milliseconds since 1970-01-01 00:00:00Z",
            minimum_value=725846400000.0,
            maximum_value=1577836800000.0,
        )
        start, end = _helpers.temporal_bounds(_dataset_with_time_coord(coord))
        assert start == "1993-01-01", f"start: expected 1993-01-01, got {start}"
        assert end == "2020-01-01", f"end: expected 2020-01-01, got {end}"

    def test_hours_since_1950(self):
        """An hours-since-1950 time coord converts correctly."""
        # 1950-01-01 + 0 h = 1950-01-01; + 24 h = 1950-01-02.
        coord = FakeCoordinate("time", "hours since 1950-01-01",
                               minimum_value=0.0, maximum_value=24.0)
        start, end = _helpers.temporal_bounds(_dataset_with_time_coord(coord))
        assert (start, end) == ("1950-01-01", "1950-01-02"), f"got {(start, end)}"

    def test_no_time_coordinate_returns_none(self):
        """A dataset whose variables carry no time coord yields (None, None)."""
        start, end = _helpers.temporal_bounds(_dataset_with_time_coord(None))
        assert (start, end) == (None, None), f"expected (None, None), got {(start, end)}"

    def test_unparseable_unit_returns_none(self):
        """A time coord with an unparseable unit yields (None, None)."""
        coord = FakeCoordinate("time", "garbage unit", minimum_value=1.0, maximum_value=2.0)
        assert _helpers.temporal_bounds(_dataset_with_time_coord(coord)) == (None, None)

    def test_missing_min_returns_none(self):
        """A time coord without a minimum_value yields (None, None)."""
        coord = FakeCoordinate("time", "hours since 1950-01-01",
                               minimum_value=None, maximum_value=24.0)
        assert _helpers.temporal_bounds(_dataset_with_time_coord(coord)) == (None, None)

    def test_max_none_gives_start_only(self):
        """A time coord with min but no max yields a start and a None end."""
        coord = FakeCoordinate("time", "hours since 1950-01-01",
                               minimum_value=0.0, maximum_value=None)
        start, end = _helpers.temporal_bounds(_dataset_with_time_coord(coord))
        assert start == "1950-01-01" and end is None, f"got {(start, end)}"
