"""Unit tests for `earthlens.mswep.catalog`."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.mswep.catalog import (
    CATALOG_PATH,
    Catalog,
    MswepVariant,
    ProvisionalValueError,
    clear_catalog_cache,
)

pytestmark = [pytest.mark.mswep, pytest.mark.unit]

MINIMAL = """\
license: CC-BY-NC
attribution: cite me
nrt_revision_days: 10
granule_warn_threshold: 500
products:
  mswep:
    path_template: "{root}/{variant}/{temporal}/{stem}.nc"
    default_version: "3.15"
    versions:
      "3.15":
        root: MSWEP_V315
    variants:
      Past:
        start: "1979-01-01"
        end: "2024-12-31"
    resolutions:
      daily:
        folder: Daily
        stem: "%Y%j"
        step: P1D
    variables:
      precipitation:
        netcdf_field: precipitation
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    """Drop the parse cache so each test reads its own YAML."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


def _write(tmp_path, text):
    """Write `text` as a catalog YAML and return its path."""
    path = tmp_path / "cat.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestShippedCatalog:
    """The bundled `mswep_data_catalog.yaml`."""

    def test_ships_with_the_package(self):
        """The catalog YAML is present next to the module."""
        assert CATALOG_PATH.exists()

    def test_products_are_mswep_and_mswx(self):
        """Both GloH2O products are registered."""
        assert Catalog().products() == ["mswep", "mswx"]

    def test_license_is_non_commercial(self):
        """The licence id matches the label `warn_license` treats as restrictive."""
        assert Catalog().license_id == "CC-BY-NC"

    def test_attribution_cites_the_v3_paper(self):
        """The required citation names the MSWEP V3 reference."""
        assert "Wang" in Catalog().attribution

    def test_nrt_revision_window_is_ten_days(self):
        """GloH2O revises the trailing ten days of NRT."""
        assert Catalog().nrt_revision_days == 10

    def test_granule_warn_threshold_is_set(self):
        """A positive threshold arms the bulk-request warning."""
        assert Catalog().granule_warn_threshold > 0


class TestPathShapes:
    """The two products carry different Drive path shapes."""

    def test_mswep_has_no_variable_level(self):
        """MSWEP paths go straight from variant to temporal folder."""
        product = Catalog().get_product("mswep")
        assert product.path_template == "{root}/{variant}/{temporal}/{stem}.nc"
        assert not product.needs_variable_folder

    def test_mswx_inserts_a_variable_level(self):
        """MSWX shards by variable between variant and temporal folder."""
        product = Catalog().get_product("mswx")
        assert product.path_template == (
            "{root}/{variant}/{variable}/{temporal}/{stem}.nc"
        )
        assert product.needs_variable_folder


class TestVersions:
    """Version-stamped root folders."""

    def test_versions_map_to_stamped_roots(self):
        """Each version names its own coexisting Drive root folder."""
        versions = Catalog().get_product("mswep").versions
        assert versions["2.80"].root == "MSWEP_V280"
        assert versions["3.15"].root == "MSWEP_V315"

    def test_default_version_is_registered(self):
        """The default version key exists in the version map."""
        product = Catalog().get_product("mswep")
        assert product.default_version in product.versions

    def test_v280_documents_the_trend_defect(self):
        """V2.80 carries the note about the V3.15/V3.16 trend artifact."""
        assert "trend" in Catalog().get_product("mswep").versions["2.80"].description

    def test_v316_root_is_provisional(self):
        """The live V3.16 folder name is unconfirmed, so it is flagged."""
        assert Catalog().get_product("mswep").versions["3.16"].provisional

    def test_mswx_version_root(self):
        """MSWX ships under its own version-stamped root."""
        assert Catalog().get_product("mswx").versions["1.00"].root == "MSWX_V100"


class TestVariants:
    """Variant windows and date-based routing."""

    def test_past_ends_with_2024(self):
        """The gauge-corrected history stops at the end of 2024."""
        assert Catalog().get_product("mswep").variants["Past"].end == dt.date(
            2024, 12, 31
        )

    def test_nrt_starts_in_2025(self):
        """Near-real-time picks up where the history stops."""
        assert Catalog().get_product("mswep").variants["NRT"].start == dt.date(
            2025, 1, 1
        )

    def test_nrt_has_no_end(self):
        """NRT runs to real time, so its window is open-ended."""
        assert Catalog().get_product("mswep").variants["NRT"].end is None

    def test_all_three_mswep_variants_present(self):
        """Both history variants and NRT are registered."""
        assert set(Catalog().get_product("mswep").variants) == {
            "Past",
            "Past_nogauge",
            "NRT",
        }

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (dt.date(1979, 1, 1), "Past"),
            (dt.date(2020, 4, 25), "Past"),
            (dt.date(2024, 12, 31), "Past"),
            (dt.date(2025, 1, 1), "NRT"),
            (dt.date(2026, 6, 1), "NRT"),
        ],
    )
    def test_variant_for_routes_by_date(self, day, expected):
        """A date maps to the variant whose window covers it, across the cut-over."""
        assert Catalog().get_product("mswep").variant_for(day) == expected

    def test_variant_before_the_record_is_unroutable(self):
        """A pre-1979 date falls outside every variant."""
        assert Catalog().get_product("mswep").variant_for(dt.date(1970, 1, 1)) is None

    def test_covers_is_inclusive_at_both_bounds(self):
        """The window includes its start and end dates."""
        variant = MswepVariant(start=dt.date(2000, 1, 1), end=dt.date(2000, 12, 31))
        assert variant.covers(dt.date(2000, 1, 1))
        assert variant.covers(dt.date(2000, 12, 31))
        assert not variant.covers(dt.date(1999, 12, 31))
        assert not variant.covers(dt.date(2001, 1, 1))

    def test_unbounded_variant_covers_everything(self):
        """A variant with no bounds accepts any date."""
        assert MswepVariant().covers(dt.date(1800, 1, 1))


class TestResolutions:
    """Temporal folders and file-name stems."""

    @pytest.mark.parametrize(
        ("key", "folder", "stem", "units"),
        [
            ("hourly", "Hourly", "%Y%j.%H", "mm/hour"),
            ("3hourly", "3hourly", "%Y%j.%H", "mm/3-hour"),
            ("daily", "Daily", "%Y%j", "mm/day"),
            ("monthly", "Monthly", "%Y%m", "mm/month"),
        ],
    )
    def test_mswep_resolution_rows(self, key, folder, stem, units):
        """Each MSWEP resolution carries its upstream folder, stem and units."""
        row = Catalog().get_product("mswep").resolutions[key]
        assert (row.folder, row.stem, row.units) == (folder, stem, units)

    def test_folder_casing_is_preserved(self):
        """Upstream mixes casing — `Hourly` but `3hourly` — so it is not normalised."""
        resolutions = Catalog().get_product("mswep").resolutions
        assert resolutions["hourly"].folder == "Hourly"
        assert resolutions["3hourly"].folder == "3hourly"


class TestVariables:
    """Per-product variable sets."""

    def test_mswep_carries_only_precipitation(self):
        """MSWEP is a single-field product."""
        assert list(Catalog().get_product("mswep").variables) == ["precipitation"]

    def test_mswep_grid_dims_recorded(self):
        """The 0.1-degree global grid is 1800 x 3600."""
        variable = Catalog().get_product("mswep").variables["precipitation"]
        assert variable.dims == [1800, 3600]

    def test_mswx_carries_ten_variables(self):
        """MSWX ships the full meteorological forcing set."""
        assert len(Catalog().get_product("mswx").variables) == 10

    def test_only_temp_is_confirmed(self):
        """`Temp` is the one externally-verified MSWX folder spelling."""
        variables = Catalog().get_product("mswx").variables
        confirmed = [k for k, v in variables.items() if not v.provisional]
        assert confirmed == ["Temp"]


class TestProvisionalGuard:
    """Unverified values must fail loudly, not resolve to a missing path."""

    def test_provisional_row_raises(self):
        """A provisional variable is refused."""
        variables = Catalog().get_product("mswx").variables
        with pytest.raises(ProvisionalValueError, match="provisional"):
            Catalog.check_not_provisional(variables["SWd"], "MSWX variable 'SWd'")

    def test_message_names_the_row_and_the_fix(self):
        """The error quotes the row and points at the catalog flag."""
        variables = Catalog().get_product("mswx").variables
        with pytest.raises(ProvisionalValueError) as excinfo:
            Catalog.check_not_provisional(variables["LWd"], "MSWX variable 'LWd'")
        message = str(excinfo.value)
        assert "MSWX variable 'LWd'" in message
        assert "mswep_data_catalog.yaml" in message

    def test_confirmed_row_passes(self):
        """A verified row is not refused."""
        variables = Catalog().get_product("mswx").variables
        Catalog.check_not_provisional(variables["Temp"], "MSWX variable 'Temp'")

    def test_provisional_error_is_a_value_error(self):
        """Callers can catch it with a plain `except ValueError`."""
        assert issubclass(ProvisionalValueError, ValueError)


class TestLoading:
    """Parsing, validation and the did-you-mean surface."""

    def test_minimal_catalog_loads(self, tmp_path):
        """A one-product catalog parses into a usable Catalog."""
        catalog = Catalog.load(_write(tmp_path, MINIMAL))
        assert catalog.products() == ["mswep"]
        assert catalog.nrt_revision_days == 10

    def test_unknown_product_suggests_a_near_match(self):
        """A typo'd product name gets a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'mswep'"):
            Catalog().get_product("mswpe")

    def test_missing_products_block_raises(self, tmp_path):
        """A catalog with no products is rejected."""
        with pytest.raises(ValueError, match="products"):
            Catalog.load(_write(tmp_path, "license: CC-BY-NC\n"))

    def test_missing_required_field_raises(self, tmp_path):
        """A product without a path template names the missing field."""
        text = 'products:\n  mswep:\n    default_version: "1"\n'
        with pytest.raises(ValueError, match="path_template"):
            Catalog.load(_write(tmp_path, text))

    def test_unknown_row_field_is_rejected(self, tmp_path):
        """`extra="forbid"` catches a typo'd row key rather than dropping it."""
        text = MINIMAL.replace("        root: MSWEP_V315", "        rooot: MSWEP_V315")
        with pytest.raises(ValueError, match="versions"):
            Catalog.load(_write(tmp_path, text))

    def test_duplicate_keys_are_rejected(self, tmp_path):
        """The strict YAML loader refuses a duplicated mapping key."""
        text = MINIMAL + '  mswep:\n    path_template: "x"\n    default_version: "1"\n'
        with pytest.raises(ValueError):
            Catalog.load(_write(tmp_path, text))

    def test_injected_datasets_skip_the_disk_read(self):
        """Passing datasets builds a Catalog without touching the YAML."""
        product = Catalog().get_product("mswep")
        catalog = Catalog(datasets={"only": product})
        assert catalog.products() == ["only"]

    def test_available_datasets_is_sorted(self):
        """The index mirrors the product map, sorted."""
        catalog = Catalog()
        assert catalog.available_datasets == sorted(catalog.datasets)

    def test_get_catalog_returns_the_product_map(self):
        """The abstract contract returns the same mapping as `datasets`."""
        catalog = Catalog()
        assert catalog.get_catalog() is catalog.datasets

    def test_load_is_cached_by_mtime(self, tmp_path):
        """A second load of an unchanged file reuses the parsed payload."""
        path = _write(tmp_path, MINIMAL)
        first = Catalog.load(path)
        second = Catalog.load(path)
        assert first.datasets["mswep"] is second.datasets["mswep"]

    def test_clear_cache_forces_a_reparse(self, tmp_path):
        """Clearing the cache drops the memoised rows."""
        path = _write(tmp_path, MINIMAL)
        first = Catalog.load(path)
        clear_catalog_cache()
        second = Catalog.load(path)
        assert first.datasets["mswep"] is not second.datasets["mswep"]
