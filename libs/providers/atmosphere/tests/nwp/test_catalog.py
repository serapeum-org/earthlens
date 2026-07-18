"""Unit tests for the NWP catalog loader."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.nwp import Catalog, NWPModel
from earthlens.nwp import catalog as catalog_mod
from earthlens.nwp.catalog import KNOWN_BACKENDS, clear_catalog_cache

pytestmark = [pytest.mark.nwp, pytest.mark.unit]

_CATALOG = """
datasets:
  gfs:
    provider: noaa-nodd
    model_family: gfs
    cycles_utc: [0, 6, 12, 18]
    horizon_h: 384
    backend: herbie
    mirrors: [aws, google]
    bands:
      temperature_2m: ":TMP:2 m above ground:"
  icon-global:
    provider: dwd-opendata
    backend: direct-https
    horizon_h: 180
    url_template: "https://x/{var}.bz2"
    bands:
      temperature_2m: T_2M
"""


def _write_catalog(tmp_path, body=_CATALOG):
    """Write a catalog YAML under tmp_path and return its path."""
    path = tmp_path / "nwp_data_catalog.yaml"
    path.write_text(body)
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts and ends with a clean parse cache."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


class TestCatalogLoad:
    """Tests for Catalog construction and the bundled / on-disk load."""

    def test_bundled_catalog_has_the_mvp_and_expanded_models(self):
        """The shipped catalog resolves the MVP models plus the expanded set."""
        models = set(Catalog().datasets)
        mvp = {"gfs", "gefs", "hrrr", "ifs-hres", "icon-global"}
        expanded = {
            "rap",
            "nam",
            "nbm",
            "rrfs",
            "gdps",
            "rdps",
            "hrdps",
            "icon-eu",
            "icon-d2",
            "ens",
            "aifs",
        }
        assert mvp <= models and expanded <= models, sorted(models)

    def test_load_from_disk(self, tmp_path, monkeypatch):
        """A monkey-patched CATALOG_PATH is parsed into typed rows."""
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path))
        cat = Catalog()
        assert set(cat.datasets) == {"gfs", "icon-global"}, cat.datasets

    def test_injected_datasets_skip_disk(self):
        """Passing datasets= bypasses the disk read entirely."""
        cat = Catalog(datasets={"x": NWPModel(provider="p")})
        assert list(cat.datasets) == ["x"], cat.datasets

    def test_second_load_hits_cache(self):
        """A second construction reuses the cached parse for the same file."""
        first = sorted(Catalog().datasets)
        second = sorted(Catalog().datasets)
        assert first == second and "gfs" in first and "aifs" in first

    def test_empty_datasets_block_raises(self, tmp_path, monkeypatch):
        """A YAML with no datasets: block raises ValueError."""
        monkeypatch.setattr(
            catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path, "datasets:\n")
        )
        with pytest.raises(ValueError, match="empty 'datasets:'"):
            Catalog()

    def test_invalid_row_raises(self, tmp_path, monkeypatch):
        """A row with an unknown field surfaces a validation ValueError."""
        body = "datasets:\n  bad:\n    provider: p\n    nope: 1\n"
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", _write_catalog(tmp_path, body))
        with pytest.raises(ValueError, match="failed validation"):
            Catalog()


class TestCatalogResolve:
    """Tests for get_model / resolve / get_catalog and did-you-mean."""

    def test_get_model(self):
        """get_model returns the typed row for a known key."""
        model = Catalog().get_model("gfs")
        assert model.backend == "herbie"
        assert model.bands["temperature_2m"] == ":TMP:2 m above ground:"

    def test_models_expose_extended_surface_bands(self):
        """Forecast models carry the extended surface-field set."""
        cat = Catalog()
        extended = {
            "relative_humidity_2m",
            "wind_gust",
            "surface_pressure",
            "total_cloud_cover",
            "cape",
        }
        for key in ("gfs", "ifs-hres", "icon-eu", "arpege-world"):
            assert extended <= set(cat.get_model(key).bands), key
        # the GRIB2 gust selector is the standard surface GUST regex
        assert cat.get_model("gfs").bands["wind_gust"] == ":GUST:surface:"
        # DWD CAPE token validated live for icon-eu
        assert cat.get_model("icon-eu").bands["cape"] == "CAPE_ML"

    def test_resolve_is_get_model(self):
        """resolve is an alias returning the same row as get_model."""
        cat = Catalog()
        assert cat.resolve("hrrr") is cat.get_model("hrrr")

    def test_get_catalog_returns_datasets(self):
        """get_catalog returns the same mapping as .datasets."""
        cat = Catalog()
        assert cat.get_catalog() is cat.datasets

    def test_unknown_key_did_you_mean(self):
        """An unknown key raises ValueError with a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'gfs'"):
            Catalog().get_model("gffs")


