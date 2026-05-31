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

## Requester-pays datasets (`usgs-landsat`, `naip-source`)

Two registered datasets — **`usgs-landsat`** and **`naip-source`** — live in
**requester-pays** buckets. These need **valid AWS credentials** and **bill your
AWS account** for every request and byte downloaded (a single Landsat band or
NAIP quad is pennies; large pulls add up). There is no keyless path — that's an
AWS billing rule, not a backend limitation.

The backend handles the mechanics for you: when a dataset has
`requester_pays: true`, it builds a **signed** client (from the default
credential chain, in the dataset's region) and sends `RequestPayer="requester"`
on every list and download. You only need to make valid credentials resolvable.

### 1. Create an access key

If you don't have an AWS account, create one at
[aws.amazon.com](https://aws.amazon.com/) (a payment method is required —
requester-pays egress is billed to you).

Create a least-privilege IAM user with programmatic access:

1. AWS Console → **IAM** → **Users** → **Create user** (e.g. `earthlens-test`).
2. Attach a permissions policy. The managed **`AmazonS3ReadOnlyAccess`** works;
   tighter, scoped to just these two buckets:

    ```json
    {
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": [
          "arn:aws:s3:::usgs-landsat", "arn:aws:s3:::usgs-landsat/*",
          "arn:aws:s3:::naip-source", "arn:aws:s3:::naip-source/*"
        ]
      }]
    }
    ```

    Requester-pays buckets are readable by **any** authenticated AWS identity —
    you don't need to be granted bucket access; you need a valid identity,
    permission to call S3, and you pay egress.
3. Open the user → **Security credentials** → **Create access key** →
   *Application running outside AWS* → copy the **Access key ID** and
   **Secret access key** (the secret is shown only once).

### 2. Configure the credentials (any one)

The backend uses boto3's default credential chain, so any standard method works.
You do **not** need to set a region — the backend pins `us-west-2` (Landsat) and
`us-east-1` (NAIP) itself.

=== "Environment variables"

    ```bash
    export AWS_ACCESS_KEY_ID="AKIA...new..."
    export AWS_SECRET_ACCESS_KEY="...secret..."
    ```

=== "aws configure"

    ```bash
    aws configure        # paste the key id + secret
    ```

=== "~/.aws/credentials"

    ```ini
    [default]
    aws_access_key_id = AKIA...new...
    aws_secret_access_key = ...secret...
    ```

Verify the key resolves: `aws sts get-caller-identity`.

### 3. Download

With valid credentials present, the requester-pays datasets work like any other
(addressed by an explicit `scene=` / `tile=` — see [Usage](usage.md)):

```python
from earthlens.s3 import S3

S3(start="2021-09-01", end="2021-09-01",
   lat_lim=[36.5, 37.0], lon_lim=[-120.5, -120.0],
   dataset="usgs-landsat", variables=["red", "nir"],
   scene="LC08_L2SP_039037_20210901_20210910_02_T1").download()
```

A named profile is also supported via `aws_profile="my-profile"` (for a signed
profile other than the default chain).

> **Cost & safety:** you are billed for requester-pays downloads; never commit
> credentials; the gated e2e tests (`tests/s3/test_e2e.py`) skip automatically
> when no credential chain is found, so CI never runs them.

## The auth class

Authentication is encapsulated in `earthlens.s3.S3Auth`, an
`earthlens.base.AbstractAuth` subclass over an `S3Credentials` value object
(`aws_profile` / `signed` / `region`). It builds an **unsigned** client by
default, or a **signed** client when `signed=True` (requester-pays) or a profile
is named. `boto3` is imported lazily inside
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
