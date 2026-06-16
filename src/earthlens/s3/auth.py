"""Unsigned (and optionally profile-signed) AWS S3 authentication.

Hosts `S3Credentials` and `S3Auth`, the `earthlens.s3` backend's
:class:`~earthlens.base.AbstractAuth` implementation. The AWS Open-Data
buckets this backend targets (`nsf-ncar-era5`, `sentinel-cogs`,
`noaa-goes16` / `noaa-goes18`, `copernicus-dem-30m` / `-90m`,
`esa-worldcover`) are public, so the default path builds an **unsigned**
`boto3` client — no credentials, no `~/.aws` config required.

The class collapses the two ad-hoc clients the legacy module built (one
in `S3._initialize`, a duplicate in `Catalog.initialize`) into a single
auth surface, and imports `boto3` / `botocore` lazily inside
:meth:`S3Auth.configure` so `import earthlens.s3` works without the
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
            >>> from earthlens.s3.auth import S3Credentials
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
            >>> from earthlens.s3.auth import S3Auth, S3Credentials
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
