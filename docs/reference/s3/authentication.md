# Amazon S3 — authentication

The AWS Open-Data buckets `earthlens.s3` targets are **public**, so the default
path needs **no credentials**: the backend builds an *unsigned* `boto3` client
(`botocore.client.Config(signature_version=botocore.UNSIGNED)`). There is no key
to obtain, no config file to write, and no token to refresh.

## Default (unsigned)

```python
from earthlens.s3 import S3

src = S3(
    start="2021-01-01", end="2021-01-01",
    lat_lim=[0.40, 0.45], lon_lim=[6.40, 6.45],
    dataset="copernicus-dem",
)
src.download()  # no credentials needed
```

## Optional: a signed profile

For the rare case of a signed / requester-pays bucket, pass `aws_profile=` to
sign requests with a named profile from your local AWS configuration
(`~/.aws/credentials` / `~/.aws/config`):

```python
src = S3(..., dataset={...}, aws_profile="my-profile")
```

This is a thin escape hatch, not a full credential-management surface. The
first-cut registry contains only unsigned public datasets; requester-pays
datasets (e.g. `usgs-landsat`, `naip-source`) are out of scope.

## The auth class

Authentication is encapsulated in `earthlens.s3.S3Auth`, an
`earthlens.base.AbstractAuth` subclass over an `S3Credentials` value object
(`aws_profile: str | None`). `boto3` is imported lazily inside
`S3Auth.configure()`, so importing the package without the `[s3]` extra does not
fail; the missing-extra `ImportError` (naming `earthlens[s3]`) is raised only
when a client is actually built.

```python
>>> from earthlens.s3 import S3Auth, S3Credentials
>>> auth = S3Auth(S3Credentials())   # unsigned
>>> auth.is_authenticated()
False
>>> client = auth.client()           # builds + caches the boto3 client
>>> auth.is_authenticated()
True
```
