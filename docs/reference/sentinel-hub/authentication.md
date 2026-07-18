# Sentinel Hub — authentication

Sentinel Hub authenticates **non-interactively** with an OAuth2
*client-credentials* pair (a `client_id` / `client_secret`). This is closest to
GEE's service-account model and unlike openEO's interactive OIDC device flow.
`sentinelhub-py` mints and refreshes the bearer token internally from the
configuration, so there is no explicit login step.

## 1. Mint an OAuth client in the CDSE Dashboard

1. Sign in to the **CDSE Dashboard**:
   <https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings>
2. Under **OAuth clients**, click **Create**.
3. Name the client and set the **Grant Type** to **`Client Credentials`**.
4. Copy the generated **client id** and **client secret** (the secret is shown
   once).

A CDSE account is free. Note that the Sentinel Hub `client_id` / `client_secret`
are a **different** credential from the openEO OIDC token and the STAC `cdse` S3
keys — the same account has three separate auth planes.

## 2. Provide the credentials to earthlens

The backend resolves credentials in this order:

1. **Environment** — `SENTINELHUB_CLIENT_ID` / `SENTINELHUB_CLIENT_SECRET` (and
   optional `SENTINELHUB_PROFILE`). The `sentinelhub-py`-native `SH_CLIENT_ID` /
   `SH_CLIENT_SECRET` / `SH_PROFILE` are accepted as a fallback.
2. **Constructor kwargs** — `client_id=` / `client_secret=` (these win over the
   environment).
3. **Saved `SHConfig` profile** — `profile=` (a profile written earlier with
   `SHConfig.save("name")`).

### Environment variables (recommended for CI / headless)

=== "bash"

    ```bash
    export SENTINELHUB_CLIENT_ID="your-client-id"
    export SENTINELHUB_CLIENT_SECRET="your-client-secret"
    ```

=== "PowerShell"

    ```powershell
    $env:SENTINELHUB_CLIENT_ID = "your-client-id"
    $env:SENTINELHUB_CLIENT_SECRET = "your-client-secret"
    ```

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-10", end="2020-06-20",
    variables={"sentinel-2-l2a-ndvi": []},
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
)  # credentials read from SENTINELHUB_CLIENT_ID / SENTINELHUB_CLIENT_SECRET
```

### Explicit keyword arguments

```python
el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-10", end="2020-06-20",
    variables={"sentinel-2-l2a-ndvi": []},
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
    client_id="your-client-id",
    client_secret="your-client-secret",
)
```

### A saved `SHConfig` profile

```python
from sentinelhub import SHConfig

cfg = SHConfig()
cfg.sh_client_id = "your-client-id"
cfg.sh_client_secret = "your-client-secret"
cfg.save("cdse")          # writes ~/.config/sentinelhub/config.toml

# then, in earthlens:
el = EarthLens(data_source="sentinel-hub", ..., profile="cdse")
```

If no credentials are resolvable, the backend raises `AuthenticationError` with
a pointer back to the CDSE Dashboard.

## CDSE vs commercial deployment

`endpoint=` selects the deployment:

| `endpoint=` | Base URL | Token URL |
|---|---|---|
| `"cdse"` (default) | `https://sh.dataspace.copernicus.eu` | CDSE Keycloak |
| `"commercial"` | `https://services.sentinel-hub.com` | commercial Keycloak |

A full `http(s)://` base URL is also accepted (the token URL is paired by host).

```python
el = EarthLens(data_source="sentinel-hub", ..., endpoint="commercial")
```
