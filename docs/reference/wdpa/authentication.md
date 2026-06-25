# Protected Planet (WDPA) — authentication

The Protected Planet v4 API requires a personal token on **every**
request, passed as a `?token=` **query parameter** (not an
`Authorization: Bearer` header). This page covers getting a token and the
two ways earthlens resolves it. There is no username/password — a single
secret string is the entire credential set, and the backend uses core
`requests`, so there is no SDK extra to install.

## 1. Request a token

Request a personal API token at <https://api.protectedplanet.net/request>.
Keep it secret — treat it like a password.

> **v3 is being retired.** Protected Planet API v3 is taken down on
> **1 May 2026**; earthlens targets **v4** (`api.protectedplanet.net/v4`),
> which is current and reflects the merged WDPA + WD-OECM database (WDPCA).

## 2. Supply the token

The backend resolves the token in this order when the backend is built
(it surfaces a missing token early, before any request):

1. **An explicit `token=`** passed to `EarthLens(...)` / `WDPA(...)`.
2. **The `WDPA_TOKEN` environment variable.**

If neither resolves, `WdpaAuth.configure()` raises
`earthlens.wdpa.AuthenticationError` naming `WDPA_TOKEN` and the
request URL — it never blocks on an interactive prompt.

```python
from earthlens import EarthLens

# (a) explicit — handy in a notebook
EarthLens(data_source="wdpa", token="...", variables=["KEN"], ...)

# (b) environment — preferred for scripts / CI
#   export WDPA_TOKEN=...        (bash)
#   $env:WDPA_TOKEN = "..."      (PowerShell)
EarthLens(data_source="wdpa", variables=["KEN"], ...)
```

The explicit `token=` always wins over the environment variable. The
token is held as a `pydantic.SecretStr`, so it is never echoed in a
`repr()` or in logs.

## 3. CI secret pattern

Store the token as a CI secret (e.g. a GitHub Actions repository secret
`WDPA_TOKEN`) and export it into the job environment; the backend picks
it up via the env-var path with no code change. The gated live e2e test
(`tests/wdpa/test_wdpa_e2e.py`) skips cleanly when the variable is
absent.

## 4. Licensing — read before redistributing

A token authenticates you, but WDPA data carries a **custom UNEP-WCMC
license** (not Creative Commons): commercial use needs prior written
permission and redistribution is restricted. Every `download()` raises a
`LicenseWarning`, and you must cite the database as the
[Protected Planet terms](https://www.protectedplanet.net/en/legal)
require. See [Usage](usage.md) for the request knobs and
[Introduction](introduction.md) for the licensing detail.
