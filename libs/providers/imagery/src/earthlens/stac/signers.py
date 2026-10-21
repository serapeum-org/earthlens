"""earthlens-side adapter over pyramids-eo's STAC asset signers.

The provider signer logic — Planetary Computer SAS grafting, the Earthdata and
CDSE bearer-token mints, the CDSE S3 endpoint rewrite, the Brazil Data Cube
token, and the anonymous / opt-in-region signer — lives in `pyramids_eo.stac`.
earthlens consumes those classes directly rather than keeping a second copy, so
there is only one home for the signing logic to harden and evolve.

The one earthlens-specific concern kept here is the framework's exception
contract: every earthlens backend raises `earthlens.base.AuthenticationError` on
an auth failure, so `build_signer` returns signers whose credential-minting calls
re-raise pyramids-eo's `AuthenticationError` as that single type. A consumer's
`except earthlens.base.AuthenticationError` therefore still covers STAC, as it
covers every other backend.
"""

from __future__ import annotations

from typing import Any, cast

from pyramids_eo.errors import AuthenticationError as _PyramidsEoAuthError
from pyramids_eo.stac import (
    BdcTokenSigner,
    CdseS3Signer,
    CDSESigner,
    EarthdataSigner,
    PlanetaryComputerSigner,
)
from pyramids_eo.stac import (
    build_signer as _eo_build_signer,
)

from earthlens.base import AuthenticationError

__all__ = [
    "BdcTokenSigner",
    "CDSESigner",
    "CdseS3Signer",
    "EarthdataSigner",
    "PlanetaryComputerSigner",
    "build_signer",
]


class _ContractSigner:
    """Delegate to a pyramids-eo signer, mapping its auth errors to earthlens's.

    pyramids-eo's signers raise `pyramids_eo.errors.AuthenticationError`; the
    earthlens framework contract is a single `earthlens.base.AuthenticationError`
    across every backend. The calls that mint credentials — and can therefore
    fail — are `sign_href`, `sign_item`, `sign_request` and `gdal_env` (a bearer
    signer mints its token when `gdal_env` is read); each re-raises as earthlens's
    type. Every other attribute delegates to the wrapped signer unchanged, so the
    wrapper still satisfies pyramids' structural `Signer` protocol.

    Args:
        inner: The pyramids-eo signer to wrap.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """Delegate any attribute this wrapper does not define to the signer."""
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def sign_href(self, href: str) -> str:
        """Sign `href`, re-raising a pyramids-eo auth failure as earthlens's.

        Args:
            href: The asset href to sign.

        Returns:
            The signed href.

        Raises:
            AuthenticationError: When the wrapped signer cannot mint the
                credential the href needs.
        """
        try:
            result = self._inner.sign_href(href)
        except _PyramidsEoAuthError as exc:
            raise AuthenticationError(str(exc)) from exc
        return cast("str", result)

    def sign_item(self, item: Any) -> None:
        """Sign a STAC item in place, re-raising auth failures as earthlens's.

        Args:
            item: The STAC item (or item collection / mapping) to sign.

        Raises:
            AuthenticationError: When the wrapped signer cannot mint the
                credential the item's assets need.
        """
        try:
            self._inner.sign_item(item)
        except _PyramidsEoAuthError as exc:
            raise AuthenticationError(str(exc)) from exc

    def sign_request(self, request: Any) -> Any:
        """Sign a request, re-raising a pyramids-eo auth failure as earthlens's.

        Args:
            request: The request object to sign.

        Returns:
            The signed request, or `None` when the signer does not sign requests.

        Raises:
            AuthenticationError: When the wrapped signer cannot mint the
                credential the request needs.
        """
        try:
            result = self._inner.sign_request(request)
        except _PyramidsEoAuthError as exc:
            raise AuthenticationError(str(exc)) from exc
        return result

    def gdal_env(self) -> dict[str, str]:
        """Return the signer's GDAL config, re-raising auth failures as earthlens's.

        Returns:
            The GDAL configuration the signed read needs.

        Raises:
            AuthenticationError: When a bearer signer cannot mint the token its
                `GDAL_HTTP_HEADERS` entry carries.
        """
        try:
            result = self._inner.gdal_env()
        except _PyramidsEoAuthError as exc:
            raise AuthenticationError(str(exc)) from exc
        return cast("dict[str, str]", result)


def build_signer(signer_type: str, **creds: Any) -> Any:
    """Build a pyramids-eo signer wrapped to honour earthlens's auth contract.

    Delegates to `pyramids_eo.stac.build_signer` and wraps the result so a STAC
    auth failure surfaces as `earthlens.base.AuthenticationError` — the single
    exception every earthlens backend raises — rather than pyramids-eo's own
    `AuthenticationError`.

    Args:
        signer_type: One of `anonymous`, `aws-requester-pays`, `mpc-sas`,
            `earthdata`, `cdse`, `cdse-s3`, `bdc-token`.
        **creds: Credential keyword arguments forwarded verbatim; each signer
            whitelists the ones it accepts.

    Returns:
        A signer satisfying pyramids' `Signer` protocol whose auth failures are
        `earthlens.base.AuthenticationError`.

    Raises:
        AuthenticationError: When the signer cannot be constructed because a
            required credential is absent.

    Examples:
        - The anonymous signer opts a read out of request signing:
            ```python
            >>> from earthlens.stac import build_signer
            >>> signer = build_signer("anonymous")
            >>> signer.name
            'anonymous'
            >>> signer.gdal_env()["AWS_NO_SIGN_REQUEST"]
            'YES'

            ```
        - An opt-in AWS region is pinned to its region-specific endpoint:
            ```python
            >>> from earthlens.stac import build_signer
            >>> env = build_signer("anonymous", region="af-south-1").gdal_env()
            >>> env["AWS_REGION"]
            'af-south-1'

            ```
        - The CDSE S3 signer carries the eodata endpoint for `/vsis3` reads:
            ```python
            >>> from earthlens.stac import build_signer
            >>> env = build_signer("cdse-s3", access_key="ak", secret_key="sk").gdal_env()
            >>> env["AWS_S3_ENDPOINT"]
            'eodata.dataspace.copernicus.eu'

            ```
    """
    try:
        inner = _eo_build_signer(signer_type, **creds)
    except _PyramidsEoAuthError as exc:
        raise AuthenticationError(str(exc)) from exc
    return _ContractSigner(inner)
