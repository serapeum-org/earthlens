"""Provider-specific STAC asset signers for the earthlens STAC backend.

pyramids ships the *generic* signers (`AnonymousSigner`,
`AWSRequesterPaysSigner`, `BearerTokenSigner`) and the generic `Signer`
protocol; earthlens owns the **provider** signers, which encode the semantics
of a specific Earth-observation provider's auth (a hardcoded token endpoint,
a provider-specific URL carve-out) and so are out of scope for a generic GIS
library, per the pyramids/earthlens split (serapeum-org/earthlens#384):

* `PlanetaryComputerSigner` — Microsoft Planetary Computer. Mints a per-(account,
  container) SAS token over stdlib `urllib` and appends it to the blob href —
  no `planetary-computer` SDK required.
* `EarthdataSigner` — NASA Earthdata (EDL). Mints / uses a bearer token from the
  EDL `find_or_create_token` endpoint, sent as a GDAL `Authorization: Bearer`
  header for `/vsicurl/` reads of EDL-gated DAAC assets.
* `CDSESigner` — Copernicus Data Space (CDSE) Keycloak-OAuth2 *bearer* signer for
  the CDSE HTTPS/OData path (password + refresh grant).
* `CdseS3Signer` — Copernicus Data Space, the S3-credential variant: CDSE STAC
  assets resolve to an S3-compatible store at `eodata.dataspace.copernicus.eu`
  read with S3 access-key/secret credentials, so this signer is a **GDAL-env S3
  signer** (shaped like `AWSRequesterPaysSigner`), not a bearer signer — a
  different mechanism from `CDSESigner`, so both coexist.
* `BdcTokenSigner` — Brazil Data Cube (BDC). Rewrites the asset href to carry
  an `?access_token=…` query parameter; the token is read from
  `$BDC_ACCESS_TOKEN`. Used by the token-gated BDC tiers (rare — most BDC
  collections in the published STAC v1 catalog read anonymously).

Search is anonymous on these providers (`sign_request` is a no-op); only the
asset-read boundary needs credentials. The `build_signer` factory maps a catalog
`signer:` string to the right object, reusing the pyramids generic signers for
the `anonymous` / `aws-requester-pays` cases.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


class _BaseSigner:
    """No-op signer base; concrete signers override only what they need."""

    name = "base"

    def sign_request(self, request: Any) -> Any | None:
        """Return `None` — leave the request unchanged."""
        return None

    def sign_item(self, item: Any) -> None:
        """Return `None` — leave the item unchanged."""
        return None

    def sign_href(self, href: str) -> str:
        """Return `href` unchanged."""
        return href

    def gdal_env(self) -> dict[str, str]:
        """Return an empty GDAL-config mapping."""
        return {}


class PlanetaryComputerSigner(_BaseSigner):
    """Native Microsoft Planetary Computer SAS signer (no SDK dependency).

    PC hosts Sentinel / Landsat / many collections behind short-lived Shared
    Access Signature (SAS) tokens. This signer mints a token per
    `(account, container)` from the PC token endpoint and appends it to the
    blob href's query string — the same algorithm as `planetary_computer.sign`
    but implemented over the standard library (`urllib`), so earthlens gains PC
    support without taking the `planetary-computer` SDK as a dependency.

    Because the credential rides the URL, `gdal_env` is empty: a signed
    `https://<account>.blob.core.windows.net/...?<sas>` href is read directly
    through GDAL `/vsicurl/` with no extra config. Wire it in via
    `open_client(..., signer=PlanetaryComputerSigner())` (its `sign_item`
    rewrites returned Items) or `from_stac(..., signer=PlanetaryComputerSigner())`.

    Non-PC hrefs, the public `ai4edatasetspublicassets` bucket, and
    already-signed URLs pass through unchanged. Tokens are cached until
    `refresh_window` seconds before their advertised expiry.

    Args:
        sas_url: SAS token endpoint. Defaults to `$PC_SDK_SAS_URL` or
            `https://planetarycomputer.microsoft.com/api/sas/v1/token`.
        subscription_key: Optional PC subscription key (raises rate limits),
            sent as the `Ocp-Apim-Subscription-Key` header. Defaults to
            `$PC_SDK_SUBSCRIPTION_KEY`.
        refresh_window: Refetch a cached token when it is within this many
            seconds of expiry (default 60).
        timeout: Per-request timeout, in seconds, for the token GET.

    Examples:
        - A non-PC href passes through untouched:
            ```python
            >>> from earthlens.stac import PlanetaryComputerSigner
            >>> signer = PlanetaryComputerSigner()
            >>> signer.sign_href("https://example.com/scene.tif")
            'https://example.com/scene.tif'

            ```
        - An already-signed blob href is left as-is:
            ```python
            >>> from earthlens.stac import PlanetaryComputerSigner
            >>> signed = "https://x.blob.core.windows.net/c/b.tif?se=2034&sig=abc"
            >>> PlanetaryComputerSigner().sign_href(signed) == signed
            True

            ```
        - The public assets bucket is never signed:
            ```python
            >>> from earthlens.stac import PlanetaryComputerSigner
            >>> pub = "https://ai4edatasetspublicassets.blob.core.windows.net/c/b.tif"
            >>> PlanetaryComputerSigner().sign_href(pub) == pub
            True

            ```
    """

    name = "planetary-computer"

    _BLOB_DOMAIN = ".blob.core.windows.net"
    _PUBLIC_HOST = "ai4edatasetspublicassets.blob.core.windows.net"
    _DEFAULT_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
    _SIGNED_KEYS = frozenset({"st", "se", "sp", "sig"})

    def __init__(
        self,
        *,
        sas_url: str | None = None,
        subscription_key: str | None = None,
        refresh_window: float = 60.0,
        timeout: float = 30.0,
    ) -> None:
        """Store endpoint / auth settings and initialise the token cache.

        Args:
            sas_url: SAS token endpoint (env `PC_SDK_SAS_URL` or the PC
                default when `None`).
            subscription_key: Optional PC subscription key (env
                `PC_SDK_SUBSCRIPTION_KEY` when `None`).
            refresh_window: Seconds-before-expiry at which a cached token is
                refetched.
            timeout: Token-request timeout in seconds.
        """
        self._sas_url = (
            sas_url or os.environ.get("PC_SDK_SAS_URL") or self._DEFAULT_SAS_URL
        ).rstrip("/")
        self._subscription_key = subscription_key or os.environ.get(
            "PC_SDK_SUBSCRIPTION_KEY"
        )
        self._refresh_window = refresh_window
        self._timeout = timeout
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()

    def sign_href(self, href: str) -> str:
        """Append a SAS token to a PC blob href; pass non-PC hrefs through.

        Args:
            href: The asset href.

        Returns:
            The href with `?<sas-token>` appended when it is an unsigned PC
            blob URL, otherwise `href` unchanged.
        """
        account, container = self._parse_blob(href)
        if account is None or container is None or self._already_signed(href):
            return href
        token = self._token(account, container)
        sep = "&" if urlparse(href).query else "?"
        return f"{href}{sep}{token}"

    def sign_item(self, item: Any) -> None:
        """Rewrite every asset href on an Item / ItemCollection in place.

        Args:
            item: A STAC Item, an ItemCollection (iterable of Items), or the
                raw-dict equivalent.

        Returns:
            None (pystac-client's `modifier` contract).
        """
        for one in self._iter_signable(item):
            assets = getattr(one, "assets", None)
            if assets is None and isinstance(one, dict):
                assets = one.get("assets")
            if not assets:
                continue
            values = assets.values() if hasattr(assets, "values") else []
            for asset in values:
                href = getattr(asset, "href", None)
                if href is not None:
                    asset.href = self.sign_href(href)
                elif isinstance(asset, dict) and asset.get("href") is not None:
                    asset["href"] = self.sign_href(asset["href"])
        return None

    @staticmethod
    def _iter_signable(item: Any) -> list[Any]:
        """Return `[item]`, or its member items when `item` is a collection."""
        has_assets = getattr(item, "assets", None) is not None or (
            isinstance(item, dict) and "assets" in item
        )
        if has_assets:
            return [item]
        try:
            return list(item)
        except TypeError:
            return [item]

    def _parse_blob(self, href: str) -> tuple[str | None, str | None]:
        """Return `(account, container)` for a signable PC blob href.

        Returns `(None, None)` when `href` is not an Azure blob URL, is the
        public assets bucket, or carries no container path segment.
        """
        parsed = urlparse(href)
        netloc = parsed.netloc.lower()
        if not netloc.endswith(self._BLOB_DOMAIN) or netloc == self._PUBLIC_HOST:
            return None, None
        account = netloc.split(".", 1)[0]
        segments = parsed.path.lstrip("/").split("/", 1)
        container = segments[0] if segments and segments[0] else None
        if not account or not container:
            return None, None
        return account, container

    def _already_signed(self, href: str) -> bool:
        """Return True when `href` already carries SAS query parameters."""
        return bool(self._SIGNED_KEYS & set(parse_qs(urlparse(href).query)))

    def _token(self, account: str, container: str) -> str:
        """Return a cached SAS token, refetching when near expiry (locked)."""
        key = (account, container)

        def fresh(entry: tuple[str, float] | None) -> bool:
            return entry is not None and entry[1] - time.time() > self._refresh_window

        cached = self._cache.get(key)
        if fresh(cached):
            assert cached is not None  # fresh() is False for a None entry
            return cached[0]
        # Double-checked locking: serialise the fetch so concurrent callers for
        # the same (account, container) do not each mint a token.
        with self._lock:
            cached = self._cache.get(key)
            if fresh(cached):
                assert cached is not None  # fresh() is False for a None entry
                return cached[0]
            token, expiry = self._fetch_token(account, container)
            self._cache[key] = (token, expiry)
            return token

    def _fetch_token(self, account: str, container: str) -> tuple[str, float]:
        """GET a fresh SAS token + expiry epoch from the PC token endpoint.

        Args:
            account: Azure storage account name.
            container: Blob container name.

        Returns:
            A `(token, expiry_epoch_seconds)` tuple. When the response carries
            no parseable `msft:expiry` the token is treated as already expired
            so the next call refetches.
        """
        url = f"{self._sas_url}/{account}/{container}"
        request = urllib.request.Request(url)
        if self._subscription_key:
            request.add_header("Ocp-Apim-Subscription-Key", self._subscription_key)
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # nosec B310 - fixed http(s) endpoint, not attacker-controlled
            payload = json.loads(response.read().decode("utf-8"))
        token = payload["token"]
        expiry = self._parse_expiry(payload.get("msft:expiry"))
        return token, expiry

    @staticmethod
    def _parse_expiry(value: Any) -> float:
        """Parse an RFC 3339 `msft:expiry` string to an epoch (seconds).

        Returns a past timestamp when `value` is missing or unparseable, so the
        caller does not cache an unbounded token.
        """
        if not isinstance(value, str):
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()


class _BearerProviderSigner(_BaseSigner):
    """Shared base for provider signers that mint + refresh a bearer token.

    Subclasses implement `_fetch_token` (the network seam, returning a
    `(access_token, expiry_epoch_seconds)` tuple). The token is cached and
    refetched when within `refresh_window` seconds of expiry. The credential is
    sent as a GDAL `Authorization: Bearer` header (`gdal_env`); `sign_href` is
    identity (auth is header-side, not URL-side).

    Security note: GDAL forwards the `Authorization` header across HTTP
    redirects, including cross-host ones. Use only with catalogs whose asset
    host authenticates with the bearer directly.
    """

    def __init__(self, *, refresh_window: float = 300.0, timeout: float = 30.0) -> None:
        """Initialise the token cache and timing knobs.

        Args:
            refresh_window: Seconds-before-expiry at which to refetch.
            timeout: Token-request timeout in seconds.
        """
        self._refresh_window = refresh_window
        self._timeout = timeout
        self._cache: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _fetch_token(self) -> tuple[str, float]:
        """Return a fresh `(access_token, expiry_epoch)` — implemented by subclasses."""
        raise NotImplementedError

    def _token(self) -> str:
        """Return a cached bearer token, refetching when near expiry (locked)."""

        def fresh() -> bool:
            return (
                self._cache is not None
                and self._cache[1] - time.time() > self._refresh_window
            )

        if fresh():
            assert self._cache is not None  # fresh() is False when _cache is None
            return self._cache[0]
        # Double-checked locking: serialise the mint so concurrent callers do
        # not each fetch a token.
        with self._lock:
            if fresh():
                assert self._cache is not None  # fresh() is False when _cache is None
                return self._cache[0]
            token, expiry = self._fetch_token()
            self._cache = (token, expiry)
            return token

    def sign_request(self, request: Any) -> Any:
        """Set the `Authorization: Bearer` header on an outgoing request."""
        request.headers["Authorization"] = f"Bearer {self._token()}"
        return request

    def gdal_env(self) -> dict[str, str]:
        """Return the GDAL config carrying the bearer header for asset reads."""
        return {"GDAL_HTTP_HEADERS": f"Authorization: Bearer {self._token()}"}


class EarthdataSigner(_BearerProviderSigner):
    """NASA Earthdata (EDL) bearer signer — native, no `earthaccess` SDK.

    Uses a pre-minted token when given (or `$EARTHDATA_TOKEN` / `$EARTHDATA_PAT`),
    otherwise mints one from the EDL `find_or_create_token` endpoint with HTTP
    Basic auth (`$EARTHDATA_USERNAME` / `$EARTHDATA_PASSWORD`). The token is sent
    as a GDAL `Authorization: Bearer` header for `/vsicurl/` reads of EDL-gated
    DAAC assets.

    Args:
        username: EDL username (env `EARTHDATA_USERNAME` when `None`).
        password: EDL password (env `EARTHDATA_PASSWORD` when `None`).
        token: A pre-minted bearer token (env `EARTHDATA_TOKEN` / `EARTHDATA_PAT`
            when `None`); skips minting entirely.
        refresh_window: Seconds-before-expiry at which to refetch a minted token.
        timeout: Token-request timeout in seconds.

    Examples:
        - A pre-minted token is used directly in the GDAL header:
            ```python
            >>> from earthlens.stac import EarthdataSigner
            >>> signer = EarthdataSigner(token="edl-tok")
            >>> signer.gdal_env()["GDAL_HTTP_HEADERS"]
            'Authorization: Bearer edl-tok'

            ```
    """

    name = "earthdata"
    _TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"  # nosec B105 - not a secret (public URL / identifier)

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        refresh_window: float = 300.0,
        timeout: float = 30.0,
    ) -> None:
        """Store EDL credentials / static token; init the token cache."""
        super().__init__(refresh_window=refresh_window, timeout=timeout)
        self._username = username or os.environ.get("EARTHDATA_USERNAME")
        self._password = password or os.environ.get("EARTHDATA_PASSWORD")
        self._static_token = (
            token
            or os.environ.get("EARTHDATA_TOKEN")
            or os.environ.get("EARTHDATA_PAT")
        )

    def _token(self) -> str:
        """Return the static token when present, else the minted/cached one."""
        if self._static_token:
            return self._static_token
        return super()._token()

    def _fetch_token(self) -> tuple[str, float]:
        """Mint an EDL bearer token via find_or_create_token (HTTP Basic)."""
        if not (self._username and self._password):
            raise ValueError(
                "EarthdataSigner needs a token (EARTHDATA_TOKEN/PAT) or "
                "EARTHDATA_USERNAME + EARTHDATA_PASSWORD."
            )
        creds = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        request = urllib.request.Request(self._TOKEN_URL, method="POST")
        request.add_header("Authorization", f"Basic {creds}")
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # nosec B310 - fixed http(s) endpoint, not attacker-controlled
            payload = json.loads(response.read().decode("utf-8"))
        token = payload["access_token"]
        expiry = self._parse_expiry(payload.get("expiration_date"))
        return token, expiry

    @staticmethod
    def _parse_expiry(value: Any) -> float:
        """Parse an EDL `expiration_date` to an epoch; default to now + 1h."""
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return time.time() + 3600.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        return time.time() + 3600.0


class CDSESigner(_BearerProviderSigner):
    """Copernicus Data Space Ecosystem (CDSE) bearer signer via Keycloak OAuth2.

    Mints an access token from the CDSE Keycloak token endpoint with a password
    grant (`$CDSE_USERNAME` / `$CDSE_PASSWORD`, public client `cdse-public`), then
    refreshes it with the refresh-token grant. The access token is sent as a GDAL
    `Authorization: Bearer` header for `/vsicurl/` reads of CDSE HTTPS/OData
    assets.

    This is the *bearer* (HTTPS/OData) variant; for the S3-credential variant of
    CDSE asset reads use `CdseS3Signer`.

    Args:
        username: CDSE username (env `CDSE_USERNAME` when `None`).
        password: CDSE password (env `CDSE_PASSWORD` when `None`).
        client_id: Keycloak client id (default `"cdse-public"`).
        refresh_window: Seconds-before-expiry at which to refresh (CDSE access
            tokens live ~600 s).
        timeout: Token-request timeout in seconds.
    """

    name = "cdse"
    _TOKEN_URL = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/"  # nosec B105 - not a secret (public URL / identifier)
        "openid-connect/token"
    )

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "cdse-public",
        refresh_window: float = 30.0,
        timeout: float = 30.0,
    ) -> None:
        """Store CDSE credentials; init the token + refresh-token cache."""
        super().__init__(refresh_window=refresh_window, timeout=timeout)
        self._username = username or os.environ.get("CDSE_USERNAME")
        self._password = password or os.environ.get("CDSE_PASSWORD")
        self._client_id = client_id
        self._refresh_token: str | None = None

    def _fetch_token(self) -> tuple[str, float]:
        """Mint or refresh an access token, falling back to a password grant.

        Tries the refresh-token grant when a refresh token is held; if that
        fails (the refresh token has expired — CDSE refresh tokens live
        ~3600 s — or is otherwise rejected), the stale token is dropped and a
        fresh password grant is attempted. This lets a long-idle, reused signer
        recover instead of raising on the expired refresh token.
        """
        if self._refresh_token is not None:
            try:
                return self._request_token(
                    {
                        "client_id": self._client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    }
                )
            except urllib.error.URLError:
                # Refresh token expired / rejected — re-authenticate below.
                self._refresh_token = None
        if not (self._username and self._password):
            raise ValueError("CDSESigner needs CDSE_USERNAME + CDSE_PASSWORD.")
        return self._request_token(
            {
                "client_id": self._client_id,
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            }
        )

    def _request_token(self, form: dict[str, str]) -> tuple[str, float]:
        """POST a Keycloak token request and return `(access_token, expiry)`.

        Rotates the cached refresh token from the response, and derives the
        access-token expiry from `expires_in` (default 600 s).
        """
        body = urlencode(form).encode("utf-8")
        request = urllib.request.Request(self._TOKEN_URL, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # nosec B310 - fixed http(s) endpoint, not attacker-controlled
            payload = json.loads(response.read().decode("utf-8"))
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        expiry = time.time() + float(payload.get("expires_in", 600))
        return payload["access_token"], expiry


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
            return "/vsis3/" + href[len("s3://") :]
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


class BdcTokenSigner(_BaseSigner):
    """Brazil Data Cube (BDC) OAuth-token URL signer.

    Some Brazil Data Cube collections (token-gated tiers) require an
    `?access_token=<token>` query parameter on each asset href; open
    collections (the bulk of the published STAC v1 catalog) are anonymous and
    do not need a token. `BdcTokenSigner` rewrites a clean asset href into a
    token-bearing one for every read, leaving the bare-anonymous BDC reads to
    the `anonymous` signer (the endpoint default).

    BDC search itself is anonymous — `sign_request` is a no-op, and
    `sign_item` does not mutate returned items. The token rides in the asset
    URL's query string, not the GDAL environment, so `gdal_env` is empty: a
    signed `https://data.inpe.br/...tif?access_token=…` href is read directly
    through GDAL `/vsicurl/` with no extra config.

    The token is read from the `BDC_ACCESS_TOKEN` environment variable on
    first use; absent it, `sign_href` raises `AuthenticationError` (from
    `earthlens.base`) naming the env var and pointing at an open collection
    so the user can switch tiers if they want. Wire it in via a
    per-collection `signer: bdc-token` override on the token-gated rows of
    `catalog/bdc.yaml`.

    Args:
        token: Explicit BDC OAuth token. Defaults to `$BDC_ACCESS_TOKEN`.

    Examples:
        - A clean https href becomes a query-bearing one (passing the token
          explicitly so the example does not mutate the process env):
            ```python
            >>> from earthlens.stac import BdcTokenSigner
            >>> BdcTokenSigner(token="tok").sign_href("https://data.inpe.br/bdc/data/x.tif")
            'https://data.inpe.br/bdc/data/x.tif?access_token=tok'

            ```
        - An href that already carries a query gets `&access_token=…` instead:
            ```python
            >>> from earthlens.stac import BdcTokenSigner
            >>> BdcTokenSigner(token="tok").sign_href("https://data.inpe.br/bdc/data/x.tif?foo=1")
            'https://data.inpe.br/bdc/data/x.tif?foo=1&access_token=tok'

            ```
    """

    name = "bdc-token"

    def __init__(self, token: str | None = None) -> None:
        """Store the explicit token (or defer to `$BDC_ACCESS_TOKEN`)."""
        self._explicit_token = token

    def _token(self) -> str:
        """Return the BDC OAuth token from kwarg or `BDC_ACCESS_TOKEN`."""
        token = self._explicit_token or os.environ.get("BDC_ACCESS_TOKEN")
        if not token:
            from earthlens.base import AuthenticationError

            raise AuthenticationError(
                "set BDC_ACCESS_TOKEN — this Brazil Data Cube collection requires "
                "an OAuth token; open collections such as CBERS4-WFI-16D-2 do not "
                "need one and read anonymously."
            )
        return token

    def sign_href(self, href: str) -> str:
        """Append `?access_token=<token>` (or `&access_token=`) to the asset href.

        Uses `urlsplit`/`urlunsplit` so the token lands in the URL's query
        component even when the href carries a `#fragment` (a naive
        `"?" in href` check would mistake a fragment for a query and append
        after `#`, where the server never sees it).
        """
        from urllib.parse import quote, urlsplit, urlunsplit

        token = quote(self._token(), safe="")
        parts = urlsplit(href)
        new_query = (
            f"{parts.query}&access_token={token}"
            if parts.query
            else f"access_token={token}"
        )
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )


#: AWS **opt-in** regions (disabled by default). What matters here: their S3
#: service *rejects* a request sent to the global `<bucket>.s3.amazonaws.com`
#: endpoint with `IllegalLocationConstraintException` instead of redirecting it,
#: so a public bucket in one of these regions is only reachable by naming the
#: region's endpoint explicitly. Standard (enabled-by-default) regions redirect
#: normally and are deliberately left unpinned — see `_region_needs_endpoint`.
_OPT_IN_REGIONS = frozenset(
    {
        "af-south-1",
        "ap-east-1",
        "ap-east-2",
        "ap-south-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-southeast-5",
        "ap-southeast-7",
        "ca-west-1",
        "eu-central-2",
        "eu-south-1",
        "eu-south-2",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "mx-central-1",
    }
)


def _region_needs_endpoint(region: str) -> bool:
    """Return whether a region must be reached at its own S3 endpoint.

    True for an AWS opt-in region (the global endpoint rejects it, see
    :data:`_OPT_IN_REGIONS`) and for any China-partition region (`cn-*`, whose
    buckets the global `amazonaws.com` endpoint cannot reach at all). Standard
    regions return False: the global endpoint redirects to them, so leaving them
    unpinned keeps cross-region reads working for an endpoint that federates
    buckets across regions (e.g. earth-search serving Copernicus DEM from
    `eu-central-1` while its catalog region is `us-west-2`). GovCloud (`us-gov-*`)
    is standard-TLD and needs no special case; the air-gapped ISO partitions
    (`us-iso-*` / `us-isob-*`) are intentionally out of scope — they host no
    public/anonymous STAC bucket.

    Args:
        region: An AWS region code (e.g. `"af-south-1"`, `"us-west-2"`).

    Returns:
        True when the region needs an explicit `AWS_S3_ENDPOINT`, else False.

    Examples:
        - An opt-in region needs its own endpoint:
            ```python
            >>> from earthlens.stac.signers import _region_needs_endpoint
            >>> _region_needs_endpoint("af-south-1")
            True

            ```
        - A standard region does not (the global endpoint redirects to it):
            ```python
            >>> from earthlens.stac.signers import _region_needs_endpoint
            >>> _region_needs_endpoint("us-west-2")
            False

            ```
        - A China-partition region needs its own endpoint:
            ```python
            >>> from earthlens.stac.signers import _region_needs_endpoint
            >>> _region_needs_endpoint("cn-north-1")
            True

            ```
    """
    return region in _OPT_IN_REGIONS or region.startswith("cn-")


class _AnonymousS3Signer:
    """Anonymous signer that also tells GDAL not to sign its S3 reads.

    pyramids' `AnonymousSigner` contributes an empty GDAL config, which is
    right for a plain-HTTPS anonymous asset but not for one on S3: GDAL's
    `/vsis3/` driver then tries to *sign* the request, finds no AWS
    credentials, and fails with `InvalidCredentials` — an error whose own text
    tells you to set `AWS_NO_SIGN_REQUEST`. The anonymous STAC endpoints that
    serve assets straight out of a public bucket (Digital Earth Africa, DEA,
    VEDA) hit exactly that, which is why their e2e reads failed.

    Setting the flag is safe here precisely *because* this signer is the
    anonymous one: there are no credentials to use, so an unsigned request is
    the only kind that can succeed. Requester-pays keeps its own signer, which
    contributes real credentials plus `AWS_REQUEST_PAYER`, and is untouched.

    When the endpoint declares a `region` that **needs** its own S3 endpoint
    (see :func:`_region_needs_endpoint`), the signer pins GDAL there
    (`AWS_REGION` + `AWS_S3_ENDPOINT=s3.<region>.amazonaws.com`), so a `/vsis3/`
    read lands at `<bucket>.s3.<region>.amazonaws.com` rather than the default
    global `<bucket>.s3.amazonaws.com`. This is **required** for a bucket in an
    **opt-in** region (e.g. Digital Earth Africa on `af-south-1`), which rejects
    the global endpoint with `IllegalLocationConstraintException` ("the <region>
    location constraint is incompatible for the region specific endpoint this
    request was sent to"), and for the China partition (`cn-*`).

    A **standard** region (e.g. `us-west-2`, `ap-southeast-2`) is deliberately
    **left unpinned**: the global endpoint redirects to it, and pinning would be
    wrong for an endpoint that federates buckets across regions — `earth-search`
    is pinned by the catalog to `us-west-2` yet serves its `cop-dem-glo-90`
    assets from an `eu-central-1` bucket, so a hard endpoint pin would send those
    reads to the wrong region. Leaving standard regions unpinned keeps GDAL's
    region redirect handling that case. The region comes from the catalog
    endpoint's `region:` field, threaded through `build_signer`; when it is
    `None`, a standard region, or an HTTPS `/vsicurl/` asset read, only the
    no-sign flag is emitted.

    Args:
        region: AWS region of the endpoint's public bucket, or `None` when the
            endpoint's assets are not region-bound S3 objects. Only an opt-in or
            `cn-*` region triggers the endpoint pin.

    Examples:
        - The GDAL config opts out of request signing:
            ```python
            >>> from earthlens.stac.signers import build_signer
            >>> build_signer("anonymous").gdal_env()["AWS_NO_SIGN_REQUEST"]
            'YES'

            ```
        - A region-bound endpoint also pins GDAL to that region's S3 endpoint:
            ```python
            >>> from earthlens.stac.signers import build_signer
            >>> env = build_signer("anonymous", region="af-south-1").gdal_env()
            >>> env["AWS_REGION"], env["AWS_S3_ENDPOINT"]
            ('af-south-1', 's3.af-south-1.amazonaws.com')

            ```
        - Signing a request or an href is still a no-op — nothing to add:
            ```python
            >>> from earthlens.stac.signers import build_signer
            >>> signer = build_signer("anonymous")
            >>> signer.sign_href("s3://bucket/key.tif")
            's3://bucket/key.tif'

            ```
    """

    #: Signer name, matching the catalog's `signer:` value and the pyramids
    #: signers' own attribute, so `_signer_for` logging reads the same.
    name: str = "anonymous"

    def __init__(self, region: str | None = None) -> None:
        """Store the endpoint's bucket region (or `None` for non-S3 assets).

        Args:
            region: AWS region of the anonymous bucket, used to pin GDAL at the
                region's S3 endpoint. `None` leaves the endpoint at GDAL's
                default (correct for HTTPS-asset endpoints).
        """
        self._region = region

    def gdal_env(self) -> dict[str, str]:
        """Return the GDAL config for unsigned public-bucket reads.

        Always emits `AWS_NO_SIGN_REQUEST=YES`. When the signer carries a region
        that needs its own endpoint (opt-in or `cn-*`; see
        :func:`_region_needs_endpoint`), it additionally emits `AWS_REGION` and
        `AWS_S3_ENDPOINT=s3.<region>.amazonaws.com` so a bucket the global
        endpoint would reject is reached at its region-specific endpoint. A
        standard region emits only the no-sign flag, leaving GDAL's redirect to
        find the bucket's region (correct for cross-region endpoints).

        Deliberately nothing more. Copying the requester-pays signer's
        `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR` and
        `CPL_VSIL_CURL_USE_HEAD=NO` alongside it would apply to *every*
        anonymous endpoint — the catalog default, so most of them — and
        suppress the sidecar discovery (`.aux.xml`, `.msk`, overviews) that
        some of those assets rely on. Those knobs are a billing optimisation
        for requester-pays, not part of not signing a request.

        Returns:
            dict[str, str]: `{"AWS_NO_SIGN_REQUEST": "YES"}`, plus `AWS_REGION`
            and `AWS_S3_ENDPOINT` when the region needs an explicit endpoint.

        Examples:
            - An endpoint with no region emits only the no-sign flag:
                ```python
                >>> from earthlens.stac.signers import build_signer
                >>> build_signer("anonymous").gdal_env()
                {'AWS_NO_SIGN_REQUEST': 'YES'}

                ```
            - An opt-in region pins GDAL at that region's S3 endpoint, so a
              bucket the global endpoint would reject is reached instead:
                ```python
                >>> from earthlens.stac.signers import build_signer
                >>> env = build_signer("anonymous", region="af-south-1").gdal_env()
                >>> env["AWS_REGION"]
                'af-south-1'
                >>> env["AWS_S3_ENDPOINT"]
                's3.af-south-1.amazonaws.com'

                ```
            - A standard region is left unpinned (GDAL redirects to the bucket):
                ```python
                >>> from earthlens.stac.signers import build_signer
                >>> build_signer("anonymous", region="us-west-2").gdal_env()
                {'AWS_NO_SIGN_REQUEST': 'YES'}

                ```
        """
        env = {"AWS_NO_SIGN_REQUEST": "YES"}
        if self._region and _region_needs_endpoint(self._region):
            env["AWS_REGION"] = self._region
            # China regions live in a separate AWS partition whose S3 hosts end
            # in `.amazonaws.com.cn`; every other region uses `.amazonaws.com`.
            tld = (
                "amazonaws.com.cn"
                if self._region.startswith("cn-")
                else "amazonaws.com"
            )
            env["AWS_S3_ENDPOINT"] = f"s3.{self._region}.{tld}"
        return env

    def sign_request(self, request: Any) -> Any:
        """Return `request` unchanged — an anonymous search needs no signing.

        Args:
            request: The outgoing search request.

        Returns:
            The same request object.
        """
        return request

    def sign_href(self, href: str) -> str:
        """Return `href` unchanged — the credential is the absence of one.

        Args:
            href: The asset href.

        Returns:
            The same href.
        """
        return href

    def sign_item(self, item: Any) -> Any:
        """Return `item` unchanged.

        Args:
            item: A STAC item.

        Returns:
            The same item.
        """
        return item


def build_signer(signer_type: str, **creds: Any) -> Any:
    """Build the signer named by a catalog `signer:` field.

    The `anonymous` signer is the earthlens-local `_AnonymousS3Signer` and the
    `aws-requester-pays` signer comes from `pyramids.stac` (imported lazily so
    the package imports without the `[stac]` extra); the `mpc-sas` /
    `earthdata` / `cdse` / `cdse-s3` / `bdc-token` provider signers are the
    earthlens-local classes above.

    Args:
        signer_type: One of `"anonymous"`, `"aws-requester-pays"`, `"mpc-sas"`,
            `"earthdata"`, `"cdse"`, `"cdse-s3"`, `"bdc-token"`.
        **creds: Extra credentials forwarded to the selected signer — `region`
            for `anonymous` (pins GDAL at an opt-in region's S3 endpoint) and
            `aws-requester-pays`; `username` / `password` / `token` for
            `earthdata`; `username` / `password` / `client_id` for `cdse`; the
            CDSE S3 credential resolution kwargs for `cdse-s3` (see
            `auth_cdse.s3_credentials`); `token` for `bdc-token` (defaults to
            `$BDC_ACCESS_TOKEN`).

    Returns:
        A signer satisfying the `pyramids.stac.Signer` protocol.

    Raises:
        ValueError: When `signer_type` is not a known signer name.

    Examples:
        - The MPC key maps to earthlens' native SAS signer (no SDK):
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("mpc-sas").name
            'planetary-computer'

            ```
        - Build the CDSE S3 signer from explicit S3 keys:
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("cdse-s3", access_key="ak", secret_key="sk").gdal_env()["AWS_ACCESS_KEY_ID"]
            'ak'

            ```
        - Build the BDC token signer with an explicit token:
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("bdc-token", token="abc").name
            'bdc-token'

            ```
        - An unknown signer name is rejected:
            ```python
            >>> from earthlens.stac import build_signer
            >>> build_signer("nope")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: unknown signer_type 'nope'; expected one of ... 'bdc-token'.

            ```
    """
    if signer_type == "anonymous":
        return _AnonymousS3Signer(region=creds.get("region"))
    if signer_type == "aws-requester-pays":
        from pyramids.stac import AWSRequesterPaysSigner

        return AWSRequesterPaysSigner(region=creds.get("region"))
    if signer_type == "mpc-sas":
        return PlanetaryComputerSigner()
    if signer_type == "earthdata":
        keys = ("username", "password", "token", "refresh_window", "timeout")
        return EarthdataSigner(**{k: creds[k] for k in keys if k in creds})
    if signer_type == "cdse":
        keys = ("username", "password", "client_id", "refresh_window", "timeout")
        return CDSESigner(**{k: creds[k] for k in keys if k in creds})
    if signer_type == "cdse-s3":
        from earthlens.stac import auth_cdse

        access_key, secret_key = auth_cdse.s3_credentials(**creds)
        return CdseS3Signer(access_key=access_key, secret_key=secret_key)
    if signer_type == "bdc-token":
        return BdcTokenSigner(token=creds.get("token"))
    raise ValueError(
        f"unknown signer_type {signer_type!r}; expected one of "
        "'anonymous', 'aws-requester-pays', 'mpc-sas', 'earthdata', 'cdse', "
        "'cdse-s3', 'bdc-token'."
    )
