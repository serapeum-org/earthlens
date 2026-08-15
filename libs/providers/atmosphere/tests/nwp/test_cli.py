"""Tests for the NWP catalog-tooling handlers (`earthlens.nwp.cli`).

Moved out of core's CLI test suite when the NWP handlers moved into this
distribution (issue #863).
"""

from __future__ import annotations

import datetime as dt
import pathlib
import runpy
import sys
import types
from types import SimpleNamespace

import pytest

import earthlens.nwp.cli as nwp_cli
from earthlens.cli.adapter import list_backends, load_catalog
from earthlens.cli.curate import probe_dataset
from earthlens.cli.validate import validate_one
from earthlens.nwp.catalog import NWPModel

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the nwp backend."""
    return next(b for b in list_backends() if b.provider == "nwp")


def _load():
    """Load the bundled nwp catalog."""
    return load_catalog(_info())


class TestProber:
    """Tests for the NWP `.idx` band prober (Herbie template, no eccodes)."""

    def test_reports_band_presence(self, monkeypatch):
        """nwp probe flags which catalog band tokens appear in the live .idx."""
        catalog = _load()
        model_key = next(
            key
            for key, model in catalog.datasets.items()
            if getattr(model, "model_family", None)
            not in nwp_cli._NWP_NO_IDX_FAMILIES | nwp_cli._NWP_NEEDS_EXTRA_ATTRS
            and (getattr(model, "bands", None) or {})
        )
        token = next(iter(catalog.datasets[model_key].bands.values()))
        monkeypatch.setattr(
            nwp_cli, "_nwp_idx_body", lambda model: f"1:0:d=x:{token}:surface:\n"
        )
        result = probe_dataset(_info(), model_key)
        assert result.status == "ok", "nwp probe ran"
        assert any(v["present"] for v in result.assets.values()), "a band present"

    def test_no_idx_family_is_error(self):
        """An ECCC model (no .idx) reports 'error' with the reason."""
        catalog = _load()
        eccc = next(
            (
                key
                for key, model in catalog.datasets.items()
                if getattr(model, "model_family", None) in nwp_cli._NWP_NO_IDX_FAMILIES
            ),
            None,
        )
        if eccc is None:
            pytest.skip("no ECCC model in the catalog")
        result = probe_dataset(_info(), eccc)
        assert result.status == "error"
        assert "no .idx" in result.detail


class TestDeepProber:
    """Tests for the credentialed nwp `--deep` availability sampler."""

    def test_reports_live_availability(self, monkeypatch):
        """nwp --deep reports the model's live availability for a recent cycle."""
        monkeypatch.setattr(
            nwp_cli, "_nwp_availability", lambda model, cycle, step: "HTTP 200 (ok)"
        )
        catalog = _load()
        model_key = next(
            key
            for key, model in catalog.datasets.items()
            if getattr(model, "backend", None) == "direct-https"
        )
        result = probe_dataset(_info(), model_key, deep=True)
        assert result.status == "ok", "nwp deep probe ran"
        entry = next(iter(result.assets.values()))
        assert "HTTP 200" in entry["status"], "availability status reported"


