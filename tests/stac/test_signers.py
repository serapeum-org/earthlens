"""Unit tests for `earthlens.stac.signers`."""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from earthlens.base import AuthenticationError
from earthlens.stac import auth_cdse
from earthlens.stac.signers import (
    CDSESigner,
    CdseS3Signer,
    EarthdataSigner,
    PlanetaryComputerSigner,
    build_signer,
)


class _FakeResponse:
    """A urlopen() context-manager stand-in returning a fixed JSON payload."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _patch_urlopen(monkeypatch, payloads):
    """Patch urllib.request.urlopen to pop successive JSON payloads; count calls."""
    queue = list(payloads)
    calls = {"n": 0}

    def _fake(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


@pytest.mark.stac
class TestCdseS3Signer:
    """The CDSE signer authenticates asset reads via a GDAL S3 env."""

    def test_name(self):
        """The signer reports its catalog label."""
        assert CdseS3Signer("ak", "sk").name == "cdse-s3"

    def test_sign_request_and_item_are_noops(self):
        """CDSE search is anonymous and assets are signed via the env, not the href."""
        signer = CdseS3Signer("ak", "sk")
        assert signer.sign_request(object()) is None
        assert signer.sign_item(object()) is None

    def test_sign_href_rewrites_s3_to_vsis3(self):
        """An s3://eodata/<key> href becomes the /vsis3/eodata/<key> GDAL path."""
        out = CdseS3Signer("ak", "sk").sign_href("s3://eodata/foo/bar.tif")
        assert out == "/vsis3/eodata/foo/bar.tif"

    def test_sign_href_rewrites_https_eodata_host(self):
        """An https href on the CDSE host is rewritten to the /vsis3/eodata path."""
        out = CdseS3Signer("ak", "sk").sign_href(
            "https://eodata.dataspace.copernicus.eu/Sentinel-2/foo/B04.jp2"
        )
        assert out == "/vsis3/eodata/Sentinel-2/foo/B04.jp2"

    def test_sign_href_https_path_already_eodata(self):
        """An https path already prefixed with eodata/ is not double-prefixed."""
        out = CdseS3Signer("ak", "sk").sign_href(
            "https://eodata.dataspace.copernicus.eu/eodata/foo/B04.jp2"
        )
        assert out == "/vsis3/eodata/foo/B04.jp2"

    def test_sign_href_passes_foreign_host_through(self):
        """An https href on an unrelated host is returned unchanged."""
        out = CdseS3Signer("ak", "sk").sign_href("https://example.com/a.tif")
        assert out == "https://example.com/a.tif"

    def test_sign_href_non_http_scheme_passthrough(self):
        """A non-s3, non-http href is returned unchanged."""
        assert CdseS3Signer("ak", "sk").sign_href("/vsicurl/local.tif") == "/vsicurl/local.tif"

    def test_gdal_env_carries_s3_credentials_no_authorization(self):
        """gdal_env supplies the S3 endpoint + keys and never an Authorization header."""
        env = CdseS3Signer("ak", "sk", endpoint="eodata.example").gdal_env()
        assert env["AWS_S3_ENDPOINT"] == "eodata.example"
        assert env["AWS_ACCESS_KEY_ID"] == "ak"
        assert env["AWS_SECRET_ACCESS_KEY"] == "sk"
        assert env["AWS_VIRTUAL_HOSTING"] == "FALSE"
        assert "GDAL_HTTP_HEADERS" not in env and "Authorization" not in str(env)


@pytest.mark.stac
class TestPlanetaryComputerSigner:
    """The native PC SAS signer mints + appends tokens without the SDK."""

    def test_name_and_empty_gdal_env(self):
        """The credential rides the URL, so the GDAL env is empty."""
        signer = PlanetaryComputerSigner()
        assert signer.name == "planetary-computer"
        assert signer.gdal_env() == {}

    def test_non_pc_href_passthrough(self):
        """A non-PC href is returned unchanged."""
        assert (
            PlanetaryComputerSigner().sign_href("https://example.com/a.tif")
            == "https://example.com/a.tif"
        )

    def test_already_signed_passthrough(self):
        """An href already carrying SAS query keys is left as-is."""
        signed = "https://x.blob.core.windows.net/c/b.tif?se=2034&sig=abc"
        assert PlanetaryComputerSigner().sign_href(signed) == signed

    def test_public_bucket_never_signed(self):
        """The public ai4edatasetspublicassets bucket is never signed."""
        pub = "https://ai4edatasetspublicassets.blob.core.windows.net/c/b.tif"
        assert PlanetaryComputerSigner().sign_href(pub) == pub

    def test_blob_href_gets_token_appended_and_cached(self, monkeypatch):
        """A PC blob href gets its SAS token appended; tokens are cached per container."""
        signer = PlanetaryComputerSigner()
        calls = {"n": 0}

        def _fetch(account, container):
            calls["n"] += 1
            return "se=x&sig=y", time.time() + 3600.0

        monkeypatch.setattr(signer, "_fetch_token", _fetch)
        href = "https://acct.blob.core.windows.net/cont/blob.tif"
        out = signer.sign_href(href)
        assert out == href + "?se=x&sig=y"
        signer.sign_href(href)
        assert calls["n"] == 1

    def test_fetch_token_reads_pc_endpoint(self, monkeypatch):
        """_fetch_token GETs the token + parses the msft:expiry epoch."""
        _patch_urlopen(
            monkeypatch, [{"token": "se=tok", "msft:expiry": "2099-01-01T00:00:00Z"}]
        )
        token, expiry = PlanetaryComputerSigner()._fetch_token("acct", "cont")
        assert token == "se=tok"
        assert expiry > time.time()


