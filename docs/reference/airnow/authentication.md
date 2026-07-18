# AirNow — authentication

The AirNow `/aq/data/` API requires a free `API_KEY` on **every**
request. This page covers getting a key and the two ways earthlens
resolves it. There is no username/password — a single secret string is
the entire credential set.

## 1. Register a free API key

AirNow access is free. Request a key once at
<https://docs.airnowapi.org/account/request/>; the portal issues an API
key after you confirm your email. Keep it secret — treat it like a
password.

## 2. Supply the key

The backend resolves the key in this order on the first `download()`
call:

1. **An explicit `api_key=`** passed to `EarthLens(...)` /
   `AirNow(...)`.
2. **The `AIRNOW_API_KEY` environment variable.**

If neither resolves, `AirnowAuth.configure()` raises
`earthlens.airnow.AuthenticationError` naming the register URL — it
never blocks on an interactive prompt.

```python
from earthlens.core import EarthLens

# (a) explicit — handy in a notebook
EarthLens(data_source="airnow", api_key="...", ...)

# (b) environment — preferred for scripts / CI
#   export AIRNOW_API_KEY=...        (bash)
#   $env:AIRNOW_API_KEY = "..."      (PowerShell)
EarthLens(data_source="airnow", ...)
```

The explicit `api_key=` always wins over the environment variable. The
key is held as a `pydantic.SecretStr`, so it is never echoed in a
`repr()` or in logs.

## 3. CI secret pattern

Store the key as a CI secret (e.g. a GitHub Actions repository secret
`AIRNOW_API_KEY`) and export it into the job environment; the backend
picks it up via the env-var path with no code change. The gated live
e2e test (`tests/airnow/test_airnow_e2e.py`) is marked `airnow` and
skips cleanly when the variable is absent.
