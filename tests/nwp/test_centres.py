"""Unit tests for the NWP per-centre fetchers and dispatch."""

from __future__ import annotations

import datetime as dt
import sys
import types

import pytest

from earthlens.nwp.catalog import NWPModel
from earthlens.nwp.centres import resolve_centre
from earthlens.nwp.centres.base import CENTRE_REGISTRY, _NWPCentre
from earthlens.nwp.centres.dwd import DWDCentre
from earthlens.nwp.centres.ecmwf import ECMWFCentre, _source_for
from earthlens.nwp.centres.noaa import NOAACentre, _import_herbie, _priority

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


def _gfs(**overrides) -> NWPModel:
    """Build a Herbie gfs row, overriding any field."""
    base = dict(
        provider="noaa-nodd",
        model_family="gfs",
        cycles_utc=[0, 12],
        horizon_h=48,
        backend="herbie",
        mirrors=["aws", "google", "azure"],
        bands={"temperature_2m": ":TMP:2 m above ground:", "precipitation_acc": ":APCP:surface:"},
    )
    base.update(overrides)
    return NWPModel(**base)


class TestResolveCentre:
    """Tests for the centre dispatch in centres.base."""

    def test_registry_keys(self):
        """The registry maps the SDK + direct backends to centre classes."""
        assert set(CENTRE_REGISTRY) == {"herbie", "ecmwf-opendata", "direct-https"}

    @pytest.mark.parametrize(
        "backend, cls",
        [("herbie", NOAACentre), ("ecmwf-opendata", ECMWFCentre), ("direct-https", DWDCentre)],
    )
    def test_resolve_returns_bound_centre(self, backend, cls, tmp_path):
        """resolve_centre imports and constructs the registered centre."""
        centre = resolve_centre(backend, tmp_path)
        assert isinstance(centre, cls) and isinstance(centre, _NWPCentre)
        assert centre.save_dir == tmp_path

    def test_unknown_backend_raises(self, tmp_path):
        """An unregistered backend raises a listing ValueError."""
        with pytest.raises(ValueError, match="no NWP centre registered"):
            resolve_centre("direct-boto3", tmp_path)


class TestNOAACentre:
    """Tests for the Herbie-backed NOAA centre."""

    def test_priority_auto_uses_catalog_order(self):
        """mirror='auto' maps the model's mirrors to Herbie source keys."""
        assert _priority("auto", _gfs()) == ["aws", "google", "azure"]

    def test_priority_explicit_gcp_maps_to_google(self):
        """An explicit gcp mirror maps to Herbie's 'google' key."""
        assert _priority("gcp", _gfs()) == ["google"]

    def test_priority_auto_empty_is_none(self):
        """A model with no mirrors yields None (Herbie's own default)."""
        assert _priority("auto", _gfs(mirrors=[])) is None

    def test_fetch_one_builds_search_and_path(self, fake_herbie, tmp_path):
        """fetch_one joins the params' regexes and returns Herbie's path."""
        centre = NOAACentre(tmp_path)
        out = centre.fetch_one(
            _gfs(), dt.datetime(2024, 6, 1, 0), 6, ["temperature_2m", "precipitation_acc"], "auto"
        )
        handle = fake_herbie.instances[-1]
        assert handle.download_calls == [":TMP:2 m above ground:|:APCP:surface:"]
        assert handle.kwargs["fxx"] == 6 and handle.kwargs["priority"] == ["aws", "google", "azure"]
        assert "product" not in handle.kwargs
        assert str(out).endswith("subset_gfs_f6.grib2")

    def test_fetch_one_passes_product_when_set(self, fake_herbie, tmp_path):
        """A model with a product (HRRR) forwards product= to Herbie."""
        NOAACentre(tmp_path).fetch_one(
            _gfs(model_family="hrrr", product="wrfsfcf"),
            dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws",
        )
        assert fake_herbie.instances[-1].kwargs["product"] == "wrfsfcf"

    def test_import_herbie_success(self, fake_herbie):
        """_import_herbie returns the Herbie class when the SDK is present."""
        assert _import_herbie() is fake_herbie

    def test_import_herbie_missing_raises_friendly(self, monkeypatch):
        """A missing herbie module raises an earthlens[nwp] ImportError."""
        monkeypatch.setitem(sys.modules, "herbie", None)
        with pytest.raises(ImportError, match=r"earthlens\[nwp\]"):
            _import_herbie()

    def test_import_herbie_eccodes_runtimeerror_becomes_importerror(self, monkeypatch):
        """A cfgrib/eccodes RuntimeError is rewritten as an ImportError."""
        module = types.ModuleType("herbie")

        def _raise(name):
            raise RuntimeError("Cannot find the ecCodes library")

        module.__getattr__ = _raise
        monkeypatch.setitem(sys.modules, "herbie", module)
        with pytest.raises(ImportError, match="eccodes"):
            _import_herbie()


