"""Unit tests for the NWP per-centre fetchers and dispatch."""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import types

import pytest

from earthlens.nwp.catalog import NWPModel
from earthlens.nwp.centres import resolve_centre
from earthlens.nwp.centres.base import CENTRE_REGISTRY, _NWPCentre
from earthlens.nwp.centres.dwd import DWDCentre
from earthlens.nwp.centres.ecmwf import ECMWFCentre, _group_params, _source_for
from earthlens.nwp.centres.meteofrance import MeteoFranceCentre
from earthlens.nwp.centres.meteofrance_api import MeteoFranceAPICentre, resolve_api_key
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
        bands={
            "temperature_2m": ":TMP:2 m above ground:",
            "precipitation_acc": ":APCP:surface:",
        },
    )
    base.update(overrides)
    return NWPModel(**base)


class TestResolveCentre:
    """Tests for the centre dispatch in centres.base."""

    def test_registry_keys(self):
        """The registry maps the SDK + direct backends to centre classes."""
        assert set(CENTRE_REGISTRY) == {
            "herbie",
            "ecmwf-opendata",
            "direct-https",
            "direct-boto3",
            "meteofrance-api",
            "eccc-msc",
        }

    @pytest.mark.parametrize(
        "backend, cls",
        [
            ("herbie", NOAACentre),
            ("ecmwf-opendata", ECMWFCentre),
            ("direct-https", DWDCentre),
            ("direct-boto3", MeteoFranceCentre),
            ("meteofrance-api", MeteoFranceAPICentre),
        ],
    )
    def test_resolve_returns_bound_centre(self, backend, cls, tmp_path):
        """resolve_centre imports and constructs the registered centre."""
        centre = resolve_centre(backend, tmp_path)
        assert isinstance(centre, cls) and isinstance(centre, _NWPCentre)
        assert centre.save_dir == tmp_path

    def test_unknown_backend_raises(self, tmp_path):
        """An unregistered backend raises a listing ValueError."""
        with pytest.raises(ValueError, match="no NWP centre registered"):
            resolve_centre("direct-ftp", tmp_path)


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
            _gfs(),
            dt.datetime(2024, 6, 1, 0),
            6,
            ["temperature_2m", "precipitation_acc"],
            "auto",
        )
        handle = fake_herbie.instances[-1]
        assert handle.download_calls == [":TMP:2 m above ground:|:APCP:surface:"]
        assert handle.kwargs["fxx"] == 6 and handle.kwargs["priority"] == [
            "aws",
            "google",
            "azure",
        ]
        assert "product" not in handle.kwargs
        assert str(out).endswith("subset_gfs_f6.grib2")

    def test_fetch_one_whole_downloads_full_file(self, fake_herbie, tmp_path):
        """whole=True calls Herbie's download with no search selector (full file)."""
        NOAACentre(tmp_path).fetch_one(
            _gfs(),
            dt.datetime(2024, 6, 1, 0),
            6,
            ["temperature_2m", "precipitation_acc"],
            "auto",
            whole=True,
        )
        # download(None) is Herbie's full-file contract; the .idx search is dropped.
        assert fake_herbie.instances[-1].download_calls == [None]

    def test_fetch_one_subset_is_default_and_unchanged(self, fake_herbie, tmp_path):
        """whole defaults to False and the subset call passes the .idx search (regression)."""
        NOAACentre(tmp_path).fetch_one(
            _gfs(),
            dt.datetime(2024, 6, 1, 0),
            6,
            ["temperature_2m"],
            "auto",
        )
        assert fake_herbie.instances[-1].download_calls == [":TMP:2 m above ground:"]

    def test_fetch_one_passes_product_when_set(self, fake_herbie, tmp_path):
        """A model with a product (HRRR) forwards product= to Herbie."""
        NOAACentre(tmp_path).fetch_one(
            _gfs(model_family="hrrr", product="wrfsfcf"),
            dt.datetime(2024, 6, 1, 0),
            0,
            ["temperature_2m"],
            "aws",
        )
        assert fake_herbie.instances[-1].kwargs["product"] == "wrfsfcf"

    def test_fetch_one_splats_request_options(self, fake_herbie, tmp_path):
        """request_options (e.g. domain for HiResW/HREF) pass through to Herbie."""
        model = _gfs(
            model_family="hiresw",
            product="arw_2p5km",
            request_options={"domain": "conus"},
        )
        NOAACentre(tmp_path).fetch_one(
            model, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws"
        )
        assert fake_herbie.instances[-1].kwargs["domain"] == "conus"

    def test_fetch_one_member_to_herbie(self, fake_herbie, tmp_path):
        """A numeric member becomes an int Herbie member=; 'mean' stays a string."""
        NOAACentre(tmp_path).fetch_one(
            _gfs(model_family="gefs"),
            dt.datetime(2024, 6, 1, 0),
            0,
            ["temperature_2m"],
            "aws",
            "5",
        )
        assert fake_herbie.instances[-1].kwargs["member"] == 5
        NOAACentre(tmp_path).fetch_one(
            _gfs(model_family="gefs"),
            dt.datetime(2024, 6, 1, 0),
            0,
            ["temperature_2m"],
            "aws",
            "mean",
        )
        assert fake_herbie.instances[-1].kwargs["member"] == "mean"

    def test_fetch_one_threads_show_progress_to_verbose(self, fake_herbie, tmp_path):
        """show_progress is forwarded to Herbie's verbose= (L4)."""
        centre = NOAACentre(tmp_path)
        centre.show_progress = False
        centre.fetch_one(
            _gfs(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws"
        )
        assert fake_herbie.instances[-1].kwargs["verbose"] is False

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

    def test_group_params_splits_surface_and_pressure(self):
        """_group_params yields the surface group first, then levels ascending."""
        groups = _group_params(["2t", "tp", "t@850", "u@850", "gh@500"])
        assert groups == [(None, ["2t", "tp"]), ("500", ["gh"]), ("850", ["t", "u"])]

    def test_group_params_pressure_only(self):
        """With no surface tokens, only pressure-level groups are returned."""
        assert _group_params(["t@850", "u@850"]) == [("850", ["t", "u"])]

    def test_fetch_one_failure_leaves_no_partial_file(self, monkeypatch, tmp_path):
        """A retrieve failure unlinks the partial output and re-raises."""
        import sys
        import types

        class _Client:
            def __init__(self, *a, **k):
                pass

            def retrieve(self, **kwargs):
                raise RuntimeError("retrieve failed")

        pkg = types.ModuleType("ecmwf")
        sub = types.ModuleType("ecmwf.opendata")
        sub.Client = _Client
        pkg.opendata = sub
        monkeypatch.setitem(sys.modules, "ecmwf", pkg)
        monkeypatch.setitem(sys.modules, "ecmwf.opendata", sub)
        with pytest.raises(RuntimeError, match="retrieve failed"):
            ECMWFCentre(tmp_path).fetch_one(
                self._ifs(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws"
            )
        assert list(tmp_path.iterdir()) == [], "no partial file should remain"

    def test_fetch_one_pressure_level_groups_into_retrieves(
        self, fake_ecmwf_client, tmp_path
    ):
        """A surface+pressure request issues one retrieve per level type/level."""
        model = NWPModel(
            provider="ecmwf-opendata",
            model_family="ifs",
            cycles_utc=[0],
            horizon_h=240,
            backend="ecmwf-opendata",
            mirrors=["aws"],
            bands={
                "temperature_2m": "2t",
                "temperature_850hPa": "t@850",
                "geopotential_height_500hPa": "gh@500",
            },
        )
        out = ECMWFCentre(tmp_path).fetch_one(
            model,
            dt.datetime(2024, 6, 1, 0),
            0,
            ["temperature_2m", "temperature_850hPa", "geopotential_height_500hPa"],
            "aws",
        )
        calls = fake_ecmwf_client.instances[-1].retrieve_calls
        assert len(calls) == 3
        surface = [c for c in calls if "levtype" not in c]
        pressure = [c for c in calls if c.get("levtype") == "pl"]
        assert surface[0]["param"] == ["2t"]
        assert {c["levelist"] for c in pressure} == {"500", "850"}
        assert out.exists() and out.name == "ifs_2024060100_f000.grib2"

    def test_source_auto_picks_first_known_mirror(self):
        """mirror='auto' selects the first catalog mirror with a known source."""
        assert _source_for("auto", self._ifs()) == "aws"

    def test_source_gcp_falls_back_to_ecmwf(self):
        """gcp is unavailable on ecmwf-opendata and falls back to 'ecmwf'."""
        assert _source_for("gcp", self._ifs()) == "ecmwf"

    def test_source_auto_no_known_mirror_falls_back(self):
        """mirror='auto' with no usable catalog mirror falls back to 'ecmwf'."""
        model = NWPModel(
            provider="ecmwf-opendata", backend="ecmwf-opendata", mirrors=["nomads"]
        )
        assert _source_for("auto", model) == "ecmwf"

    def test_fetch_one_retrieves_param_tokens(self, fake_ecmwf_client, tmp_path):
        """fetch_one calls retrieve with the param tokens and a target path."""
        out = ECMWFCentre(tmp_path).fetch_one(
            self._ifs(),
            dt.datetime(2024, 6, 1, 12),
            24,
            ["temperature_2m", "precipitation_acc"],
            "azure",
        )
        client = fake_ecmwf_client.instances[-1]
        assert client.source == "azure"
        assert client.kwargs.get("model") == "ifs"
        call = client.retrieve_calls[-1]
        assert (
            call["param"] == ["2t", "tp"] and call["step"] == 24 and call["time"] == 12
        )
        assert call["date"] == "2024-06-01"
        assert "stream" not in call
        assert out.exists() and out.name == "ifs_2024060112_f024.grib2"

    def test_whole_is_a_noop(self, fake_ecmwf_client, tmp_path):
        """whole=True retrieves identically to whole=False (param-addressed, no subset)."""
        args = (self._ifs(), dt.datetime(2024, 6, 1, 12), 24, ["temperature_2m"], "aws")
        ECMWFCentre(tmp_path).fetch_one(*args)
        subset_call = dict(fake_ecmwf_client.instances[-1].retrieve_calls[-1])
        ECMWFCentre(tmp_path).fetch_one(*args, whole=True)
        whole_call = dict(fake_ecmwf_client.instances[-1].retrieve_calls[-1])
        subset_call.pop("target", None)
        whole_call.pop("target", None)
        assert whole_call == subset_call

    def test_fetch_one_perturbed_member_sets_pf_and_number(
        self, fake_ecmwf_client, tmp_path
    ):
        """A numeric ENS member selects type=pf + number=<member>."""
        ens = NWPModel(
            provider="ecmwf-opendata",
            model_family="ens",
            cycles_utc=[0],
            horizon_h=360,
            backend="ecmwf-opendata",
            mirrors=["aws"],
            bands={"temperature_2m": "2t"},
            request_options={"stream": "enfo", "type": "cf"},
            members=["control", "1", "2"],
        )
        out = ECMWFCentre(tmp_path).fetch_one(
            ens, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws", "3"
        )
        call = fake_ecmwf_client.instances[-1].retrieve_calls[-1]
        assert call["type"] == "pf" and call["number"] == 3
        assert out.name == "ens_2024060100_f000_m3.grib2"

    def test_fetch_one_control_member_keeps_cf(self, fake_ecmwf_client, tmp_path):
        """The 'control' member keeps the row's configured type (cf), no number."""
        ens = NWPModel(
            provider="ecmwf-opendata",
            model_family="ens",
            cycles_utc=[0],
            horizon_h=360,
            backend="ecmwf-opendata",
            mirrors=["aws"],
            bands={"temperature_2m": "2t"},
            request_options={"stream": "enfo", "type": "cf"},
            members=["control", "1"],
        )
        ECMWFCentre(tmp_path).fetch_one(
            ens, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws", "control"
        )
        call = fake_ecmwf_client.instances[-1].retrieve_calls[-1]
        assert call["type"] == "cf" and "number" not in call

    def test_fetch_one_aifs_uses_model_and_family(self, fake_ecmwf_client, tmp_path):
        """An AIFS row sets Client(model='aifs-single') and names the file by family."""
        aifs = NWPModel(
            provider="ecmwf-opendata",
            model_family="aifs",
            cycles_utc=[0],
            horizon_h=360,
            backend="ecmwf-opendata",
            mirrors=["aws"],
            bands={"temperature_2m": "2t"},
            request_options={"ecmwf_model": "aifs-single"},
        )
        out = ECMWFCentre(tmp_path).fetch_one(
            aifs, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws"
        )
        assert fake_ecmwf_client.instances[-1].kwargs.get("model") == "aifs-single"
        assert out.name == "aifs_2024060100_f000.grib2"

    def test_fetch_one_ens_passes_stream_and_type(self, fake_ecmwf_client, tmp_path):
        """An ENS row forwards stream/type from request_options to retrieve."""
        ens = NWPModel(
            provider="ecmwf-opendata",
            model_family="ens",
            cycles_utc=[0],
            horizon_h=360,
            backend="ecmwf-opendata",
            mirrors=["aws"],
            bands={"temperature_2m": "2t"},
            request_options={"stream": "enfo", "type": "cf"},
        )
        ECMWFCentre(tmp_path).fetch_one(
            ens, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "aws"
        )
        call = fake_ecmwf_client.instances[-1].retrieve_calls[-1]
        assert call["stream"] == "enfo" and call["type"] == "cf"

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
            self._icon(),
            dt.datetime(2024, 6, 1, 0),
            3,
            ["temperature_2m", "precipitation_acc"],
            "auto",
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

    def test_band_url_pressure_level_uses_pl_template(self, tmp_path):
        """A VAR@level band builds the pressure-level URL with the level filled in."""
        model = self._icon(
            request_options={
                "pl_url_template": "https://x/{var_lc}/pl_{date:%Y%m%d%H}_{step:03d}_{level}_{var}.bz2"
            },
            bands={"temperature_850hPa": "T@850"},
        )
        url = DWDCentre._band_url(
            model, "temperature_850hPa", dt.datetime(2024, 6, 1, 0), 3
        )
        assert url == "https://x/t/pl_2024060100_003_850_T.bz2"

    def test_band_url_pressure_level_without_pl_template_raises(self, tmp_path):
        """A pressure-level band with no pl_url_template is rejected."""
        model = self._icon(request_options={}, bands={"temperature_850hPa": "T@850"})
        with pytest.raises(ValueError, match="pl_url_template"):
            DWDCentre._band_url(
                model, "temperature_850hPa", dt.datetime(2024, 6, 1, 0), 0
            )

    def test_fetch_one_failure_leaves_no_partial_file(self, tmp_path, monkeypatch):
        """A failure on a later variable leaves no truncated .grib2 (L1)."""
        import sys
        import types

        def failing_get(url, timeout=None, **kwargs):
            raise RuntimeError("network down")

        module = types.ModuleType("requests")
        module.get = failing_get
        monkeypatch.setitem(sys.modules, "requests", module)
        with pytest.raises(RuntimeError, match="network down"):
            DWDCentre(tmp_path).fetch_one(
                self._icon(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
            )
        assert list(tmp_path.iterdir()) == [], "no partial file should remain"


class TestMeteoFranceCentre:
    """Tests for the direct-boto3 Météo-France centre."""

    def _mf(self, **overrides) -> NWPModel:
        """Build a direct-boto3 ARPEGE row, overriding any field."""
        base = dict(
            provider="meteofrance",
            model_family="arpege",
            cycles_utc=[0, 12],
            horizon_h=102,
            backend="direct-boto3",
            bands={"temperature_2m": "T2M", "precipitation_acc": "TP"},
            request_options={
                "bucket": "mf-nwp-models",
                "key_template": "arpege-world/{date:%Y%m%d%H}/f{step:03d}_{var}.grib2",
                "region": "eu-west-1",
            },
        )
        base.update(overrides)
        return NWPModel(**base)

    def test_fetch_one_reads_keys_and_concatenates(self, monkeypatch, tmp_path):
        """fetch_one builds per-variable S3 keys and concatenates their bodies."""
        import sys
        import types

        keys = []

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        class _Client:
            def get_object(self, Bucket, Key):
                keys.append((Bucket, Key))
                return {"Body": _Body(b"<" + Key.rsplit("_", 1)[-1].encode() + b">")}

        boto3_mod = types.ModuleType("boto3")
        boto3_mod.client = lambda *a, **k: _Client()
        botocore = types.ModuleType("botocore")
        botocore.UNSIGNED = object()
        client_mod = types.ModuleType("botocore.client")
        client_mod.Config = lambda **k: None
        botocore.client = client_mod
        monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
        monkeypatch.setitem(sys.modules, "botocore", botocore)
        monkeypatch.setitem(sys.modules, "botocore.client", client_mod)

        out = MeteoFranceCentre(tmp_path).fetch_one(
            self._mf(),
            dt.datetime(2024, 6, 1, 0),
            3,
            ["temperature_2m", "precipitation_acc"],
            "auto",
        )
        assert keys == [
            ("mf-nwp-models", "arpege-world/2024060100/f003_T2M.grib2"),
            ("mf-nwp-models", "arpege-world/2024060100/f003_TP.grib2"),
        ]
        assert out.read_bytes() == b"<T2M.grib2><TP.grib2>"
        assert out.name == "arpege_2024060100_f003.grib2"

    def test_fetch_one_without_bucket_raises(self, tmp_path):
        """A row lacking bucket/key_template in request_options is rejected."""
        model = self._mf(request_options={})
        with pytest.raises(ValueError, match="bucket"):
            MeteoFranceCentre(tmp_path).fetch_one(
                model, dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
            )

    def test_missing_boto3_raises_friendly(self, monkeypatch, tmp_path):
        """A missing boto3 surfaces an earthlens[nwp] ImportError."""
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(ImportError, match=r"earthlens\[nwp\]"):
            MeteoFranceCentre(tmp_path).fetch_one(
                self._mf(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
            )

    def test_fetch_one_failure_leaves_no_partial_file(self, monkeypatch, tmp_path):
        """A get_object failure unlinks the partial file and re-raises."""
        boto3_mod = types.ModuleType("boto3")

        class _Client:
            def get_object(self, Bucket, Key):
                raise RuntimeError("s3 down")

        boto3_mod.client = lambda *a, **k: _Client()
        botocore = types.ModuleType("botocore")
        botocore.UNSIGNED = object()
        client_mod = types.ModuleType("botocore.client")
        client_mod.Config = lambda **k: None
        botocore.client = client_mod
        monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
        monkeypatch.setitem(sys.modules, "botocore", botocore)
        monkeypatch.setitem(sys.modules, "botocore.client", client_mod)
        with pytest.raises(RuntimeError, match="s3 down"):
            MeteoFranceCentre(tmp_path).fetch_one(
                self._mf(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
            )
        assert list(tmp_path.iterdir()) == [], "no partial file should remain"


class TestMeteoFranceAPICentre:
    """Tests for the authenticated WCS-API Météo-France centre."""

    def _arpege(self, **overrides) -> NWPModel:
        """Build an ARPEGE WCS-API row, overriding any field."""
        base = dict(
            provider="meteofrance",
            model_family="arpege",
            cycles_utc=[0, 6, 12, 18],
            horizon_h=102,
            backend="meteofrance-api",
            bands={
                "temperature_2m": "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
                "precipitation_acc": "TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE",
            },
            request_options={
                "api_base": "https://public-api.meteofrance.fr/public/arpege/1.0",
                "coverage_service": "MF-NWP-GLOBAL-ARPEGE-025-GLOBE-WCS",
            },
        )
        base.update(overrides)
        return NWPModel(**base)

    def test_resolve_api_key_from_env(self, monkeypatch):
        """resolve_api_key reads METEO_FRANCE_API_KEY / MF_API_KEY."""
        monkeypatch.delenv("METEO_FRANCE_API_KEY", raising=False)
        monkeypatch.setenv("MF_API_KEY", "secret-key")
        assert resolve_api_key() == "secret-key"

    def test_resolve_api_key_missing_raises(self, monkeypatch):
        """A missing API key raises AuthenticationError naming the env var."""
        from earthlens.base import AuthenticationError

        monkeypatch.delenv("METEO_FRANCE_API_KEY", raising=False)
        monkeypatch.delenv("MF_API_KEY", raising=False)
        with pytest.raises(AuthenticationError, match="METEO_FRANCE_API_KEY"):
            resolve_api_key()

    def test_fetch_one_builds_wcs_getcoverage(self, monkeypatch, tmp_path):
        """fetch_one issues a GetCoverage per band with apikey + bbox subset."""
        monkeypatch.setenv("MF_API_KEY", "k")
        calls = []

        class _Resp:
            def __init__(self, content):
                self.content = content
                self.status_code = 200
                self.headers = {}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=None):
                """Yield the canned body in one chunk, as a streamed response would."""
                yield self.content

            def close(self):
                """Release the response, as the streaming call sites do."""
                self.closed = True

        def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
            calls.append({"url": url, "params": params, "headers": headers})
            return _Resp(b"GRIB-" + dict(params)["coverageid"].split("__")[0].encode())

        module = types.ModuleType("requests")
        module.get = fake_get
        monkeypatch.setitem(sys.modules, "requests", module)

        centre = MeteoFranceAPICentre(tmp_path)
        centre.bbox = (-5.0, 41.0, 10.0, 51.0)
        out = centre.fetch_one(
            self._arpege(),
            dt.datetime(2024, 6, 1, 0),
            24,
            ["temperature_2m", "precipitation_acc"],
            "auto",
        )
        assert len(calls) == 2
        first = calls[0]
        assert first["url"].endswith(
            "/wcs/MF-NWP-GLOBAL-ARPEGE-025-GLOBE-WCS/GetCoverage"
        )
        assert first["headers"]["apikey"] == "k"
        qs = first["params"]
        assert ("service", "WCS") in qs and ("version", "2.0.1") in qs
        assert (
            "coverageid",
            "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___2024-06-01T00.00.00Z",
        ) in qs
        # valid time = cycle + 24 h; bbox subsets present
        assert ("subset", "time(2024-06-02T00:00:00Z)") in qs
        assert ("subset", "lat(41.0,51.0)") in qs
        assert ("subset", "long(-5.0,10.0)") in qs
        assert out.name == "arpege_2024060100_f024.grib2"
        assert out.read_bytes() == b"GRIB-TEMPERATUREGRIB-TOTAL_PRECIPITATION"

    def test_coverage_query_pressure_level(self, tmp_path):
        """A COVERAGE@level band adds an isobaric pressure subset (hPa -> Pa)."""
        import datetime as dt2

        centre = MeteoFranceAPICentre(tmp_path)
        centre.bbox = (-5.0, 41.0, 10.0, 51.0)
        query = centre._coverage_query(
            "TEMPERATURE__ISOBARIC_SURFACE@850",
            dt2.datetime(2024, 6, 1, 0),
            dt2.datetime(2024, 6, 1, 12),
        )
        assert (
            "coverageid",
            "TEMPERATURE__ISOBARIC_SURFACE___2024-06-01T00.00.00Z",
        ) in query
        assert ("subset", "pressure(85000)") in query

    def test_fetch_one_without_options_raises(self, monkeypatch, tmp_path):
        """A row lacking api_base / coverage_service is rejected."""
        monkeypatch.setenv("MF_API_KEY", "k")
        with pytest.raises(ValueError, match="api_base"):
            MeteoFranceAPICentre(tmp_path).fetch_one(
                self._arpege(request_options={}),
                dt.datetime(2024, 6, 1, 0),
                0,
                ["temperature_2m"],
                "auto",
            )

    def test_query_without_bbox_omits_spatial_subset(self, tmp_path):
        """With no bbox set, the WCS query carries only the time subset."""
        import datetime as dt2

        centre = MeteoFranceAPICentre(tmp_path)  # bbox defaults to None
        query = centre._coverage_query(
            "TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND",
            dt2.datetime(2024, 6, 1, 0),
            dt2.datetime(2024, 6, 1, 12),
        )
        subsets = [v for k, v in query if k == "subset"]
        assert subsets == ["time(2024-06-01T12:00:00Z)"]

    def test_fetch_one_failure_leaves_no_partial_file(self, monkeypatch, tmp_path):
        """A GetCoverage failure unlinks the partial file and re-raises."""
        monkeypatch.setenv("MF_API_KEY", "k")

        def failing_get(url, params=None, headers=None, timeout=None, **kwargs):
            raise RuntimeError("gateway down")

        module = types.ModuleType("requests")
        module.get = failing_get
        monkeypatch.setitem(sys.modules, "requests", module)
        with pytest.raises(RuntimeError, match="gateway down"):
            MeteoFranceAPICentre(tmp_path).fetch_one(
                self._arpege(),
                dt.datetime(2024, 6, 1, 0),
                0,
                ["temperature_2m"],
                "auto",
            )
        assert list(tmp_path.iterdir()) == [], "no partial file should remain"


class TestECCCCentre:
    """Tests for the direct-HTTPS ECCC MSC Datamart centre."""

    _GDPS_TMPL = (
        "https://dd.weather.gc.ca/{date:%Y%m%d}/WXO-DD/model_gdps/15km/"
        "{cycle:%H}/{step:03d}/{date:%Y%m%d}T{cycle:%H}Z_MSC_GDPS_"
        "{var}_LatLon0.15_PT{step:03d}H.grib2"
    )
    _GEPS_TMPL = (
        "https://dd.weather.gc.ca/{date:%Y%m%d}/WXO-DD/ensemble/geps/grib2/raw/"
        "{cycle:%H}/{step:03d}/CMC_geps-raw_{var}_latlon0p5x0p5_"
        "{date:%Y%m%d}{cycle:%H}_P{step:03d}_allmbrs.grib2"
    )

    def _gdps(self, **overrides) -> NWPModel:
        """Build a GDPS catalog row, overriding any field."""
        base = dict(
            provider="eccc-msc",
            model_family="gdps",
            cycles_utc=[0, 12],
            horizon_h=240,
            idx=False,
            backend="eccc-msc",
            url_template=self._GDPS_TMPL,
            bands={
                "temperature_2m": "AirTemp_AGL-2m",
                "precipitation_acc": "Precip-Accum_Sfc",
            },
        )
        base.update(overrides)
        return NWPModel(**base)

    def _fake_requests_uncompressed(self, monkeypatch):
        """Fake `requests.Session.get` that streams the variable token as the body."""
        import sys
        import types

        state: dict[str, Any] = {"urls": [], "stream_calls": 0, "chunk_sizes": []}

        class _Resp:
            def __init__(self, body):
                self._body = body
                self.status_code = 200
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def close(self):
                return None

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                state["stream_calls"] += 1
                state["chunk_sizes"].append(chunk_size)
                # Emit two chunks so the test exercises the iter loop, not
                # a single materialise-then-write path.
                yield self._body[: len(self._body) // 2]
                yield self._body[len(self._body) // 2 :]

        class _Session:
            def get(self, url, stream=False, timeout=None, **kwargs):
                state["urls"].append(url)
                var = url.rsplit("_MSC_GDPS_", 1)[-1].split("_LatLon", 1)[0]
                return _Resp(b"<" + var.encode() + b">")

            def close(self):
                pass

        module = types.ModuleType("requests")
        module.Session = _Session
        module.get = lambda url, **kw: _Resp(b"")  # legacy compat (other tests)
        monkeypatch.setitem(sys.modules, "requests", module)
        return state

    def test_fetch_one_builds_urls_and_concatenates(self, monkeypatch, tmp_path):
        """`fetch_one` builds per-band URLs and concatenates the bodies."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        state = self._fake_requests_uncompressed(monkeypatch)
        out = ECCCCentre(tmp_path).fetch_one(
            self._gdps(),
            dt.datetime(2026, 6, 17, 0),
            3,
            ["temperature_2m", "precipitation_acc"],
            "origin",
        )
        assert state["urls"] == [
            "https://dd.weather.gc.ca/20260617/WXO-DD/model_gdps/15km/00/003/"
            "20260617T00Z_MSC_GDPS_AirTemp_AGL-2m_LatLon0.15_PT003H.grib2",
            "https://dd.weather.gc.ca/20260617/WXO-DD/model_gdps/15km/00/003/"
            "20260617T00Z_MSC_GDPS_Precip-Accum_Sfc_LatLon0.15_PT003H.grib2",
        ]
        assert out.read_bytes() == b"<AirTemp_AGL-2m><Precip-Accum_Sfc>"
        assert out.name == "gdps_2026061700_f003.grib2"

    def test_fetch_one_without_template_raises(self, tmp_path):
        """A model lacking a url_template raises ValueError."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        model = self._gdps(url_template=None)
        with pytest.raises(ValueError, match="url_template"):
            ECCCCentre(tmp_path).fetch_one(
                model, dt.datetime(2026, 6, 17, 0), 0, ["temperature_2m"], "origin"
            )

    def test_band_url_unknown_param_raises(self, tmp_path):
        """An unknown band name surfaces a clean KeyError."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        model = self._gdps()
        with pytest.raises(KeyError):
            ECCCCentre._band_url(model, "nope", dt.datetime(2026, 6, 17, 0), 0)

    def test_band_url_geps_member_substitutes(self):
        """A GEPS-style template with `{member:03d}` zero-pads the member id."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        template = (
            "https://dd.weather.gc.ca/{date:%Y%m%d}/.../{var}_mem{member:03d}.grib2"
        )
        model = NWPModel(
            provider="eccc-msc",
            backend="eccc-msc",
            cycles_utc=[0, 12],
            url_template=template,
            bands={"temperature_2m": "TMP_TGL_2m"},
        )
        url = ECCCCentre._band_url(
            model, "temperature_2m", dt.datetime(2026, 6, 17, 0), 0, member="7"
        )
        assert url.endswith("TMP_TGL_2m_mem007.grib2")

    def test_band_url_non_numeric_member_raises_value_error(self):
        """A non-numeric member id is rejected with a clean ValueError."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        template = "https://example.test/{var}_{member:03d}.grib2"
        model = NWPModel(
            provider="eccc-msc",
            backend="eccc-msc",
            cycles_utc=[0, 12],
            url_template=template,
            bands={"temperature_2m": "TMP_TGL_2m"},
        )
        with pytest.raises(ValueError, match="numeric string"):
            ECCCCentre._band_url(
                model, "temperature_2m", dt.datetime(2026, 6, 17, 0), 0, member="cf"
            )

    def test_unknown_mirror_raises(self, tmp_path):
        """A `mirror=` value outside ('auto', 'origin') fails loud."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        with pytest.raises(ValueError, match="single origin host"):
            ECCCCentre(tmp_path).fetch_one(
                self._gdps(),
                dt.datetime(2026, 6, 17, 0),
                0,
                ["temperature_2m"],
                mirror="msc-backup",
            )

    def test_fetch_one_streams_with_iter_content(self, monkeypatch, tmp_path):
        """`fetch_one` streams via `iter_content` (no `.content` materialisation)."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        state = self._fake_requests_uncompressed(monkeypatch)
        ECCCCentre(tmp_path).fetch_one(
            self._gdps(),
            dt.datetime(2026, 6, 17, 0),
            3,
            ["temperature_2m"],
            "origin",
        )
        assert state["stream_calls"] >= 1
        assert all(size > 0 for size in state["chunk_sizes"])

    def test_fetch_one_skips_empty_chunks(self, monkeypatch, tmp_path):
        """An empty chunk from `iter_content` is filtered out, not written."""
        import sys
        import types

        from earthlens.nwp.centres.eccc import ECCCCentre

        class _Resp:
            def __init__(self):
                self.status_code = 200
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def close(self):
                return None

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield b""
                yield b"payload"
                yield b""

        class _Session:
            def get(self, url, stream=False, timeout=None, **kwargs):
                return _Resp()

            def close(self):
                pass

        module = types.ModuleType("requests")
        module.Session = _Session
        monkeypatch.setitem(sys.modules, "requests", module)
        out = ECCCCentre(tmp_path).fetch_one(
            self._gdps(),
            dt.datetime(2026, 6, 17, 0),
            3,
            ["temperature_2m"],
            "origin",
        )
        # Only the non-empty middle chunk is written.
        assert out.read_bytes() == b"payload"

    def test_fetch_one_reuses_one_session_per_centre(self, monkeypatch, tmp_path):
        """One `requests.Session()` is reused across multiple fetch_one calls."""
        from earthlens.nwp.centres.eccc import ECCCCentre

        self._fake_requests_uncompressed(monkeypatch)
        centre = ECCCCentre(tmp_path)
        centre.fetch_one(
            self._gdps(),
            dt.datetime(2026, 6, 17, 0),
            3,
            ["temperature_2m"],
            "origin",
        )
        first_session = centre._session
        centre.fetch_one(
            self._gdps(),
            dt.datetime(2026, 6, 17, 12),
            6,
            ["temperature_2m"],
            "origin",
        )
        assert centre._session is first_session

    def test_fetch_one_failure_leaves_no_partial_file(self, tmp_path, monkeypatch):
        """A failure on a later band leaves no truncated `.grib2`."""
        import sys
        import types

        class _FailingSession:
            def get(self, url, stream=False, timeout=None, **kwargs):
                raise RuntimeError("network down")

            def close(self):
                pass

        module = types.ModuleType("requests")
        module.Session = _FailingSession
        module.get = lambda url, **kw: (_ for _ in ()).throw(
            RuntimeError("network down")
        )
        monkeypatch.setitem(sys.modules, "requests", module)

        from earthlens.nwp.centres.eccc import ECCCCentre

        with pytest.raises(RuntimeError, match="network down"):
            ECCCCentre(tmp_path).fetch_one(
                self._gdps(),
                dt.datetime(2026, 6, 17, 0),
                0,
                ["temperature_2m"],
                "origin",
            )
        assert list(tmp_path.iterdir()) == [], "no partial file should remain"


def test_base_centre_fetch_one_raises_not_implemented(tmp_path):
    """The abstract base _NWPCentre.fetch_one body raises NotImplementedError."""
    import datetime as dt

    from earthlens.nwp.centres.base import _NWPCentre

    class _Bare(_NWPCentre):
        def fetch_one(
            self, model, cycle, step, params, mirror, member=None, *, whole=False
        ):
            return super().fetch_one(
                model, cycle, step, params, mirror, member, whole=whole
            )

    with pytest.raises(NotImplementedError):
        _Bare(tmp_path).fetch_one(None, dt.datetime(2026, 1, 1), 0, [], "auto")


class _NoBufferResponse:
    """Streams its body in small blocks; reading `.content` is a failure."""

    def __init__(self, body: bytes, block: int = 8):
        self._body = body
        self._block = block
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.closed = False

    @property
    def content(self) -> bytes:
        raise AssertionError("the body must be streamed, not buffered via .content")

    def raise_for_status(self) -> None:
        """A 200 needs no action."""

    def iter_content(self, chunk_size=None):
        """Yield the body in fixed-size blocks, as a streamed response does."""
        for start in range(0, len(self._body), self._block):
            yield self._body[start : start + self._block]

    def close(self) -> None:
        """Record release of the response."""
        self.closed = True


class TestDWDStreams:
    """DWD decompresses its .bz2 incrementally rather than whole-body."""

    def _stream_requests(self, monkeypatch, block: int = 8):
        """Install a requests whose get streams a bz2 body across many blocks."""
        import bz2

        state: dict[str, object] = {"responses": [], "kwargs": []}

        def fake_get(url: str, timeout=None, **kwargs):
            state["kwargs"].append(kwargs)
            var = pathlib.Path(url).parent.name.upper()
            payload = bz2.compress(b"<" + var.encode() + b">")
            response = _NoBufferResponse(payload, block=block)
            state["responses"].append(response)
            return response

        module = types.ModuleType("requests")
        module.get = fake_get
        monkeypatch.setitem(sys.modules, "requests", module)
        return state

    def _icon(self, **overrides) -> NWPModel:
        """Reuse the DWD centre's ICON row builder."""
        return TestDWDCentre._icon(TestDWDCentre(), **overrides)

    def test_body_is_decompressed_without_buffering(self, monkeypatch, tmp_path):
        """A regression to `bz2.decompress(response.content)` fails this test."""
        state = self._stream_requests(monkeypatch)
        out = DWDCentre(tmp_path).fetch_one(
            self._icon(),
            dt.datetime(2024, 6, 1, 0),
            3,
            ["temperature_2m", "precipitation_acc"],
            "auto",
        )
        assert out.read_bytes() == b"<T_2M><TOT_PREC>"
        assert all(r.closed for r in state["responses"]), "responses must be released"

    def test_get_is_streaming(self, monkeypatch, tmp_path):
        """The per-variable GET passes stream=True."""
        state = self._stream_requests(monkeypatch)
        DWDCentre(tmp_path).fetch_one(
            self._icon(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
        )
        assert state["kwargs"][0].get("stream") is True, f"got {state['kwargs'][0]}"

    def test_decompression_spans_block_boundaries(self, monkeypatch, tmp_path):
        """A single-byte block stream still reassembles the exact payload."""
        self._stream_requests(monkeypatch, block=1)
        out = DWDCentre(tmp_path).fetch_one(
            self._icon(), dt.datetime(2024, 6, 1, 0), 0, ["temperature_2m"], "auto"
        )
        assert out.read_bytes() == b"<T_2M>", "bz2 must be decompressed incrementally"


class TestMeteoFranceAPIStreams:
    """The WCS coverage body is copied block by block, not buffered."""

    def test_coverage_is_streamed(self, monkeypatch, tmp_path):
        """A regression to `response.content` fails this test."""
        monkeypatch.setenv("MF_API_KEY", "k")
        responses: list[_NoBufferResponse] = []
        seen: list[dict] = []

        def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
            seen.append(kwargs)
            coverage = dict(params)["coverageid"].split("__")[0]
            response = _NoBufferResponse(b"GRIB-" + coverage.encode())
            responses.append(response)
            return response

        module = types.ModuleType("requests")
        module.get = fake_get
        monkeypatch.setitem(sys.modules, "requests", module)

        centre = MeteoFranceAPICentre(tmp_path)
        out = centre.fetch_one(
            TestMeteoFranceAPICentre._arpege(TestMeteoFranceAPICentre()),
            dt.datetime(2024, 6, 1, 0),
            24,
            ["temperature_2m", "precipitation_acc"],
            "auto",
        )
        assert out.read_bytes() == b"GRIB-TEMPERATUREGRIB-TOTAL_PRECIPITATION"
        assert seen[0].get("stream") is True, f"got {seen[0]}"
        assert all(r.closed for r in responses), "responses must be released"


class TestDecompressStream:
    """The streamed bz2 reader must behave exactly like `bz2.decompress`."""

    #: Bodies covering every shape the DWD feed can produce, plus the
    #: pathological ones. Each is run at several chunk sizes, including sizes
    #: that land a stream boundary exactly on a chunk edge — the case the
    #: first version of this reader got wrong.
    def _bodies(self):
        """Return `{name: body}` for the differential cases."""
        import bz2

        one = bz2.compress(b"AAAA")
        two = bz2.compress(b"BBBB")
        big = bz2.compress(b"C" * 5000)
        return {
            "single": one,
            "multi_two": one + two,
            "multi_three": one + two + big,
            "trailing_nulls": one + b"\x00\x00\x00",
            "trailing_garbage": one + b"not-a-stream",
            "truncated": one[: len(one) // 2],
            "truncated_second_stream": one + two[: len(two) // 2],
            "empty": b"",
            "not_bz2_at_all": b"plain bytes, never compressed",
            "big_then_small": big + one,
        }

    def _run(self, body: bytes, chunk: int):
        """Feed `body` through the reader in `chunk`-sized blocks."""
        import io

        from earthlens.nwp.centres.dwd import _decompress_stream

        blocks = [body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""]
        buf = io.BytesIO()
        try:
            _decompress_stream(iter(blocks), buf, "https://h/x.bz2")
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            return type(exc).__name__, None
        return "ok", buf.getvalue()

    def _reference(self, body: bytes):
        """The same body through `bz2.decompress`, the behaviour to match."""
        import bz2

        try:
            return "ok", bz2.decompress(body)
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            return type(exc).__name__, None

    @pytest.mark.parametrize(
        "case",
        [
            "single",
            "multi_two",
            "multi_three",
            "trailing_nulls",
            "trailing_garbage",
            "truncated",
            "truncated_second_stream",
            "empty",
            "not_bz2_at_all",
            "big_then_small",
        ],
    )
    def test_matches_bz2_decompress_at_every_chunking(self, case):
        """Streamed decoding agrees with `bz2.decompress` for any block split.

        Args:
            case: Key into the body table.
        """
        import bz2

        body = self._bodies()[case]
        expected = self._reference(body)
        one_stream = len(bz2.compress(b"AAAA"))
        sizes = sorted({1, 2, 7, 13, one_stream, one_stream + 1, len(body) or 1, 4096})
        for chunk in sizes:
            got = self._run(body, chunk)
            assert got == expected, (
                f"{case} at chunk={chunk}: got {got[0]} / {got[1]!r}, "
                f"expected {expected[0]} / {expected[1]!r}"
            )

    def test_stream_boundary_on_a_chunk_edge(self):
        """A boundary exactly at a block edge must not raise EOFError.

        Regression: re-seeding only when `unused_data` was non-empty in the
        same iteration left an exhausted decompressor to receive the next
        block, which raises `EOFError: End of stream already reached`.
        """
        import bz2
        import io

        from earthlens.nwp.centres.dwd import _decompress_stream

        one, two = bz2.compress(b"AAAA"), bz2.compress(b"BBBB")
        buf = io.BytesIO()
        _decompress_stream(iter([one, two]), buf, "https://h/x.bz2")
        assert buf.getvalue() == b"AAAABBBB"

    def test_returns_the_decompressed_byte_count(self):
        """The return value lets the caller reject an empty band."""
        import bz2
        import io

        from earthlens.nwp.centres.dwd import _decompress_stream

        payload = b"GRIB2" * 100
        written = _decompress_stream(
            iter([bz2.compress(payload)]), io.BytesIO(), "https://h/x.bz2"
        )
        assert written == len(payload)

    def test_empty_body_returns_zero_without_raising(self):
        """An empty body is `bz2.decompress`'s no-op, not a truncation."""
        import io

        from earthlens.nwp.centres.dwd import _decompress_stream

        assert _decompress_stream(iter([b""]), io.BytesIO(), "https://h/e.bz2") == 0

    def test_truncation_message_redacts_the_url(self):
        """The error names the host only — never a URL-borne credential."""
        import bz2
        import io

        from earthlens.nwp.centres.dwd import _decompress_stream

        whole = bz2.compress(b"body" * 200)
        with pytest.raises(ValueError) as exc:
            _decompress_stream(
                iter([whole[: len(whole) // 2]]),
                io.BytesIO(),
                "https://h/x.bz2?token=SECRET",
            )
        assert "SECRET" not in str(exc.value), f"secret leaked: {exc.value}"

    def test_empty_band_body_aborts_the_fetch(self, monkeypatch, tmp_path):
        """A band that returns nothing fails rather than vanishing from the GRIB2."""

        def fake_get(url, timeout=None, **kwargs):
            return _NoBufferResponse(b"", block=16)

        module = types.ModuleType("requests")
        module.get = fake_get
        monkeypatch.setitem(sys.modules, "requests", module)

        with pytest.raises(ValueError, match="empty body for band"):
            DWDCentre(tmp_path).fetch_one(
                TestDWDCentre._icon(TestDWDCentre()),
                dt.datetime(2024, 6, 1, 0),
                0,
                ["temperature_2m"],
                "auto",
            )
        assert list(tmp_path.glob("*.grib2")) == [], "no GRIB2 should be published"

    def test_truncated_fetch_leaves_no_grib_behind(self, monkeypatch, tmp_path):
        """A truncated band aborts `fetch_one` without publishing the .grib2."""
        import bz2

        whole = bz2.compress(b"<T_2M>" * 200)
        short = whole[: len(whole) // 2]

        def fake_get(url, timeout=None, **kwargs):
            return _NoBufferResponse(short, block=16)

        module = types.ModuleType("requests")
        module.get = fake_get
        monkeypatch.setitem(sys.modules, "requests", module)

        with pytest.raises(ValueError, match="truncated bz2 body"):
            DWDCentre(tmp_path).fetch_one(
                TestDWDCentre._icon(TestDWDCentre()),
                dt.datetime(2024, 6, 1, 0),
                0,
                ["temperature_2m"],
                "auto",
            )
        assert list(tmp_path.glob("*.grib2")) == [], "a short GRIB2 was published"
        assert list(tmp_path.glob("*.part")) == [], "the partial write was left behind"
