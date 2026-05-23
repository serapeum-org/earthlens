"""Provider-specific STAC asset signers for the earthlens STAC backend.

pyramids ships the *generic* signers (`AnonymousSigner`,
`AWSRequesterPaysSigner`, `BearerTokenSigner`) that satisfy the
`pyramids.stac.Signer` protocol; earthlens adds only the **provider**
signers that need a remote-sensing SDK or provider-specific URL/credential
logic, per the pyramids/earthlens split:

* `MpcSasSigner` — Microsoft Planetary Computer. Signs by grafting an Azure
  **SAS token onto the asset URL query string** (via the `planetary-computer`
  SDK), not by a bearer header — GDAL forwards bearer headers across the
  cross-host redirects MPC blob URLs use, which would leak the token, so URL
  signing is the correct boundary here.
* `CdseS3Signer` — Copernicus Data Space Ecosystem. CDSE STAC assets resolve
  to an S3-compatible store at `eodata.dataspace.copernicus.eu` read with S3
  access-key/secret credentials, so the signer is a **GDAL-env S3 signer**
  (shaped like `AWSRequesterPaysSigner`), not a bearer signer.

Search is anonymous on both providers (`sign_request` is a no-op); only the
asset-read boundary needs credentials. The `build_signer` factory maps a
catalog `signer:` string to the right object, reusing the generic pyramids
signers for the `anonymous` / `aws-requester-pays` cases.
"""

from __future__ import annotations

from typing import Any


class MpcSasSigner:
    """Microsoft Planetary Computer signer (Azure SAS URL signing).

    Implements the `pyramids.stac.Signer` protocol structurally. The
    credential is appended to the asset URL as a SAS query token by the
    `planetary-computer` SDK, so nothing is added to the GDAL environment.
    Search is anonymous.

    Attributes:
        name: Stable signer label (`"mpc-sas"`); matches the catalog
            `signer:` field that selects this signer.

    Examples:
        - The credential rides in the URL, so the GDAL env stays empty:
            ```python
            >>> from earthlens.stac import MpcSasSigner
            >>> MpcSasSigner().gdal_env()
            {}

            ```
        - Search is anonymous, so the request hook is a no-op:
            ```python
            >>> from earthlens.stac import MpcSasSigner
            >>> MpcSasSigner().sign_request(object()) is None
            True

            ```
    """

    name = "mpc-sas"

    def sign_request(self, request: Any) -> None:
        """Leave the outgoing search request unchanged (MPC search is anonymous)."""
        return None

    def sign_item(self, item: Any) -> None:
        """Sign every asset href on a returned Item in place via the MPC SDK.

        Matches the `pystac-client` `modifier` contract (mutate in place,
        return `None`). Delegates to `planetary_computer.sign_inplace`.

        Args:
            item: A pystac `Item` / `ItemCollection` returned by the search.
        """
        import planetary_computer as pc

        pc.sign_inplace(item)
        return None

    def sign_href(self, href: str) -> str:
        """Return `href` with an Azure SAS token grafted onto the query string.

        Args:
            href: An unsigned MPC asset href.

        Returns:
            The signed href (used by `load_asset(signer=)` /
            `from_stac(patch_url=)`).
        """
        import planetary_computer as pc

        return pc.sign(href)

    def gdal_env(self) -> dict[str, str]:
        """Return an empty GDAL config — the credential lives in the URL, not the env."""
        return {}


