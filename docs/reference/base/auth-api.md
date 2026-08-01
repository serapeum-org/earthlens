# Authentication API

The shared auth contract. A provider that needs credentials exposes a `<Provider>Auth` / `<Provider>Credentials`
pair in its own `auth.py` with environment-variable fallbacks, and raises `AuthenticationError` on failure.

```python
from earthlens.base import AbstractAuth, AuthenticationError
```

For which provider needs what, see [Supported providers](../providers.md); for a worked example of supplying
credentials, see [Authentication examples](../../examples/authentication.md).

## `AbstractAuth`

::: earthlens.base.AbstractAuth

## `AuthenticationError`

::: earthlens.base.AuthenticationError

## S3 credentials

Used by the two backends that resolve AWS credentials through this helper — `amazon-s3` (ERA5 and the other
public buckets) and `dem` (Copernicus DEM). The other bucket-backed backends open their clients directly.

::: earthlens.base.S3Auth

::: earthlens.base.S3Credentials
