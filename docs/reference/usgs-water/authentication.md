# USGS Water — Authentication

USGS NWIS / Water Data is **public** — the backend works **anonymously** with
no credentials. A token is *optional* and only raises your rate limit.

## When you need a token

The modern `api.waterdata.usgs.gov` endpoint rate-limits anonymous requests
aggressively: after one or two calls it returns HTTP 429 ("obtain an API
token or try again later"). The backend's default `api="auto"` copes by
falling back to the legacy `waterservices.usgs.gov` endpoint on a 429, so
**most workflows run fine anonymously**. You only need a token when:

* you want to use the modern endpoint directly (`api="waterdata"`), or
* you use a **modern-only** service — `samples` or `field-measurements` —
  whose legacy function was removed (so there is no anonymous fallback), or
* you make enough requests that the legacy endpoint also throttles you.

## Getting a Personal Access Token

Register for a free USGS Personal Access Token (PAT) at:

<https://api.waterdata.usgs.gov/docs/ogcapi/keys/>

## Supplying the token

In priority order, the backend resolves the token from:

1. the explicit **`api_token=`** argument, then
2. the **`API_USGS_PAT`** environment variable.

```python
from earthlens.core import EarthLens

# explicit
EarthLens(data_source="usgs-water", api_token="YOUR_PAT", ...)
```

```bash
# or via the environment (the channel the dataretrieval SDK reads)
export API_USGS_PAT="YOUR_PAT"     # PowerShell: $env:API_USGS_PAT = "YOUR_PAT"
```

When a token is resolved the backend exports it to `API_USGS_PAT` (the variable
the `dataretrieval` SDK reads) and uses the modern endpoint without throttling.
With no token anywhere, the backend runs anonymously — this is a normal mode,
not an error.

## Behaviour summary

| `api=` | token set | token absent |
|---|---|---|
| `"auto"` (default) | modern endpoint | modern, then legacy on a 429 (warns once) |
| `"waterdata"` | modern endpoint | modern; a 429 surfaces as an error |
| `"legacy"` | legacy endpoint | legacy endpoint |

For the underlying class, see `earthlens.usgs_water.UsgsWaterAuth` /
`UsgsWaterCredentials` in the [API reference](usgs_water.md).
