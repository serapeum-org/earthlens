"""Unsigned (and optionally profile-signed) AWS S3 transport.

Hosts `S3Credentials` and `S3Auth`, a shared
:class:`~earthlens.base.AbstractAuth` implementation for the AWS
Open-Data buckets that several backends read — `nsf-ncar-era5`
(`earthlens.s3`), `copernicus-dem-30m` / `-90m` (`earthlens.dem`),
`noaa-goes16` / `noaa-goes18`, `sentinel-cogs` and `esa-worldcover`.
Those buckets are public, so the default path builds an **unsigned**
`boto3` client — no credentials, no `~/.aws` config required.

Unsigned S3 is a transport rather than any one provider's concern, so it
lives in `earthlens.base` beside `HttpClient`; a backend that reads an
open bucket imports it from here instead of depending on a sibling
backend.

`boto3` / `botocore` are imported lazily inside :meth:`S3Auth.configure`,
so this module — and therefore `earthlens.base` — carries no SDK of its
own; the import raises only when a client is actually built without the
`[s3]` extra installed.

An optional `aws_profile` selects a named profile from the local AWS
config for the rare signed / requester-pays case; it is a thin escape
hatch, not a full credential-management surface (requester-pays datasets
such as `usgs-landsat` are out of the first-cut scope).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from earthlens.base import AbstractAuth

__all__ = ["S3Auth", "S3Credentials"]


class S3Credentials(BaseModel):
    """Credentials for the AWS S3 backend.

    The seeded datasets are all public, so the default (all fields
    unset) yields an unsigned client. Set `aws_profile` to sign requests
    with a named profile from the local AWS configuration — needed only
    for signed / requester-pays buckets, which are out of the first-cut
    scope.

    Attributes:
        aws_profile: Name of a profile in `~/.aws/credentials` /
            `~/.aws/config` to sign requests with. `None` (the default)
            builds an unsigned client suitable for public buckets.
        signed: Force a signed client from the default credential chain
            (without naming a profile) — used for requester-pays buckets.
        region: AWS region to build the client in (`None` = default).

    Examples:
        - The default is unsigned (no profile):
            ```python
            >>> from earthlens.base.s3 import S3Credentials
            >>> S3Credentials().aws_profile is None
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    aws_profile: str | None = None
    signed: bool = False
    region: str | None = None


class S3Auth(AbstractAuth[S3Credentials]):
    """Build an unsigned (or profile-signed) `boto3` S3 client lazily.

    Implements the :class:`~earthlens.base.AbstractAuth` contract for
    public AWS Open-Data buckets. `configure()` constructs the client on
    first use (importing `boto3` lazily so the package imports without
    the `[s3]` extra); `is_authenticated()` reports whether the client
    exists; :meth:`client` is the accessor the backend's fetch step
    calls.

    Because the target buckets are public, "authenticated" simply means
    "a client has been built" — there is no token to mint or expire.

    Examples:
        - The client is not built until `configure()` runs:
            ```python
            >>> from earthlens.base.s3 import S3Auth, S3Credentials
            >>> auth = S3Auth(S3Credentials())
            >>> auth.is_authenticated()
            False

            ```
    """

    def __init__(self, credentials: S3Credentials | None = None) -> None:
        """Store credentials and reset the (lazily built) client.

        Args:
            credentials: The :class:`S3Credentials` to use. `None`
                defaults to unsigned public access.
        """
        super().__init__(credentials or S3Credentials())
        self._client: Any = None

    def configure(self) -> None:
        """Build the `boto3` S3 client if it does not exist yet.

        Idempotent: returns immediately once :meth:`is_authenticated`
        is `True`. Imports `boto3` / `botocore` lazily so importing the
        package without the `[s3]` extra does not fail.

        Raises:
            ImportError: When the `[s3]` extra (`boto3`) is not
                installed. The message names `earthlens[s3]`.
        """
        if self.is_authenticated():
            return
        try:
            import boto3
            import botocore.client
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ImportError(
                "The Amazon S3 backend requires the optional 'boto3' "
                "dependency. Install it with: pip install earthlens[s3]"
            ) from exc

        region = self._creds.region
        if self._creds.aws_profile:
            session = boto3.Session(profile_name=self._creds.aws_profile)
            self._client = session.client("s3", region_name=region)
        elif self._creds.signed:
            # Requester-pays buckets need a signed client (default credential
            # chain); the caller's AWS account is billed for the requests.
            self._client = boto3.client("s3", region_name=region)
        else:
            self._client = boto3.client(
                "s3",
                region_name=region,
                config=botocore.client.Config(signature_version=botocore.UNSIGNED),
            )

    def is_authenticated(self) -> bool:
        """Return `True` once the S3 client has been built."""
        return self._client is not None

    def client(self) -> Any:
        """Return the S3 client, building it on first access.

        Returns:
            The configured `boto3` S3 client.
        """
        self.configure()
        return self._client

    def close(self) -> None:
        """Drop the reference to the S3 client."""
        self._client = None