class TestAvailability:
    """Tests for the _nwp_availability dispatch + per-backend branches."""

    def test_direct_https_builds_url(self, monkeypatch):
        """_nwp_availability HEADs the first band's URL for a direct-https model."""
        calls = {}

        class _Resp:
            status_code = 200

        def fake_head(url, timeout=None, allow_redirects=None):
            calls["url"] = url
            return _Resp()

        monkeypatch.setattr(nwp_cli.requests, "head", fake_head)
        model = NWPModel(
            provider="dwd-opendata",
            backend="direct-https",
            cycles_utc=[0],
            url_template="https://x/{var_lc}/f{step:03d}_{var}.bz2",
            bands={"temperature_2m": "T_2M"},
        )
        result = nwp_cli._nwp_availability(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "HTTP 200" in result
        assert calls["url"] == "https://x/t_2m/f000_T_2M.bz2"

    def test_herbie_unavailable(self, monkeypatch):
        """_nwp_availability reports herbie missing rather than raising."""
        monkeypatch.setitem(sys.modules, "herbie", None)
        model = NWPModel(provider="noaa-nodd", model_family="gfs", backend="herbie")
        result = nwp_cli._nwp_availability(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "herbie unavailable" in result

    def test_recent_cycle_is_in_the_past(self):
        """_nwp_recent_cycle returns a datetime at or before ~now."""
        cycle = nwp_cli._nwp_recent_cycle(
            NWPModel(provider="p", backend="direct-https", cycles_utc=[0, 12])
        )
        assert cycle <= dt.datetime.now(dt.UTC).replace(tzinfo=None), "cycle in past"

    def test_unknown_backend(self):
        """An unrecognised backend reports that no probe exists."""
        model = SimpleNamespace(backend="mystery", bands={}, request_options={})
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "no live availability probe" in out, "unknown backend reported"

    def test_direct_boto3_head_object(self, monkeypatch):
        """A direct-boto3 model HEADs the object and reports its size."""
        boto3 = types.ModuleType("boto3")
        botocore = types.ModuleType("botocore")
        botocore_client = types.ModuleType("botocore.client")
        botocore.UNSIGNED = object()
        botocore_client.Config = lambda **kw: None

        class FakeS3:
            def head_object(self, Bucket, Key):
                return {"ContentLength": 1234}

        boto3.client = lambda *a, **kw: FakeS3()
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        monkeypatch.setitem(sys.modules, "botocore", botocore)
        monkeypatch.setitem(sys.modules, "botocore.client", botocore_client)
        model = NWPModel(
            provider="p",
            backend="direct-boto3",
            bands={"t": "T"},
            request_options={"bucket": "b", "key_template": "{var}.grib"},
        )
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "1234 bytes" in out, "head_object size reported"

    def test_meteofrance_needs_key(self, monkeypatch):
        """A meteofrance model with no API key reports the missing-credential."""
        monkeypatch.delenv("METEO_FRANCE_API_KEY", raising=False)
        monkeypatch.delenv("MF_API_KEY", raising=False)
        model = NWPModel(
            provider="mf",
            backend="meteofrance-api",
            request_options={"api_base": "https://x", "coverage_service": "svc"},
        )
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "METEO_FRANCE_API_KEY" in out, "missing key reported"

    def test_direct_boto3_missing_options(self):
        """A direct-boto3 model lacking bucket/key/bands reports the gap."""
        model = NWPModel(provider="p", backend="direct-boto3")
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "bucket" in out, "missing options reported"

    def test_ecmwf_opendata_latest(self, monkeypatch):
        """The ecmwf-opendata backend reports the latest cycle from the client."""
        opendata = types.ModuleType("ecmwf.opendata")
        opendata.Client = lambda source=None, model=None: types.SimpleNamespace(
            latest=lambda **kw: dt.datetime(2024, 6, 1, 0)
        )
        monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
        monkeypatch.setitem(sys.modules, "ecmwf.opendata", opendata)
        model = NWPModel(
            provider="p", backend="ecmwf-opendata", request_options={"type": "fc"}
        )
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "latest cycle" in out, "latest cycle reported"

    def test_herbie_resolves_grib(self, monkeypatch):
        """The herbie backend reports the resolved GRIB path."""
        herbie = types.ModuleType("herbie")
        herbie.Herbie = lambda cycle, **kw: types.SimpleNamespace(grib="s3://x.grib")
        monkeypatch.setitem(sys.modules, "herbie", herbie)
        model = NWPModel(provider="p", backend="herbie", model_family="gfs")
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "resolved" in out, "GRIB path reported"

    def test_direct_https_unreachable(self, monkeypatch):
        """A direct-https HEAD failure is reported as unreachable, not raised."""

        def boom(url, timeout=None, allow_redirects=None):
            raise RuntimeError("dns")

        monkeypatch.setattr(nwp_cli.requests, "head", boom)
        model = NWPModel(
            provider="p",
            backend="direct-https",
            url_template="https://x/{var}",
            bands={"t": "T"},
        )
        out = nwp_cli._nwp_availability(model, dt.datetime(2024, 1, 1), 0)
        assert "unreachable" in out, "HEAD failure reported"


class TestIdx:
    """Cover the Herbie `.idx` URL + body helpers (runpy / requests mocked)."""

    def test_idx_url_from_template(self, monkeypatch):
        """_nwp_idx_url evaluates the template against a stub to recover the URL."""

        class _Tmpl:
            @staticmethod
            def template(stub):
                stub.SOURCES = {"aws": "https://aws/file"}

        monkeypatch.setattr(runpy, "run_path", lambda p: {"gfs": _Tmpl})
        model = NWPModel(
            provider="p", backend="direct-https", model_family="gfs", product=""
        )
        url = nwp_cli._nwp_idx_url(
            pathlib.Path("/x"), model, dt.datetime(2024, 1, 1), 0
        )
        assert url == "https://aws/file.idx", "aws source + .idx suffix"

    def test_idx_body_returns_reachable_text(self, monkeypatch):
        """_nwp_idx_body returns the first reachable cycle's .idx text."""
        monkeypatch.setattr(nwp_cli, "_herbie_models_dir", lambda: pathlib.Path("/x"))
        monkeypatch.setattr(nwp_cli, "_nwp_idx_url", lambda md, m, c, s: "https://x")
        monkeypatch.setattr(
            nwp_cli.requests,
            "get",
            lambda url, timeout=None: types.SimpleNamespace(
                status_code=200, text="1:0:VAR:\n"
            ),
        )
        model = NWPModel(
            provider="p", backend="direct-https", horizon_h=6, bands={"t": "VAR"}
        )
        assert nwp_cli._nwp_idx_body(model) == "1:0:VAR:\n", "idx text returned"

    def test_idx_body_unreachable_raises(self, monkeypatch):
        """When no cycle is reachable, _nwp_idx_body raises ValueError."""
        monkeypatch.setattr(nwp_cli, "_herbie_models_dir", lambda: pathlib.Path("/x"))
        monkeypatch.setattr(nwp_cli, "_nwp_idx_url", lambda md, m, c, s: "https://x")

        def boom(url, timeout=None):
            raise RuntimeError("offline")

        monkeypatch.setattr(nwp_cli.requests, "get", boom)
        model = NWPModel(provider="p", backend="direct-https", bands={"t": "VAR"})
        with pytest.raises(ValueError, match="no recent"):
            nwp_cli._nwp_idx_body(model)

    def test_idx_url_rejects_unsafe_model_family(self):
        """A model_family that is not a bare identifier is refused before runpy."""
        model = NWPModel(
            provider="p", backend="direct-https", model_family="../evil", product=""
        )
        with pytest.raises(ValueError, match="unsafe model_family"):
            nwp_cli._nwp_idx_url(pathlib.Path("/x"), model, "2024-01-01", 0)


class TestValidator:
    """Tests for the nwp structural + live validators."""

    def test_clean_catalog_has_no_issues(self):
        """The bundled nwp catalog passes its own structural lint."""
        checked, issues = nwp_cli.validator(_load())
        assert checked > 0, f"unexpected nwp issues: {issues}"
        assert issues == [], f"unexpected nwp issues: {issues}"

    def test_flags_missing_url_template(self):
        """A direct-https model with no url_template is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="direct-https",
                    url_template="",
                    bands={"t": 1},
                    cycles_utc=[0],
                    model_family="x",
                )
            }
        )
        checked, issues = nwp_cli.validator(catalog)
        assert checked == 1
        assert any(("url_template" in i for i in issues))

    def test_flags_empty_bands_and_bad_cycle(self):
        """An empty band map and an out-of-range cycle hour are flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="herbie",
                    model_family="m",
                    url_template=None,
                    bands={},
                    cycles_utc=[0, 99],
                )
            }
        )
        _checked, issues = nwp_cli.validator(catalog)
        assert any("empty band map" in i for i in issues), "empty bands flagged"
        assert any("out of range" in i for i in issues), "bad cycle flagged"

    def test_herbie_missing_model_family(self):
        """A herbie model with no model_family is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="herbie",
                    model_family="",
                    url_template=None,
                    bands={"t": 1},
                    cycles_utc=[0],
                )
            }
        )
        _checked, issues = nwp_cli.validator(catalog)
        assert any("model_family" in i for i in issues), "herbie family flagged"

    def test_validate_one_ok(self):
        """nwp validates clean end-to-end."""
        result = validate_one(_info())
        assert result.status == "ok"
        assert result.issues == []
        assert result.checked > 0, "models were checked"

    def test_live_flags_non_200_cycle(self, monkeypatch):
        """A direct-https model whose latest cycle does not HEAD 200 is flagged."""
        monkeypatch.setattr(nwp_cli, "http_head", lambda url: 404)
        result = validate_one(_info(), live=True)
        assert result.status == "ok", "404 cycle -> issue"
        assert result.issues, "404 cycle -> issue"

    def test_live_clean_at_200(self, monkeypatch):
        """All direct-https latest cycles HEADing 200 clear the nwp live check."""
        monkeypatch.setattr(nwp_cli, "http_head", lambda url: 200)
        result = validate_one(_info(), live=True)
        assert result.issues == [], "all 200 -> clean"

    def test_live_skips_non_direct_https(self):
        """Non-direct-https models are skipped by the nwp live check."""
        catalog = SimpleNamespace(
            datasets={"h": SimpleNamespace(backend="herbie", cycles_utc=[0])}
        )
        checked, issues = nwp_cli.live_validator(catalog)
        assert checked == 0, "herbie model skipped"
        assert issues == [], "herbie model skipped"

    def test_live_skips_model_without_url(self):
        """A direct-https model with no url_template/bands is skipped, not flagged."""
        catalog = SimpleNamespace(
            datasets={
                "x": SimpleNamespace(
                    backend="direct-https", cycles_utc=[0], url_template="", bands={}
                )
            }
        )
        checked, issues = nwp_cli.live_validator(catalog)
        assert checked == 0, "incomplete model skipped"
        assert issues == [], "incomplete model skipped"

    def test_latest_cycle_none_without_cycles(self):
        """_nwp_latest_cycle returns None for a model with no cycle hours."""
        assert nwp_cli._nwp_latest_cycle(SimpleNamespace(cycles_utc=[])) is None