@pytest.mark.stac
class TestEarthdataSigner:
    """The EDL bearer signer uses a static token or mints one over HTTP Basic."""

    def test_static_token_in_gdal_env(self):
        """A pre-minted token is sent in the GDAL Authorization header."""
        signer = EarthdataSigner(token="edl-tok")
        assert signer.gdal_env()["GDAL_HTTP_HEADERS"] == "Authorization: Bearer edl-tok"

    def test_minted_token_used_when_no_static(self, monkeypatch):
        """With credentials and no static token, a token is minted and used."""
        _patch_urlopen(
            monkeypatch,
            [{"access_token": "minted", "expiration_date": "2099-01-01T00:00:00Z"}],
        )
        signer = EarthdataSigner(username="u", password="p")
        assert "Bearer minted" in signer.gdal_env()["GDAL_HTTP_HEADERS"]

    def test_missing_credentials_raises(self, monkeypatch):
        """No token and no username/password raises a clear ValueError."""
        for var in ("EARTHDATA_TOKEN", "EARTHDATA_PAT", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="EARTHDATA_USERNAME"):
            EarthdataSigner().gdal_env()


@pytest.mark.stac
class TestCDSESigner:
    """The CDSE Keycloak bearer signer mints + refreshes an access token."""

    def test_password_grant_mints_token(self, monkeypatch):
        """A password grant yields a bearer header from the minted access token."""
        _patch_urlopen(
            monkeypatch,
            [{"access_token": "acc", "refresh_token": "ref", "expires_in": 600}],
        )
        signer = CDSESigner(username="u", password="p")
        assert signer.gdal_env()["GDAL_HTTP_HEADERS"] == "Authorization: Bearer acc"

    def test_missing_credentials_raises(self, monkeypatch):
        """No username/password raises a clear ValueError."""
        for var in ("CDSE_USERNAME", "CDSE_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="CDSE_USERNAME"):
            CDSESigner().gdal_env()

    def test_bearer_is_not_url_side(self):
        """sign_href is identity — CDSE bearer auth is header-side."""
        assert CDSESigner(username="u", password="p").sign_href("x") == "x"


@pytest.mark.stac
class TestBuildSigner:
    """`build_signer` dispatches a catalog signer name to the right object."""

    def test_anonymous(self, fake_pyramids):
        """The anonymous name resolves to the pyramids AnonymousSigner."""
        assert build_signer("anonymous").name == "anonymous"

    def test_aws_requester_pays_forwards_region(self, fake_pyramids):
        """The requester-pays name resolves to the pyramids signer with the region."""
        signer = build_signer("aws-requester-pays", region="us-west-2")
        assert signer.name == "aws-requester-pays"
        assert signer.region == "us-west-2"

    def test_mpc_sas_resolves_to_local_signer(self):
        """The mpc-sas name resolves to earthlens' own PlanetaryComputerSigner."""
        signer = build_signer("mpc-sas")
        assert isinstance(signer, PlanetaryComputerSigner)
        assert signer.name == "planetary-computer"

    def test_earthdata_from_kwargs(self):
        """The earthdata name resolves to EarthdataSigner using a supplied token."""
        signer = build_signer("earthdata", token="edl-tok")
        assert isinstance(signer, EarthdataSigner)
        assert "Bearer edl-tok" in signer.gdal_env()["GDAL_HTTP_HEADERS"]

    def test_cdse_resolves_to_bearer_signer(self):
        """The cdse name resolves to the CDSE Keycloak bearer signer."""
        signer = build_signer("cdse", username="u", password="p")
        assert isinstance(signer, CDSESigner)
        assert signer.name == "cdse"

    def test_cdse_s3_from_kwargs(self):
        """The cdse-s3 name resolves to CdseS3Signer using the supplied keys."""
        signer = build_signer("cdse-s3", access_key="ak", secret_key="sk")
        assert isinstance(signer, CdseS3Signer)
        assert signer.gdal_env()["AWS_ACCESS_KEY_ID"] == "ak"

    def test_unknown_signer_raises(self):
        """An unknown signer name raises ValueError naming the choices."""
        with pytest.raises(ValueError, match="unknown signer_type"):
            build_signer("nope")


@pytest.mark.stac
class TestS3Credentials:
    """`auth_cdse.s3_credentials` resolves CDSE S3 keys (kwarg -> env)."""

    def test_kwargs_take_priority(self, monkeypatch):
        """Explicit kwargs are returned even when env vars are also set."""
        monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "env-ak")
        monkeypatch.setenv("CDSE_S3_SECRET_KEY", "env-sk")
        assert auth_cdse.s3_credentials(access_key="ak", secret_key="sk") == ("ak", "sk")

    def test_env_fallback(self, monkeypatch):
        """The env vars supply the keys when no kwargs are given."""
        monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "env-ak")
        monkeypatch.setenv("CDSE_S3_SECRET_KEY", "env-sk")
        assert auth_cdse.s3_credentials() == ("env-ak", "env-sk")

    def test_missing_both_raises_authentication_error(self, monkeypatch):
        """Missing keys raise AuthenticationError naming the dashboard URL."""
        for var in ("CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY", "CDSE_USERNAME", "CDSE_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(AuthenticationError, match="dataspace.copernicus.eu"):
            auth_cdse.s3_credentials()

    def test_extra_kwargs_ignored(self, monkeypatch):
        """Unrelated kwargs forwarded by build_signer are ignored."""
        assert auth_cdse.s3_credentials(access_key="ak", secret_key="sk", region="x") == (
            "ak",
            "sk",
        )
