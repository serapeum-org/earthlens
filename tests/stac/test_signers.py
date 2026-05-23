"""Unit tests for `earthlens.stac.signers`."""

from __future__ import annotations

import pytest

from earthlens.base import AuthenticationError
from earthlens.stac import auth_cdse
from earthlens.stac.signers import CdseS3Signer, MpcSasSigner, build_signer


@pytest.mark.stac
class TestMpcSasSigner:
    """The MPC signer signs hrefs/items via the planetary-computer SDK."""

    def test_name(self):
        """The signer reports its catalog label."""
        assert MpcSasSigner().name == "mpc-sas"

    def test_sign_request_is_noop(self):
        """Search is anonymous, so sign_request returns None."""
        assert MpcSasSigner().sign_request(object()) is None

    def test_sign_href_calls_pc_sign(self, fake_pc):
        """sign_href delegates to planetary_computer.sign."""
        out = MpcSasSigner().sign_href("https://host/a.tif")
        assert out == "https://host/a.tif?sas=token"
        assert fake_pc.sign_calls == ["https://host/a.tif"]

    def test_sign_item_returns_none_and_calls_sign_inplace(self, fake_pc):
        """sign_item mutates in place via sign_inplace and returns None."""
        item = object()
        assert MpcSasSigner().sign_item(item) is None
        assert fake_pc.sign_inplace_calls == [item]

    def test_gdal_env_is_empty(self):
        """The SAS credential lives in the URL, so the GDAL env is empty."""
        assert MpcSasSigner().gdal_env() == {}


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

    def test_sign_href_passes_other_schemes_through(self):
        """A non-s3 href is returned unchanged."""
        out = CdseS3Signer("ak", "sk").sign_href("https://host/a.tif")
        assert out == "https://host/a.tif"

    def test_gdal_env_carries_s3_credentials_no_authorization(self):
        """gdal_env supplies the S3 endpoint + keys and never an Authorization header."""
        env = CdseS3Signer("ak", "sk", endpoint="eodata.example").gdal_env()
        assert env["AWS_S3_ENDPOINT"] == "eodata.example"
        assert env["AWS_ACCESS_KEY_ID"] == "ak"
        assert env["AWS_SECRET_ACCESS_KEY"] == "sk"
        assert env["AWS_VIRTUAL_HOSTING"] == "FALSE"
        assert "GDAL_HTTP_HEADERS" not in env and "Authorization" not in str(env)


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

    def test_mpc_sas(self):
        """The mpc-sas name resolves to MpcSasSigner."""
        assert isinstance(build_signer("mpc-sas"), MpcSasSigner)

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