class TestNWPModel:
    """Tests for the NWPModel row model."""

    def test_defaults(self):
        """Optional fields fall back to documented defaults."""
        model = NWPModel(provider="p")
        assert model.backend == "herbie"
        assert model.format == "grib2"
        assert model.idx is True
        assert model.cycles_utc == [] and model.bands == {}

    def test_extra_forbidden(self):
        """An unexpected field is rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            NWPModel(provider="p", bogus=1)

    def test_known_backends_membership(self):
        """Every backend literal is listed in KNOWN_BACKENDS."""
        for backend in ("herbie", "ecmwf-opendata", "direct-https", "direct-boto3"):
            assert backend in KNOWN_BACKENDS

    def test_license_defaults_to_none(self):
        """An ad-hoc row without a `license` is `None` — never inferred."""
        assert NWPModel(provider="noaa-nodd").license is None

    def test_license_round_trips(self):
        """A populated `license` survives construction."""
        assert NWPModel(provider="noaa-nodd", license="PD-US-GOV").license == "PD-US-GOV"


class TestBundledTitles:
    """Tests that every shipped row carries a human-readable title + description."""

    def test_every_row_has_a_title(self):
        """No bundled NWP model row is missing its `title`."""
        from earthlens.nwp import Catalog

        missing = [k for k, m in Catalog().datasets.items() if not m.title]
        assert missing == [], f"rows without title: {missing}"

    def test_every_row_has_a_description(self):
        """No bundled NWP model row is missing its `description`."""
        from earthlens.nwp import Catalog

        missing = [k for k, m in Catalog().datasets.items() if not m.description]
        assert missing == [], f"rows without description: {missing}"

    def test_title_and_description_default_to_none(self):
        """An ad-hoc row leaves title / description as None (never inferred)."""
        model = NWPModel(provider="noaa-nodd")
        assert model.title is None and model.description is None

    def test_cli_title_resolves_from_the_row(self):
        """The federated CLI title column reads the row's `title`."""
        from earthlens.cli.adapter import record_title
        from earthlens.nwp import Catalog

        assert record_title(Catalog().datasets["gfs"]) == "NOAA GFS (Global Forecast System)"


class TestBundledLicenses:
    """Tests for C2: every shipped row carries its provider's license."""

    _EXPECTED = {
        "noaa-nodd": "PD-US-GOV",
        "ecmwf-opendata": "CC-BY-4.0",
        "dwd-opendata": "CC-BY-4.0",
        "meteofrance": "Etalab-2.0",
        "eccc-msc": "OGL-Canada-2.0",
    }

    def test_every_shipped_row_has_a_license(self):
        """No bundled NWP model row is missing its `license`."""
        from earthlens.nwp import Catalog

        missing = [k for k, m in Catalog().datasets.items() if m.license is None]
        assert missing == [], f"rows without license: {missing}"

    def test_license_matches_provider(self):
        """Every row's `license` matches the curated per-provider value."""
        from earthlens.nwp import Catalog

        wrong = [
            (k, m.provider, m.license, self._EXPECTED[m.provider])
            for k, m in Catalog().datasets.items()
            if m.license != self._EXPECTED.get(m.provider)
        ]
        assert wrong == [], f"license mismatch: {wrong}"


class TestClearCache:
    """Tests for clear_catalog_cache."""

    def test_clear_forces_reparse(self, tmp_path, monkeypatch):
        """Clearing the cache picks up an on-disk edit between loads."""
        path = _write_catalog(tmp_path)
        monkeypatch.setattr(catalog_mod, "CATALOG_PATH", path)
        assert set(Catalog().datasets) == {"gfs", "icon-global"}
        path.write_text("datasets:\n  only:\n    provider: p\n")
        clear_catalog_cache()
        assert set(Catalog().datasets) == {"only"}


def test_load_catalog_data_missing_file_raises(tmp_path):
    """A non-existent catalog path raises FileNotFoundError from the loader."""
    from earthlens.nwp.catalog import _load_catalog_data

    with pytest.raises(FileNotFoundError):
        _load_catalog_data(tmp_path / "absent.yaml")


class TestBundledRetention:
    """Tests for C3: every short-retention row carries the published window."""

    def test_dwd_rows_one_day(self):
        """Every dwd-opendata row carries `retention_days=1`."""
        from earthlens.nwp import Catalog

        for k, m in Catalog().datasets.items():
            if m.provider == "dwd-opendata":
                assert m.retention_days == 1, f"{k!r} retention={m.retention_days}"

    def test_meteofrance_rows_fourteen_days(self):
        """Every meteofrance row carries `retention_days=14`."""
        from earthlens.nwp import Catalog

        for k, m in Catalog().datasets.items():
            if m.provider == "meteofrance":
                assert m.retention_days == 14, f"{k!r} retention={m.retention_days}"

    def test_archival_providers_have_no_retention(self):
        """NOAA NODD + ECMWF Open Data rows leave retention_days as None."""
        from earthlens.nwp import Catalog

        for k, m in Catalog().datasets.items():
            if m.provider in ("noaa-nodd", "ecmwf-opendata"):
                assert m.retention_days is None, f"{k!r} retention={m.retention_days}"

    def test_eccc_rows_carry_datamart_retention(self):
        """ECCC rows carry the Datamart rolling windows (30 d deterministic, 14 d ensemble)."""
        from earthlens.nwp import Catalog

        expected = {"gdps": 30, "rdps": 30, "hrdps": 30, "geps": 14}
        for k, days in expected.items():
            assert Catalog().datasets[k].retention_days == days