class TestECMWFCentre:
    """Tests for the ecmwf-opendata-backed IFS centre."""

    def _ifs(self) -> NWPModel:
        """Build an IFS HRES row with ecmwf-opendata param tokens."""
        return NWPModel(
            provider="ecmwf-opendata",
            model_family="ifs",
            cycles_utc=[0, 12],
            horizon_h=240,
            backend="ecmwf-opendata",
            mirrors=["aws", "azure", "ecmwf"],
            bands={"temperature_2m": "2t", "precipitation_acc": "tp"},
        )

    def test_source_auto_picks_first_known_mirror(self):
        """mirror='auto' selects the first catalog mirror with a known source."""
        assert _source_for("auto", self._ifs()) == "aws"

    def test_source_gcp_falls_back_to_ecmwf(self):
        """gcp is unavailable on ecmwf-opendata and falls back to 'ecmwf'."""
        assert _source_for("gcp", self._ifs()) == "ecmwf"

    def test_source_auto_no_known_mirror_falls_back(self):
        """mirror='auto' with no usable catalog mirror falls back to 'ecmwf'."""
        model = NWPModel(provider="ecmwf-opendata", backend="ecmwf-opendata", mirrors=["nomads"])
        assert _source_for("auto", model) == "ecmwf"

    def test_fetch_one_retrieves_param_tokens(self, fake_ecmwf_client, tmp_path):
        """fetch_one calls retrieve with the param tokens and a target path."""
        out = ECMWFCentre(tmp_path).fetch_one(
            self._ifs(), dt.datetime(2024, 6, 1, 12), 24, ["temperature_2m", "precipitation_acc"], "azure"
        )
        client = fake_ecmwf_client.instances[-1]
        assert client.source == "azure"
        call = client.retrieve_calls[-1]
        assert call["param"] == ["2t", "tp"] and call["step"] == 24 and call["time"] == 12
        assert call["date"] == "2024-06-01"
        assert out.exists() and out.name == "ifs_2024060112_f024.grib2"

    def test_import_client_missing_raises_friendly(self, monkeypatch):
        """A missing ecmwf.opendata raises an earthlens[nwp] ImportError."""
        from earthlens.nwp.centres.ecmwf import _import_client

        monkeypatch.setitem(sys.modules, "ecmwf.opendata", None)
        with pytest.raises(ImportError, match=r"earthlens\[nwp\]"):
            _import_client()


class TestDWDCentre:
    """Tests for the direct-HTTPS DWD ICON centre."""

    def _icon(self, **overrides) -> NWPModel:
        """Build a direct-HTTPS ICON row, overriding any field."""
        base = dict(
            provider="dwd-opendata",
            model_family="icon",
            cycles_utc=[0, 12],
            horizon_h=180,
            idx=False,
            backend="direct-https",
            url_template="https://x/{cycle:%H}/{var_lc}/icon_{date:%Y%m%d%H}_{step:03d}_{var}.grib2.bz2",
            bands={"temperature_2m": "T_2M", "precipitation_acc": "TOT_PREC"},
        )
        base.update(overrides)
        return NWPModel(**base)

    def test_fetch_one_builds_urls_and_concatenates(self, fake_requests, tmp_path):
        """fetch_one builds per-variable URLs and concatenates decompressed messages."""
        out = DWDCentre(tmp_path).fetch_one(
            self._icon(), dt.datetime(2024, 6, 1, 0), 3, ["temperature_2m", "precipitation_acc"], "auto"
        )
        assert fake_requests["urls"] == [
            "https://x/00/t_2m/icon_2024060100_003_T_2M.grib2.bz2",
            "https://x/00/tot_prec/icon_2024060100_003_TOT_PREC.grib2.bz2",
        ]
        assert out.read_bytes() == b"<T_2M><TOT_PREC>"
        assert out.name == "icon_2024060100_f003.grib2"

    def test_fetch_one_without_template_raises(self, tmp_path):
        """A model lacking a url_template raises ValueError."""
        model = self._icon(url_template=None)
        with pytest.raises(ValueError, match="url_template"):
            DWDCentre(tmp_path).fetch_one(
                model, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
            )
