# openEO backend — authentication

openEO authenticates with **OpenID Connect (OIDC)**, not a static key or
credentials file. On the default CDSE endpoint you use the **same account** as
the Copernicus Data Space Ecosystem (and the STAC `cdse` endpoint), but through
a different auth plane: OIDC bearer tokens here, S3 keys there.

## Get a CDSE account

1. Register (free) at <https://dataspace.copernicus.eu>.
2. That account is all you need for the **interactive** flow below. For a
   **headless / CI** run you additionally register an OIDC service account and
   create client credentials (next section).

## Authentication flows

`earthlens.openeo.OpeneoAuth` selects the flow by which credentials are present,
in this order:

1. **client-credentials** *(headless / CI)* — when a `client_id` (+
   `client_secret`) is supplied. Non-interactive; ideal for CI and servers.
2. **refresh-token** — when a `refresh_token` is supplied; exchanged
   non-interactively.
3. **interactive device flow** *(default for humans)* — otherwise
   `authenticate_oidc()` is called. It **reuses a refresh token cached on disk**
   (`~/.openeo/`) from an earlier login; if none is cached it prints a URL +
   code for you to complete in a browser once. After the first login, later
   sessions authenticate automatically from the cache.

### Interactive (first run on your laptop)

No kwargs needed — the first `download()` triggers the device flow and prints a
URL + code:

```python
from earthlens.earthlens import EarthLens

facade = EarthLens(
    data_source="openeo",
    variables={"sentinel-2-l2a-ndvi-monthly": []},
    start="2023-06-01", end="2023-06-30",
    lat_lim=[40.40, 40.45], lon_lim=[3.67, 3.72],
    path="out",
)
facade.download()   # prints: "Visit https://…/device and enter code ABCD-EFGH"
```

The refresh token is cached under `~/.openeo/`, so subsequent runs need no
interaction.

### Headless / CI (client credentials)

Register an OIDC service account on CDSE and pass its credentials — as kwargs:

```python
EarthLens(
    data_source="openeo",
    client_id="sh-xxxxxxxx-xxxx-xxxx",
    client_secret="…",            # kept as a SecretStr internally
    variables={"sentinel-2-l2a-ndvi-monthly": []},
    start="2023-06-01", end="2023-06-30",
    lat_lim=[40.40, 40.45], lon_lim=[3.67, 3.72],
    path="out",
).download()
```

…or via environment variables (read by `OpeneoCredentials.from_env()` when the
matching kwarg is not passed):

| Env var | Meaning |
|---|---|
| `OPENEO_CLIENT_ID` | OIDC client id (client-credentials flow) |
| `OPENEO_CLIENT_SECRET` | OIDC client secret |
| `OPENEO_REFRESH_TOKEN` | a pre-minted OIDC refresh token (refresh-token flow) |
| `OPENEO_PROVIDER_ID` | OIDC provider id, when the backend advertises several |

```bash
export OPENEO_CLIENT_ID="sh-xxxxxxxx-xxxx-xxxx"
export OPENEO_CLIENT_SECRET="…"
```

The CDSE client-credentials setup is documented at
<https://documentation.dataspace.copernicus.eu/APIs/openEO/authentication/client_credentials.html>.

## Choosing the endpoint

```python
EarthLens(data_source="openeo", endpoint="openeo-platform", ...)   # NoR federation
EarthLens(data_source="openeo", endpoint="https://my.openeo.host", ...)  # custom
```

The endpoint is resolved at construction, so an unknown alias fails fast.

## Errors

A missing/failed OIDC flow raises `earthlens.openeo.AuthenticationError`
(a subclass of `earthlens.base.AuthenticationError`, so one `except` clause
catches every backend's auth failure). The message points at the CDSE account
URL and the headless-setup option. A missing `[openeo]` extra raises an
`ImportError` naming `earthlens[openeo]`.

Authentication is **lazy**: the connection is opened and the OIDC flow runs on
the first `download()` (or first `OpeneoAuth.connection()`), not at
construction — so building the facade never blocks on the network.
