"""Unit tests for `earthlens.stac.signers`."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from earthlens.base import AuthenticationError
from earthlens.stac import auth_cdse
from earthlens.stac.signers import (
    BdcTokenSigner,
    CdseS3Signer,
    CDSESigner,
    EarthdataSigner,
    PlanetaryComputerSigner,
    _BearerProviderSigner,
    build_signer,
)

from .conftest import make_item

_EARTHDATA_ENV = (
    "EARTHDATA_TOKEN",
    "EARTHDATA_PAT",
    "EARTHDATA_USERNAME",
    "EARTHDATA_PASSWORD",
)
_CDSE_ENV = ("CDSE_USERNAME", "CDSE_PASSWORD")


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
    """Patch urllib.request.urlopen to pop successive JSON payloads; record calls."""
    return _patch_urlopen_actions(monkeypatch, payloads)


def _patch_urlopen_actions(monkeypatch, actions):
    """Patch urlopen to walk a sequence of JSON payloads or exceptions to raise."""
    queue = list(actions)
    calls = {"n": 0, "requests": []}

    def _fake(request, timeout=None):
        calls["n"] += 1
        calls["requests"].append(request)
        action = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(action, Exception):
            raise action
        return _FakeResponse(action)

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
        assert (
            CdseS3Signer("ak", "sk").sign_href("/vsicurl/local.tif")
            == "/vsicurl/local.tif"
        )

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

    def test_subscription_key_sets_apim_header(self, monkeypatch):
        """A subscription key is sent as the Ocp-Apim-Subscription-Key header."""
        calls = _patch_urlopen(monkeypatch, [{"token": "se=t", "msft:expiry": None}])
        PlanetaryComputerSigner(subscription_key="sub-key")._fetch_token("acct", "cont")
        assert calls["requests"][0].get_header("Ocp-apim-subscription-key") == "sub-key"

    def test_sign_request_is_noop(self):
        """Search is anonymous — sign_request leaves the request unchanged."""
        assert PlanetaryComputerSigner().sign_request(object()) is None

    def test_sign_item_rewrites_item_object(self):
        """sign_item rewrites each asset href on an Item with an assets mapping."""
        item = make_item("a", "2024-01-05", {"red": "https://example.com/a.tif"})
        assert PlanetaryComputerSigner().sign_item(item) is None
        assert item.assets["red"].href == "https://example.com/a.tif"

    def test_sign_item_rewrites_item_collection(self):
        """sign_item iterates an ItemCollection (an iterable of Items)."""
        items = [
            make_item("a", "2024-01-05", {"b": "https://h/a.tif"}),
            make_item("b", "2024-01-05", {"b": "https://h/b.tif"}),
        ]
        assert PlanetaryComputerSigner().sign_item(iter(items)) is None

    def test_sign_item_rewrites_raw_dict(self):
        """sign_item rewrites hrefs on a raw-dict Item via the dict branch."""
        item = {"assets": {"red": {"href": "https://h/a.tif"}}}
        PlanetaryComputerSigner().sign_item(item)
        assert item["assets"]["red"]["href"] == "https://h/a.tif"

    def test_sign_item_skips_item_without_assets(self):
        """An Item whose assets are empty is skipped without error."""
        assert (
            PlanetaryComputerSigner().sign_item(make_item("x", "2024-01-05", {}))
            is None
        )

    def test_sign_item_handles_non_iterable(self):
        """A non-iterable, asset-less argument is treated as a single item."""
        assert PlanetaryComputerSigner().sign_item(42) is None

    def test_sign_item_skips_asset_without_href(self):
        """An asset object whose href is None is left untouched."""
        item = make_item("x", "2024-01-05", {"red": "placeholder"})
        item.assets["red"].href = None
        PlanetaryComputerSigner().sign_item(item)
        assert item.assets["red"].href is None

    def test_blob_href_without_container_passes_through(self):
        """A blob URL with no container path segment is not signed."""
        href = "https://acct.blob.core.windows.net/"
        assert PlanetaryComputerSigner().sign_href(href) == href

    def test_parse_expiry_non_string_is_past(self):
        """A non-string expiry yields a past epoch so the token refetches."""
        assert PlanetaryComputerSigner._parse_expiry(None) == 0.0

    def test_parse_expiry_unparseable_is_past(self):
        """An unparseable expiry string yields a past epoch."""
        assert PlanetaryComputerSigner._parse_expiry("not-a-date") == 0.0

    def test_parse_expiry_naive_datetime_assumed_utc(self):
        """A naive (tz-less) expiry timestamp is read as UTC."""
        assert (
            PlanetaryComputerSigner._parse_expiry("2099-01-01T00:00:00") > time.time()
        )


@pytest.mark.stac
class TestEarthdataSigner:
    """The EDL bearer signer uses a static token or mints one over HTTP Basic."""

    def test_static_token_in_gdal_env(self):
        """A pre-minted token is sent in the GDAL Authorization header."""
        signer = EarthdataSigner(token="edl-tok")
        assert signer.gdal_env()["GDAL_HTTP_HEADERS"] == "Authorization: Bearer edl-tok"

    def test_minted_token_used_when_no_static(self, monkeypatch):
        """With credentials and no static token, a token is minted and used."""
        for var in _EARTHDATA_ENV:
            monkeypatch.delenv(var, raising=False)
        _patch_urlopen(
            monkeypatch,
            [{"access_token": "minted", "expiration_date": "2099-01-01T00:00:00Z"}],
        )
        signer = EarthdataSigner(username="u", password="p")
        assert "Bearer minted" in signer.gdal_env()["GDAL_HTTP_HEADERS"]

    def test_missing_credentials_raises(self, monkeypatch):
        """No token and no username/password raises a clear ValueError."""
        for var in _EARTHDATA_ENV:
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="EARTHDATA_USERNAME"):
            EarthdataSigner().gdal_env()

    def test_pat_env_used_as_static_token(self, monkeypatch):
        """A token in EARTHDATA_PAT is used directly without minting."""
        for var in _EARTHDATA_ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("EARTHDATA_PAT", "pat-tok")
        assert "Bearer pat-tok" in EarthdataSigner().gdal_env()["GDAL_HTTP_HEADERS"]

    def test_sign_request_sets_bearer_header(self):
        """sign_request stamps the Authorization header on an outgoing request."""
        request = SimpleNamespace(headers={})
        EarthdataSigner(token="t").sign_request(request)
        assert request.headers["Authorization"] == "Bearer t"

    def test_sign_href_and_item_are_noops(self):
        """The EDL signer authenticates header-side, so href/item are unchanged."""
        signer = EarthdataSigner(token="t")
        assert signer.sign_href("https://x/a.tif") == "https://x/a.tif"
        assert signer.sign_item(object()) is None

    def test_parse_expiry_valid_string(self):
        """A valid expiration_date string parses to a future epoch."""
        assert EarthdataSigner._parse_expiry("2099-01-01T00:00:00Z") > time.time()

    def test_parse_expiry_naive_datetime_assumed_utc(self):
        """A naive (tz-less) expiration_date is read as UTC."""
        assert EarthdataSigner._parse_expiry("2099-01-01T00:00:00") > time.time()

    def test_parse_expiry_unparseable_defaults_to_hour(self):
        """An unparseable expiry defaults to roughly now + 1h."""
        assert EarthdataSigner._parse_expiry("nope") > time.time()

    def test_parse_expiry_non_string_defaults_to_hour(self):
        """A non-string expiry defaults to roughly now + 1h."""
        assert EarthdataSigner._parse_expiry(None) > time.time()


@pytest.mark.stac
class TestCDSESigner:
    """The CDSE Keycloak bearer signer mints + refreshes an access token."""

    def test_password_grant_mints_token(self, monkeypatch):
        """A password grant yields a bearer header from the minted access token."""
        for var in _CDSE_ENV:
            monkeypatch.delenv(var, raising=False)
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

    def test_token_is_cached_across_calls(self, monkeypatch):
        """A minted access token is reused until near expiry (one network call)."""
        for var in _CDSE_ENV:
            monkeypatch.delenv(var, raising=False)
        calls = _patch_urlopen(
            monkeypatch,
            [{"access_token": "acc", "refresh_token": "r", "expires_in": 600}],
        )
        signer = CDSESigner(username="u", password="p")
        signer.gdal_env()
        signer.gdal_env()
        assert calls["n"] == 1

    def test_refresh_grant_used_when_refresh_token_held(self, monkeypatch):
        """Holding a refresh token mints via the refresh grant and rotates it."""
        for var in _CDSE_ENV:
            monkeypatch.delenv(var, raising=False)
        calls = _patch_urlopen(
            monkeypatch,
            [{"access_token": "acc2", "refresh_token": "rot", "expires_in": 600}],
        )
        signer = CDSESigner(username="u", password="p")
        signer._refresh_token = "old-refresh"
        token, _ = signer._fetch_token()
        assert token == "acc2"
        assert signer._refresh_token == "rot"
        body = calls["requests"][0].data.decode()
        assert "grant_type=refresh_token" in body

    def test_expired_refresh_falls_back_to_password(self, monkeypatch):
        """A rejected refresh token is dropped and a password grant is attempted."""
        for var in _CDSE_ENV:
            monkeypatch.delenv(var, raising=False)
        calls = _patch_urlopen_actions(
            monkeypatch,
            [
                urllib.error.URLError("refresh rejected"),
                {"access_token": "pw", "refresh_token": "r2", "expires_in": 600},
            ],
        )
        signer = CDSESigner(username="u", password="p")
        signer._refresh_token = "stale"
        token, _ = signer._fetch_token()
        assert token == "pw"
        assert calls["n"] == 2
        assert "grant_type=password" in calls["requests"][1].data.decode()

    def test_sign_request_sets_bearer_header(self, monkeypatch):
        """sign_request stamps the bearer header from the minted token."""
        for var in _CDSE_ENV:
            monkeypatch.delenv(var, raising=False)
        _patch_urlopen(
            monkeypatch,
            [{"access_token": "acc", "refresh_token": "r", "expires_in": 600}],
        )
        request = SimpleNamespace(headers={})
        CDSESigner(username="u", password="p").sign_request(request)
        assert request.headers["Authorization"] == "Bearer acc"


@pytest.mark.stac
class TestBearerProviderSigner:
    """The shared bearer base leaves token minting to its subclasses."""

    def test_fetch_token_not_implemented(self):
        """The base _fetch_token is abstract and must be overridden."""
        with pytest.raises(NotImplementedError):
            _BearerProviderSigner()._fetch_token()


@pytest.mark.stac
class TestBuildSigner:
    """`build_signer` dispatches a catalog signer name to the right object."""

    def test_anonymous(self, fake_pyramids):
        """The anonymous name resolves to the pyramids AnonymousSigner."""
        assert build_signer("anonymous").name == "anonymous"

    def test_anonymous_no_region_gdal_env_is_no_sign_only(self, fake_pyramids):
        """Without a region the anonymous signer emits only the no-sign flag."""
        assert build_signer("anonymous").gdal_env() == {"AWS_NO_SIGN_REQUEST": "YES"}

    def test_anonymous_forwards_region_to_gdal_env(self, fake_pyramids):
        """A region pins GDAL at that region's S3 endpoint (opt-in regions need this)."""
        env = build_signer("anonymous", region="af-south-1").gdal_env()
        assert env == {
            "AWS_NO_SIGN_REQUEST": "YES",
            "AWS_REGION": "af-south-1",
            "AWS_S3_ENDPOINT": "s3.af-south-1.amazonaws.com",
        }

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

    def test_earthdata_tolerates_backend_s3_kwargs(self):
        """build_signer drops the region/S3 kwargs the backend always forwards."""
        signer = build_signer(
            "earthdata", token="t", region="us-west-2", access_key="ak", secret_key="sk"
        )
        assert isinstance(signer, EarthdataSigner)
        assert "Bearer t" in signer.gdal_env()["GDAL_HTTP_HEADERS"]

    def test_cdse_tolerates_backend_s3_kwargs(self):
        """build_signer drops the region/S3 kwargs the backend always forwards."""
        signer = build_signer(
            "cdse",
            username="u",
            password="p",
            region="eu",
            access_key="ak",
            secret_key="sk",
        )
        assert isinstance(signer, CDSESigner)
        assert signer.name == "cdse"

    def test_cdse_s3_from_kwargs(self):
        """The cdse-s3 name resolves to CdseS3Signer using the supplied keys."""
        signer = build_signer("cdse-s3", access_key="ak", secret_key="sk")
        assert isinstance(signer, CdseS3Signer)
        assert signer.gdal_env()["AWS_ACCESS_KEY_ID"] == "ak"

    def test_bdc_token_from_kwargs(self):
        """The bdc-token name resolves to BdcTokenSigner forwarding the token."""
        signer = build_signer("bdc-token", token="abc")
        assert isinstance(signer, BdcTokenSigner)
        assert signer.name == "bdc-token"

    def test_bdc_token_from_env(self, monkeypatch):
        """build_signer('bdc-token') without kwargs falls back to $BDC_ACCESS_TOKEN."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "env-tok")
        signer = build_signer("bdc-token")
        assert (
            signer.sign_href("https://x/y.tif")
            == "https://x/y.tif?access_token=env-tok"
        )

    def test_unknown_signer_raises(self):
        """An unknown signer name raises ValueError naming the choices."""
        with pytest.raises(ValueError, match="unknown signer_type"):
            build_signer("nope")


@pytest.mark.stac
class TestBdcTokenSigner:
    """`BdcTokenSigner` appends ?access_token=… to BDC asset hrefs."""

    def test_name(self):
        """The signer reports the bdc-token catalog label."""
        assert BdcTokenSigner(token="tok").name == "bdc-token"

    def test_query_separator_when_no_existing_query(self, monkeypatch):
        """A clean https href gets `?access_token=…` appended."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "tok")
        out = BdcTokenSigner().sign_href("https://data.inpe.br/bdc/data/x.tif")
        assert out == "https://data.inpe.br/bdc/data/x.tif?access_token=tok"

    def test_query_separator_when_query_already_present(self, monkeypatch):
        """An href that already carries a query gets `&access_token=…` instead."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "tok")
        out = BdcTokenSigner().sign_href("https://data.inpe.br/x.tif?foo=1")
        assert out == "https://data.inpe.br/x.tif?foo=1&access_token=tok"

    def test_fragment_does_not_get_token_appended_after_it(self, monkeypatch):
        """An href with a `#fragment` keeps the fragment last; token lands in the query."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "tok")
        out = BdcTokenSigner().sign_href("https://data.inpe.br/x.tif#frag")
        assert out == "https://data.inpe.br/x.tif?access_token=tok#frag"

    def test_token_is_url_encoded(self, monkeypatch):
        """Tokens with reserved chars are URL-encoded so the query parses correctly."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "tok=&val/+")
        out = BdcTokenSigner().sign_href("https://data.inpe.br/x.tif")
        assert out == "https://data.inpe.br/x.tif?access_token=tok%3D%26val%2F%2B"

    def test_explicit_token_overrides_env(self, monkeypatch):
        """The kwarg token wins over $BDC_ACCESS_TOKEN."""
        monkeypatch.setenv("BDC_ACCESS_TOKEN", "env-tok")
        out = BdcTokenSigner(token="kwarg-tok").sign_href("https://x/y.tif")
        assert out == "https://x/y.tif?access_token=kwarg-tok"

    def test_missing_token_raises_authentication_error(self, monkeypatch):
        """Missing $BDC_ACCESS_TOKEN raises AuthenticationError naming the env var."""
        monkeypatch.delenv("BDC_ACCESS_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="BDC_ACCESS_TOKEN"):
            BdcTokenSigner().sign_href("https://x/y.tif")

    def test_sign_request_and_item_are_noops(self):
        """BDC search is anonymous — sign_request and sign_item return None."""
        signer = BdcTokenSigner(token="tok")
        assert signer.sign_request(object()) is None
        assert signer.sign_item(object()) is None

    def test_gdal_env_is_empty(self):
        """The credential rides in the URL, so the GDAL env stays empty."""
        assert BdcTokenSigner(token="tok").gdal_env() == {}


@pytest.mark.stac
class TestS3Credentials:
    """`auth_cdse.s3_credentials` resolves CDSE S3 keys (kwarg -> env)."""

    def test_kwargs_take_priority(self, monkeypatch):
        """Explicit kwargs are returned even when env vars are also set."""
        monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "env-ak")
        monkeypatch.setenv("CDSE_S3_SECRET_KEY", "env-sk")
        assert auth_cdse.s3_credentials(access_key="ak", secret_key="sk") == (
            "ak",
            "sk",
        )

    def test_env_fallback(self, monkeypatch):
        """The env vars supply the keys when no kwargs are given."""
        monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "env-ak")
        monkeypatch.setenv("CDSE_S3_SECRET_KEY", "env-sk")
        assert auth_cdse.s3_credentials() == ("env-ak", "env-sk")

    def test_missing_both_raises_authentication_error(self, monkeypatch):
        """Missing keys raise AuthenticationError naming the dashboard URL."""
        for var in (
            "CDSE_S3_ACCESS_KEY",
            "CDSE_S3_SECRET_KEY",
            "CDSE_USERNAME",
            "CDSE_PASSWORD",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(AuthenticationError, match="dataspace.copernicus.eu"):
            auth_cdse.s3_credentials()

    def test_extra_kwargs_ignored(self, monkeypatch):
        """Unrelated kwargs forwarded by build_signer are ignored."""
        assert auth_cdse.s3_credentials(
            access_key="ak", secret_key="sk", region="x"
        ) == (
            "ak",
            "sk",
        )