class CdseS3Signer:
    """Copernicus Data Space (CDSE) signer (S3 GDAL-env credentials).

    CDSE STAC assets live on an S3-compatible store; reads are authenticated
    with S3 access-key/secret credentials supplied through the GDAL
    environment, and `s3://eodata/<key>` hrefs are rewritten to the
    `/vsis3/eodata/<key>` GDAL VSI path. Search is anonymous.

    Attributes:
        name: Stable signer label (`"cdse-s3"`).

    Examples:
        - An `s3://eodata/...` href is rewritten to the GDAL `/vsis3/` path:
            ```python
            >>> from earthlens.stac import CdseS3Signer
            >>> CdseS3Signer("ak", "sk").sign_href("s3://eodata/foo/B04.tif")
            '/vsis3/eodata/foo/B04.tif'

            ```
        - An `https://` href on the CDSE host is rewritten the same way:
            ```python
            >>> from earthlens.stac import CdseS3Signer
            >>> CdseS3Signer("ak", "sk").sign_href(
            ...     "https://eodata.dataspace.copernicus.eu/foo/B04.tif")
            '/vsis3/eodata/foo/B04.tif'

            ```
        - The credentials surface through the GDAL S3 environment:
            ```python
            >>> from earthlens.stac import CdseS3Signer
            >>> env = CdseS3Signer("ak", "sk").gdal_env()
            >>> env["AWS_ACCESS_KEY_ID"]
            'ak'
            >>> env["AWS_S3_ENDPOINT"]
            'eodata.dataspace.copernicus.eu'

            ```
    """

    name = "cdse-s3"

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        endpoint: str = "eodata.dataspace.copernicus.eu",
    ) -> None:
        """Store the S3 credentials and endpoint.

        Args:
            access_key: CDSE S3 access key id.
            secret_key: CDSE S3 secret key.
            endpoint: S3 endpoint host. Defaults to the CDSE eodata store.
        """
        self._access_key = access_key
        self._secret_key = secret_key
        self._endpoint = endpoint

    def sign_request(self, request: Any) -> None:
        """Leave the outgoing search request unchanged (CDSE search is anonymous)."""
        return None

    def sign_item(self, item: Any) -> None:
        """Leave returned Items unchanged — asset auth is via the GDAL env, not the href."""
        return None

    def sign_href(self, href: str) -> str:
        """Rewrite a CDSE asset href to the `/vsis3/eodata/<key>` GDAL path.

        CDSE items expose assets both as `s3://eodata/<key>` and as
        `https://<eodata-host>/<key>` (the latter is the common default href,
        with the `s3://` form often only an `alternate`). Either is rewritten to
        the S3 VSI path so the credentials in `gdal_env()` apply; any other host
        is returned unchanged.

        Args:
            href: An asset href (`s3://eodata/...`, an `https://` URL on the
                CDSE endpoint host, or something else).

        Returns:
            The GDAL-readable `/vsis3/...` path, or `href` unchanged when it is
            not a CDSE asset.
        """
        if href.startswith("s3://"):
            return "/vsis3/" + href[len("s3://"):]
        if href.startswith(("https://", "http://")):
            from urllib.parse import urlsplit

            parts = urlsplit(href)
            if parts.netloc == self._endpoint or parts.netloc.endswith(
                "dataspace.copernicus.eu"
            ):
                key = parts.path.lstrip("/")
                if key.startswith("eodata/"):
                    return f"/vsis3/{key}"
                return f"/vsis3/eodata/{key}"
        return href

    def gdal_env(self) -> dict[str, str]:
        """Return the GDAL config carrying the CDSE S3 credentials for asset reads.

        Returns:
            A mapping with the S3 endpoint, access-key/secret, virtual-hosting
            off, HTTPS on, and the standard cloud-read knob.
        """
        return {
            "AWS_S3_ENDPOINT": self._endpoint,
            "AWS_ACCESS_KEY_ID": self._access_key,
            "AWS_SECRET_ACCESS_KEY": self._secret_key,
            "AWS_VIRTUAL_HOSTING": "FALSE",
            "AWS_HTTPS": "YES",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        }


def build_signer(signer_type: str, **creds: Any) -> Any:
    """Build the signer named by a catalog `signer:` field.

    Generic signers come from `pyramids.stac` (imported lazily so the package
    imports without the `[stac]` extra); the two provider signers are the
    earthlens-local classes above.

    Args:
        signer_type: One of `"anonymous"`, `"aws-requester-pays"`,
            `"mpc-sas"`, `"cdse-s3"`.
        **creds: Extra credentials forwarded to the selected signer —
            `region` for `aws-requester-pays`; the CDSE S3 credential
            resolution kwargs for `cdse-s3` (see `auth_cdse.s3_credentials`).

    Returns:
        A signer satisfying the `pyramids.stac.Signer` protocol.

    Raises:
        ValueError: When `signer_type` is not a known signer name.

    Examples:
        - Build the MPC signer (no SDK needed until it signs an href):
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("mpc-sas").name
            'mpc-sas'

            ```
        - Build the CDSE signer from explicit S3 keys:
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("cdse-s3", access_key="ak", secret_key="sk").gdal_env()["AWS_ACCESS_KEY_ID"]
            'ak'

            ```
        - An unknown signer name is rejected:
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("nope")
            Traceback (most recent call last):
                ...
            ValueError: unknown signer_type 'nope'; expected one of 'anonymous', 'aws-requester-pays', 'mpc-sas', 'cdse-s3'.

            ```
    """
    if signer_type == "anonymous":
        from pyramids.stac import AnonymousSigner

        return AnonymousSigner()
    if signer_type == "aws-requester-pays":
        from pyramids.stac import AWSRequesterPaysSigner

        return AWSRequesterPaysSigner(region=creds.get("region"))
    if signer_type == "mpc-sas":
        return MpcSasSigner()
    if signer_type == "cdse-s3":
        from earthlens.stac import auth_cdse

        access_key, secret_key = auth_cdse.s3_credentials(**creds)
        return CdseS3Signer(access_key=access_key, secret_key=secret_key)
    raise ValueError(
        f"unknown signer_type {signer_type!r}; expected one of "
        "'anonymous', 'aws-requester-pays', 'mpc-sas', 'cdse-s3'."
    )
