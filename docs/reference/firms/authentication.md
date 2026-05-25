# FIRMS — authentication

The NASA FIRMS API requires a free `MAP_KEY` on **every** request. This
page covers getting a key and the two ways earthlens resolves it. There
is no username/password and no saved-config-file dance — a single secret
string is the entire credential set.

## 1. Request a free MAP_KEY

FIRMS access is free and needs **no NASA Earthdata login**. Get a key at
<https://firms.modaps.eosdis.nasa.gov/api/map_key/>:

1. Open the page and enter your **email address**.
2. FIRMS emails you a confirmation link — click it.
3. The page then shows your `MAP_KEY`: a ~32-character hex string
   (e.g. `1a2b3c4d5e6f...`), issued instantly.

The same key works for the area CSV download API and for the
`mapkey_status` / `data_availability` endpoints. Keep it secret — treat
it like a password; you can regenerate it anytime from that same page.
Unlike a header-based key, the FIRMS `MAP_KEY` travels as a path segment
in the request URL, so avoid logging full FIRMS URLs.

## 2. Supply the key

The backend resolves the key in this order on construction (the first
`download()` builds and configures `FirmsAuth`):

1. **An explicit `map_key=`** passed to `EarthLens(...)` / `FIRMS(...)`.
2. **The `FIRMS_MAP_KEY` environment variable.**

If neither resolves, `FirmsAuth.configure()` raises
`earthlens.firms.AuthenticationError` naming the map-key URL — it never
blocks on an interactive prompt.

```python
from earthlens import EarthLens

# (a) explicit — handy in a notebook
EarthLens(data_source="firms", map_key="...", ...)

# (b) environment — preferred for scripts / CI
#   export FIRMS_MAP_KEY=...        (bash)
#   $env:FIRMS_MAP_KEY = "..."      (PowerShell)
EarthLens(data_source="firms", ...)
```

## 3. CI / secrets

For automated runs, store the key as a secret and expose it as
`FIRMS_MAP_KEY` rather than hard-coding it. For GitHub Actions:

```yaml
env:
  FIRMS_MAP_KEY: ${{ secrets.FIRMS_MAP_KEY }}
```

The live e2e test (`tests/firms/test_firms_e2e.py`) skips cleanly when
`FIRMS_MAP_KEY` is unset, so CI without the secret stays green.

## 4. The transaction quota

A `MAP_KEY` allows roughly **5000 transactions per rolling 10-minute
window**; each area request is one transaction. The backend chunks long
windows into ≤10-day requests (one transaction each), so a wide
multi-sensor, multi-month pull can approach the limit. Two safeguards:

- **Automatic back-off.** A quota response — HTTP 429, or a FIRMS
  HTTP-200 body whose text reports the transaction limit — triggers a
  capped exponential back-off and retry, so a throttled query slows down
  rather than failing.
- **Check remaining transactions.** You can query your key's status at
  `https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY=<key>`
  before launching a large multi-chunk job.

> **Bad-key responses are detected.** FIRMS often returns errors as
> HTTP 200 with a plain-text body (e.g. `Invalid MAP_KEY.`). The backend
> sniffs the body before parsing and raises
> `earthlens.firms.AuthenticationError` for a bad key rather than handing
> a non-CSV body to the parser.
