# ASF — authentication

ASF SAR data lives behind **NASA Earthdata Login (EDL)** — the same
identity provider every other earthlens NASA / DAAC backend
(`earthdata`, the OPERA / ASF / ARIA / NSIDC granule pulls) uses.
The `asf` backend reuses
[`earthlens.earthdata.EarthdataAuth`](../earthdata/authentication.md)
verbatim; there is **no second credential system** to configure.

If your Earthdata account already works for `earthlens.earthdata`,
it works for `earthlens.asf` too.

## When auth runs (and when it doesn't)

| Step | Auth required? | Why |
|---|---|---|
| `asf_search.geo_search(...)` | No | Catalog search is anonymous |
| `asf_search.granule_search(...)` | No | Same as above |
| `ASFProduct.stack(...)` | No | Baseline search is anonymous |
| `ASFSearchResults.download(...)` | **Yes** | Bearer-token-authenticated S3 / HTTPS pull |

In practice this means a dry-run inspection (`backend._search()`,
`backend._count()`) needs no credentials. Only the actual
`download()` triggers an EDL login. The backend defers
`ASFAuth.configure()` until the first product fetch.

## Credential ladder

The same priority order `EarthdataAuth` uses. Set whichever is most
convenient:

1. **EDL bearer token** — a long-lived JSON Web Token generated from
   the EDL profile (preferred for headless / CI use).
   ```bash
   export EARTHDATA_TOKEN="EDL.xxxxxxxx..."
   ```
2. **Username + password** — the two env vars together.
   ```bash
   export EARTHDATA_USERNAME="your_user"
   export EARTHDATA_PASSWORD="••••••"
   ```
3. **A `~/.netrc` entry** for the EDL host (most portable for an
   interactive workstation):
   ```
   machine urs.earthdata.nasa.gov
       login your_user
       password ••••••
   ```
4. **Explicit kwargs** to the backend:
   ```python
   from earthlens.asf import ASF, ASFCredentials

   creds = ASFCredentials(token="EDL.xxxxxxxx...")
   ASF(..., credentials=creds)
   ```

## Generating an EDL bearer token

1. Register a free account at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov).
2. Sign in and open your profile → **Generate Token**.
3. Copy the JWT and export it as `EARTHDATA_TOKEN`.

Tokens last roughly 90 days and can be regenerated on demand. They
have no per-account password coupling, so a token rotation does not
disrupt other tooling using your username/password.

## Verifying the credentials

```python
from earthlens.asf import ASFAuth, ASFCredentials

auth = ASFAuth(ASFCredentials())  # picks up env / netrc
auth.configure()                  # raises AuthenticationError on failure
assert auth.is_authenticated()
```

Or — equivalently — open the backend with
`EarthLens.authenticate()`:

```python
from earthlens.core import EarthLens

el = EarthLens(data_source="asf", variables=["sentinel-1-slc"],
               start="2024-01-01", end="2024-01-31",
               lat_lim=[0.0, 1.0], lon_lim=[0.0, 1.0], path="asf_out")
el.authenticate()                 # eager EDL login (otherwise lazy on download)
```

## Error path

A failed login raises
`earthlens.asf.AuthenticationError` (a subclass of the cross-backend
`earthlens.base.AuthenticationError`). The message names a fix —
register an account, set the env vars, or add the `.netrc` entry.

```python
from earthlens.base import AuthenticationError

try:
    el.download()
except AuthenticationError as exc:
    print(f"EDL failure: {exc}")
```

The same `except AuthenticationError` clause catches `earthdata`,
`asf`, `cmems`, `eumetsat`, and every other authed earthlens
backend.

## Sharing the EDL handle with `earthlens.earthdata`

If you use both backends in the same process, build a single
`EarthdataAuth` (or rely on the env vars) and both will use it —
the token is cached on the `earthaccess.Auth` handle that
`EarthdataAuth.configure()` returns, and `ASFAuth` reads it from
there. There is no duplicated login round-trip.
