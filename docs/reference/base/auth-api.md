# Authentication API

The shared auth contract. A provider that needs credentials exposes a `<Provider>Auth` / `<Provider>Credentials`
pair in its own `auth.py` with environment-variable fallbacks, and raises `AuthenticationError` on failure.

```python
from earthlens.base import AbstractAuth, AuthenticationError
```

For which provider needs what, see [Supported providers](../providers.md); for a worked example of supplying
credentials, see [Authentication examples](../../examples/authentication.md).

## `AbstractAuth`

::: earthlens.base.auth.AbstractAuth

## `AuthenticationError`

::: earthlens.base.auth.AuthenticationError

## S3 credentials

Shared by the backends that read public or requester-pays AWS buckets (ERA5, Copernicus DEM, GOES, NWM, NEXRAD).

::: earthlens.base.s3.S3Auth

::: earthlens.base.s3.S3Credentials
