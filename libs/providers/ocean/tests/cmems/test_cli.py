"""Tests for the CMEMS catalog-tooling handlers (`earthlens.cmems.cli`).

Moved out of core's CLI test suite when the CMEMS refresh/probe/deep-probe
handlers moved into this distribution (issue #863).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import earthlens.cmems.cli as cmems_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.curate import probe_dataset
from earthlens.cli.refresh import refresh_one

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the cmems backend."""
    return next(b for b in list_backends() if b.provider == "cmems")


class TestRefresher:
    """Tests for the CMEMS (copernicusmarine) lister."""

    def test_walks_products_and_datasets(self, monkeypatch):
        """cmems refresh flattens products[].datasets[].dataset_id."""
        fake = SimpleNamespace(
            products=[
                SimpleNamespace(datasets=[SimpleNamespace(dataset_id="a")]),
                SimpleNamespace(
                    datasets=[
                        SimpleNamespace(dataset_id="b"),
                        SimpleNamespace(dataset_id="c"),
                    ]
                ),
            ]
        )
        monkeypatch.setattr(cmems_cli, "_describe", lambda: fake)
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "cmems refresh ran"
        assert outcome.live_count == 3, "a/b/c across two products"

    def test_grouped_flattens(self, monkeypatch):
        """cmems grouped flattens products[].datasets[].dataset_id."""
        cat = SimpleNamespace(
            products=[
                SimpleNamespace(
                    datasets=[
                        SimpleNamespace(dataset_id="a"),
                        SimpleNamespace(dataset_id="b"),
                    ]
                )
            ]
        )
        monkeypatch.setattr(cmems_cli, "_describe", lambda: cat)
        assert cmems_cli.refresher(None) == {"cmems": ["a", "b"]}

    def test_describe_delegates(self, monkeypatch):
        """_describe calls the SDK describe and returns the catalogue."""
        fake = types.ModuleType("copernicusmarine")
        fake.describe = lambda disable_progress_bar=None: "CAT"
        monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
        assert cmems_cli._describe() == "CAT"


class TestProber:
    """Tests for the CMEMS variable prober (SDK describe)."""

    def test_walks_nested_variables(self, monkeypatch):
        """cmems probe flattens the nested products→…→variables to a schema."""
        variable = SimpleNamespace(
            short_name="thetao", standard_name="sea_water_temp", units="degC"
        )
        service = SimpleNamespace(variables=[variable])
        part = SimpleNamespace(services=[service])
        version = SimpleNamespace(parts=[part])
        entry = SimpleNamespace(versions=[version])
        catalogue = SimpleNamespace(products=[SimpleNamespace(datasets=[entry])])
        monkeypatch.setattr(
            cmems_cli, "_describe_dataset", lambda dataset_id: catalogue
        )
        result = probe_dataset(_info(), "cmems_mod_glo_phy")
        assert result.status == "ok", "cmems probe ran"
        assert result.assets["thetao"]["units"] == "degC", "variable units parsed"

    def test_describe_dataset_delegates(self, monkeypatch):
        """_describe_dataset calls the SDK describe and returns it."""
        fake = types.ModuleType("copernicusmarine")
        fake.describe = lambda dataset_id=None, disable_progress_bar=None: "CAT"
        monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
        assert cmems_cli._describe_dataset("x") == "CAT"


class TestDeepProber:
    """Tests for the credentialed `--deep` NetCDF sampler (SDK faked)."""

    def test_deep_reads_netcdf_vars(self, monkeypatch):
        """cmems --deep reads the real NetCDF variable schema."""
        monkeypatch.setattr(
            cmems_cli,
            "_deep_sample",
            lambda dsid: {"thetao": {"units": "degC", "dtype": "float32"}},
        )
        result = probe_dataset(_info(), "cmems_mod_glo_phy", deep=True)
        assert result.status == "ok", "cmems deep probe ran"
        assert result.assets["thetao"]["units"] == "degC", "real var units read"

    def test_deep_sample_reads_data_vars(self, monkeypatch):
        """_deep_sample reads each NetCDF data_var's attrs + dtype."""
        fake = types.ModuleType("copernicusmarine")

        class _Var:
            attrs = {
                "units": "degC",
                "standard_name": "sea_water_temp",
                "long_name": "Temp",
            }
            dtype = "float32"

        fake.open_dataset = lambda dataset_id=None: types.SimpleNamespace(
            data_vars={"thetao": _Var()}
        )
        monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
        out = cmems_cli._deep_sample("x")
        assert out["thetao"]["units"] == "degC", "units read"
        assert out["thetao"]["dtype"] == "float32", "dtype stringified"
