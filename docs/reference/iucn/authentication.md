# IUCN Red List — authentication

The IUCN Red List v4 API requires a token on **every** request, sent as
an `Authorization: Bearer <token>` header (the retired v3 `?token=` query
param is gone). This page covers getting a token and the two ways
earthlens resolves it. There is no username/password — a single secret
string is the entire credential set, and the backend uses core `requests`,
so there is no SDK extra to install.

## 1. Sign up for a token

Sign up for a free token at <https://api.iucnredlist.org/users/sign_up>;
after confirming your account the token is shown on your account page,
where you can also cycle it. Keep it secret — treat it like a password.

> **v3 is retired.** The old v3 API (`apiv3.iucnredlist.org`, `?token=`
> query param) was retired in March 2025 and v3 accounts were **not**
> migrated — a fresh v4 sign-up is required. earthlens targets v4
> (`api.iucnredlist.org/api/v4`).

## 2. Supply the token

The backend resolves the token in this order when the backend is built
(it surfaces a missing token early, before any request):

1. **An explicit `token=`** passed to `EarthLens(...)` / `IUCN(...)`.
2. **The `IUCN_TOKEN` environment variable.**

If neither resolves, `IucnAuth.configure()` raises
`earthlens.iucn.AuthenticationError` naming `IUCN_TOKEN` and the sign-up
URL — it never blocks on an interactive prompt.

```python
from earthlens.core import EarthLens

# (a) explicit — handy in a notebook
EarthLens(data_source="iucn", token="...", variables=["species:Panthera leo"], ...)

# (b) environment — preferred for scripts / CI
#   export IUCN_TOKEN=...        (bash)
#   $env:IUCN_TOKEN = "..."      (PowerShell)
EarthLens(data_source="iucn", variables=["species:Panthera leo"], ...)
```

The explicit `token=` always wins over the environment variable. The
token is held as a `pydantic.SecretStr`, so it is never echoed in a
`repr()` or in logs.

## 3. CI secret pattern

Store the token as a CI secret (e.g. a GitHub Actions repository secret
`IUCN_TOKEN`) and export it into the job environment; the backend picks
it up via the env-var path with no code change. The gated live e2e test
(`tests/iucn/test_iucn_e2e.py`) skips cleanly when the variable is
absent.

## 4. Rate limits and licensing

IUCN advises a **~2-second delay between calls** (the backend honours
this and retries `429` / `Retry-After`); heavy use should contact the Red
List Unit. Critically, Red List data is **`CC-BY-NC`** and may **not** be
redistributed without a written IUCN waiver, so **every** `download()`
raises a `LicenseWarning` and the data is never shipped as package data —
you fetch under your own token and agreement. Cite the Red List as its
[Terms of Use](https://www.iucnredlist.org/terms/terms-of-use) require.
