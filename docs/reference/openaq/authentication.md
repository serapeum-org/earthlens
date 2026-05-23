# OpenAQ — authentication

The OpenAQ v3 API requires a free `X-API-Key` on **every** request.
This page covers getting a key and the two ways earthlens resolves it.
There is no username/password and no saved-config-file dance — a single
secret string is the entire credential set.

## 1. Register a free API key

OpenAQ access is free. Sign up once at
<https://explore.openaq.org/register>; the portal issues an API key
immediately. Keep it secret — treat it like a password.

> **v1 / v2 are retired.** OpenAQ API v1 and v2 were shut down on
> 31 January 2025. Only v3 (and therefore a v3 key) works.

## 2. Supply the key

The backend resolves the key in this order on the first
`download()` call:

1. **An explicit `api_key=`** passed to `EarthLens(...)` /
   `OpenAQ(...)`.
2. **The `OPENAQ_API_KEY` environment variable.**

If neither resolves, `OpenaqAuth.configure()` raises
`earthlens.openaq.AuthenticationError` naming the register URL — it
never blocks on an interactive prompt.

```python
from earthlens import EarthLens

# (a) explicit — handy in a notebook
EarthLens(data_source="openaq", api_key="...", ...)

# (b) environment — preferred for scripts / CI
#   export OPENAQ_API_KEY=...        (bash)
#   $env:OPENAQ_API_KEY = "..."      (PowerShell)
EarthLens(data_source="openaq", ...)
```

The explicit `api_key=` always wins over the environment variable. The
key is held as a `pydantic.SecretStr`, so it is never echoed in a
`repr()` or in logs.

## 3. CI secret pattern

Store the key as a CI secret (e.g. a GitHub Actions repository secret
`OPENAQ_API_KEY`) and export it into the job environment; the backend
picks it up via the env-var path with no code change. The gated live
e2e test (`tests/openaq/test_openaq_e2e.py`) skips cleanly when the
variable is absent.

## 4. Rate limits — size your requests

A key authenticates you, but the free tier still rate-limits (the
service returns `429 Too Many Requests` with a `Retry-After` header
when you exceed it). Because the locations → sensors → measurements
fan-out can be large, keep requests bounded:

- Prefer a **server-side rollup** (`temporal_resolution="daily"`) over
  raw measurements for long windows.
- Cap the fan-out with **`max_locations`** (default `500`) and
  **`max_sensors_per_location`**.
- Start with a **small bbox / short window** and widen as needed.

The backend already honours `429` / `Retry-After` with capped
exponential back-off, so a momentary throttle is retried rather than
fatal — but sizing the request well is what keeps a continental query
from taking hours. See [Usage](usage.md) for the knobs.
