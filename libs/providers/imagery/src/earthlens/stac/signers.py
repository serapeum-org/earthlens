"""earthlens-side adapter over pyramids-eo's STAC asset signers.

The provider signer logic — Planetary Computer SAS grafting, the Earthdata and
CDSE bearer-token mints, the CDSE S3 endpoint rewrite, the Brazil Data Cube
token, and the anonymous / opt-in-region signer — lives in `pyramids_eo.stac`.
earthlens consumes those classes directly rather than keeping a second copy, so
there is only one home for the signing logic to harden and evolve.

Two earthlens-specific concerns are kept here. The first is the framework's
exception contract: every earthlens backend raises
`earthlens.base.AuthenticationError` on an auth failure, so `build_signer`
returns signers whose credential-minting calls re-raise pyramids-eo's
`AuthenticationError` as that single type. A consumer's
`except earthlens.base.AuthenticationError` therefore still covers STAC, as it
covers every other backend.

The second is which asset mirror a signer has to read — `alternate_preference_for`
and the `_ALTERNATE_PREFERENCE` table below. It belongs on this side of the
boundary because it is an earthlens-level policy, not part of pyramids-eo's
signing algorithm; the backend reads it when resolving each asset href.

Where it should live long-term is not settled. The fact it encodes is a property
of an *endpoint's asset layout*, which the catalog already models per endpoint and
per collection (`Endpoint` / `Collection` carry endpoint-specific details such as
`region`), whereas the table is keyed by *signer name* — so it currently applies
to any collection using `aws-requester-pays`, present or future, whether or not
that collection publishes the same layout. `earth-search/landsat-c2-l2` is the
live counter-example: same signer, canonical hrefs already `s3://`, no `alternate`
block at all. A catalog `alternate:` field, or a `preferred_alternates` attribute
upstreamed to the pyramids-eo signers, would tie the fact to whatever actually
determines it. The name key is kept for now because there is exactly one entry;
`TestAlternatePreference` asserts every key is a name `build_signer` can really
produce, so a typo cannot sit here inert, and this comment is the note to settle
the home before a second entry is added.
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
    "alternate_preference_for",
    "build_signer",
]

#: Which `alternate-assets` href a signer prefers over the asset's canonical one,
#: keyed by signer name. A STAC asset may publish mirrors under `alternate`; the
#: canonical href is not always the one the signer can actually read.
#:
#: `aws-requester-pays` is the case that forced this: every USGS Landsat asset
#: publishes a canonical `https://landsatlook.usgs.gov/...` href *and* an
#: `alternate.s3` href on the requester-pays bucket. The canonical one 302s to the
#: EROS login, which an AWS key pair cannot satisfy, so reading it fails whatever
#: credentials are present; the `s3://` mirror is the readable copy.
#:
#: `cdse-s3` is deliberately absent even though it also reads an S3 store: its own
#: `sign_href` already rewrites the canonical `eodata` href to a `/vsis3/` path, so
#: it reads correctly today and adding a preference here would change a working
#: path with no way to verify it short of live CDSE credentials. The remaining
#: signers (`anonymous`, `mpc-sas`, `earthdata`, `cdse`, `bdc-token`) read
#: publicly-reachable HTTPS hrefs and need no mirror.
_ALTERNATE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "aws-requester-pays": ("s3",),
}


def alternate_preference_for(signer: Any) -> tuple[str, ...] | None:
    """Return the `alternate-assets` keys `signer` prefers, best first.

    Keyed by the signer's `name`, so it resolves for both a `_ContractSigner`
    wrapper and a bare pyramids-eo signer.

    Args:
        signer: Any object exposing the `Signer` protocol's `name`.

    Returns:
        The preferred `alternate` keys in order, or `None` when the signer wants
        the canonical href. Pass the result straight to
        `pyramids.stac.resolved_href(..., alternate=...)`, which falls back to the
        canonical href for an asset that publishes no such alternate.

    Examples:
        - The requester-pays signer prefers the `s3` mirror:
            ```python
            >>> from earthlens.stac import alternate_preference_for, build_signer
            >>> alternate_preference_for(build_signer("aws-requester-pays"))
            ('s3',)

            ```
        - Every other signer keeps the canonical href:
            ```python
            >>> from earthlens.stac import alternate_preference_for, build_signer
            >>> alternate_preference_for(build_signer("anonymous")) is None
            True

            ```
        - An object exposing no `name` resolves canonically rather than raising:
            ```python
            >>> from earthlens.stac import alternate_preference_for
            >>> alternate_preference_for(object()) is None
            True

            ```

    See Also:
        build_signer: Builds the signer whose preference this reports.
        earthlens.stac.backend.STAC._fetch: Passes the result to
            `pyramids.stac.resolved_href` as its `alternate` argument.
    """
    return _ALTERNATE_PREFERENCE.get(getattr(signer, "name", ""))


class _ContractSigner:
    """Delegate to a pyramids-eo signer, mapping its auth errors to earthlens's.

    pyramids-eo's signers raise `pyramids_eo.errors.AuthenticationError`; the
    earthlens framework contract is a single `earthlens.base.AuthenticationError`
    across every backend. The calls that mint credentials — and can therefore
    fail — are `sign_href`, `sign_item`, `sign_request` and `gdal_env` (a bearer
    signer mints its token when `gdal_env` is read); each re-raises as earthlens's
    type. Every other attribute delegates to the wrapped signer unchanged. The
    protocol's `name` data member is bound as a real instance attribute at
    construction (not delegated through `__getattr__`), because
    `typing.runtime_checkable` resolves non-method members against the instance
    `__dict__` / MRO and does not consult `__getattr__`; binding it is what keeps
    `isinstance(wrapper, pyramids.stac.Signer)` true.

    Args:
        inner: The pyramids-eo signer to wrap.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.name = inner.name

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
        ValueError: When `signer_type` is not one of the known signer keys
            (propagated unchanged from `pyramids_eo.stac.build_signer`).

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
