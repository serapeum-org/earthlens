"""Tests for earthlens's thin STAC signer adapter over pyramids-eo.

The signer behaviour (token minting, SAS grafting, the CDSE S3 rewrite, the
opt-in-region endpoint) is pyramids-eo's and is tested there. These cover only
earthlens's own responsibility: `build_signer` selecting the right signer and
wrapping it so an auth failure raises `earthlens.base.AuthenticationError`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pyramids.stac.signers import Signer
from pyramids_eo.errors import AuthenticationError as EoAuthError

from earthlens.base import AuthenticationError
from earthlens.stac import build_signer
from earthlens.stac.signers import _ContractSigner


class _RaisingInner:
    """A fake pyramids-eo signer whose every minting call raises the EO error."""

    name = "fake"

    def sign_href(self, href: str) -> str:
        raise EoAuthError("boom-href")

    def sign_item(self, item: Any) -> None:
        raise EoAuthError("boom-item")

    def sign_request(self, request: Any) -> Any:
        raise EoAuthError("boom-request")

    def gdal_env(self) -> dict[str, str]:
        raise EoAuthError("boom-env")


class _PassingInner:
    """A fake pyramids-eo signer whose calls succeed, covering the pass-through paths."""

    name = "fake-ok"

    def sign_href(self, href: str) -> str:
        return f"signed:{href}"

    def sign_item(self, item: Any) -> None:
        return None

    def sign_request(self, request: Any) -> Any:
        return request

    def gdal_env(self) -> dict[str, str]:
        return {"AWS_NO_SIGN_REQUEST": "YES"}


@pytest.mark.stac
class TestBuildSignerDispatch:
    """`build_signer` selects the right pyramids-eo signer and wraps it."""

    @pytest.mark.parametrize(
        "signer_type, creds, expected_name",
        [
            ("anonymous", {}, "anonymous"),
            ("aws-requester-pays", {}, "aws-requester-pays"),
            ("mpc-sas", {}, "planetary-computer"),
            ("cdse-s3", {"access_key": "ak", "secret_key": "sk"}, "cdse-s3"),
            ("bdc-token", {"token": "tok"}, "bdc-token"),
        ],
    )
    def test_selects_the_right_signer(self, signer_type, creds, expected_name):
        """Each signer key resolves to the signer carrying the matching name."""
        assert build_signer(signer_type, **creds).name == expected_name

    def test_result_is_the_contract_wrapper(self):
        """The returned signer is the earthlens contract wrapper, not the raw one."""
        assert isinstance(build_signer("anonymous"), _ContractSigner)

    def test_unknown_signer_type_raises_value_error(self):
        """An unrecognised signer key is rejected with ValueError, not a silent fallback."""
        with pytest.raises(ValueError, match="unknown signer_type"):
            build_signer("nope")

    @pytest.mark.parametrize(
        "signer_type", ["anonymous", "aws-requester-pays", "mpc-sas"]
    )
    def test_wrapper_satisfies_the_signer_protocol(self, signer_type):
        """The wrapped signer still passes isinstance against pyramids' runtime_checkable Signer."""
        assert isinstance(build_signer(signer_type), Signer)

    def test_anonymous_gdal_env_is_unsigned(self):
        """The anonymous signer still opts out of request signing."""
        assert build_signer("anonymous").gdal_env()["AWS_NO_SIGN_REQUEST"] == "YES"

    def test_anonymous_opt_in_region_pins_the_endpoint(self):
        """An opt-in region is forwarded through to the region-pinned endpoint."""
        env = build_signer("anonymous", region="af-south-1").gdal_env()
        assert env["AWS_REGION"] == "af-south-1"
        assert "af-south-1" in env["AWS_S3_ENDPOINT"]

    def test_cdse_s3_gdal_env_has_endpoint_and_no_stopgap(self):
        """The CDSE signer carries the eodata endpoint and no thread stopgap."""
        env = build_signer("cdse-s3", access_key="ak", secret_key="sk").gdal_env()
        assert env["AWS_S3_ENDPOINT"] == "eodata.dataspace.copernicus.eu"
        assert "GDAL_NUM_THREADS" not in env


@pytest.mark.stac
class TestAuthContract:
    """A STAC auth failure surfaces as `earthlens.base.AuthenticationError`."""

    def test_cdse_s3_missing_credentials_raises_at_construction(self, monkeypatch):
        """Missing CDSE keys raise earthlens's error, naming the dashboard host."""
        for var in ("CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY", "CDSE_USERNAME"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(AuthenticationError, match="dataspace.copernicus.eu"):
            build_signer("cdse-s3")

    def test_bdc_missing_token_raises_on_use(self, monkeypatch):
        """A missing BDC token surfaces as earthlens's error when a href is signed."""
        monkeypatch.delenv("BDC_ACCESS_TOKEN", raising=False)
        signer = build_signer("bdc-token")
        with pytest.raises(AuthenticationError, match="BDC_ACCESS_TOKEN"):
            signer.sign_href("https://data.inpe.br/bdc/data/x.tif")


@pytest.mark.stac
class TestContractWrapper:
    """`_ContractSigner` maps every pyramids-eo auth failure to earthlens's type."""

    def test_sign_href_is_translated(self):
        """A pyramids-eo auth error from sign_href becomes earthlens's."""
        signer = _ContractSigner(_RaisingInner())
        with pytest.raises(AuthenticationError, match="boom-href"):
            signer.sign_href("h")

    def test_sign_item_is_translated(self):
        """A pyramids-eo auth error from sign_item becomes earthlens's."""
        signer = _ContractSigner(_RaisingInner())
        with pytest.raises(AuthenticationError, match="boom-item"):
            signer.sign_item(object())

    def test_sign_request_is_translated(self):
        """A pyramids-eo auth error from sign_request becomes earthlens's."""
        signer = _ContractSigner(_RaisingInner())
        with pytest.raises(AuthenticationError, match="boom-request"):
            signer.sign_request(object())

    def test_gdal_env_is_translated(self):
        """A pyramids-eo auth error from gdal_env becomes earthlens's."""
        signer = _ContractSigner(_RaisingInner())
        with pytest.raises(AuthenticationError, match="boom-env"):
            signer.gdal_env()

    def test_sign_href_passes_through_on_success(self):
        """A successful sign_href returns the wrapped signer's result unchanged."""
        assert _ContractSigner(_PassingInner()).sign_href("h") == "signed:h"

    def test_sign_item_returns_none_on_success(self):
        """A successful sign_item is a no-op returning None."""
        assert _ContractSigner(_PassingInner()).sign_item(object()) is None

    def test_sign_request_passes_through_on_success(self):
        """A successful sign_request returns the wrapped signer's result unchanged."""
        sentinel = object()
        assert _ContractSigner(_PassingInner()).sign_request(sentinel) is sentinel

    def test_other_attributes_delegate(self):
        """Non-signing attributes pass straight through to the wrapped signer."""
        assert _ContractSigner(_PassingInner()).name == "fake-ok"

    def test_getattr_does_not_recurse_before_inner_is_set(self):
        """Attribute access before _inner is set raises AttributeError, not recursion."""
        bare = _ContractSigner.__new__(_ContractSigner)
        with pytest.raises(AttributeError):
            getattr(bare, "name")
