# JAXA — authentication

JAXA's archive is reached through two protocols, only one of which needs
credentials. The `jaxa-earth` half (the official `jaxa.earth` API over
STAC + COG) is **authless** — `JaxaAuth.configure()` is a no-op for it.
The `gportal` half (G-Portal mission archive over SFTP) needs a free
JAXA G-Portal account.

## 1. Register a free G-Portal account

- Sign up at <https://gportal.jaxa.jp/gpr/user/regist1>. The form asks
  for an email, a username (this becomes `$GPORTAL_USERNAME`), a
  password, name, organisation, country, and purpose.
- **Password complexity rule.** G-Portal requires characters from at
  least 3 of these 4 categories: uppercase, lowercase, digits, and
  symbols — but **only these four symbols are accepted**: `-` `_` `@`
  `.`. Common choices like `!` `#` `$` `&` `*` are rejected. Pick a
  password like `Earthlens2026_jaxa` to pass.
- JAXA emails a confirmation link. Click it; until you do, the account
  cannot SFTP-download.
- Search is **anonymous** even after you sign up — only the actual
  `gportal.download(...)` step uses the credentials.

## 2. Supply the credentials

`JaxaAuth.configure()` resolves the username and password in this order
for the `gportal` protocol:

1. **Explicit kwargs** to `EarthLens(...)` / `JAXA(...)`:
   `gportal_username=` and `gportal_password=`.
2. **Environment variables** `GPORTAL_USERNAME` and `GPORTAL_PASSWORD`.

The `gportal` SDK does **not** auto-read either variable in v0.4.0;
`JaxaAuth` reads them and threads them straight into
`gportal.download(username=, password=)` as call-site kwargs so the SDK's
module-level credential globals stay untouched between requests.

If neither resolves on a `gportal` request, `JaxaAuth.configure()`
raises :class:`earthlens.jaxa.AuthenticationError` naming both
environment variables and the registration URL — it never blocks on an
interactive prompt.

```python
from earthlens import EarthLens

# (a) explicit — handy in a notebook
EarthLens(
    data_source="jaxa",
    variables=["sgli-l3-nwlr"],
    gportal_username="...",
    gportal_password="...",
    start="2024-01-01", end="2024-01-02",
    lat_lim=[0.0, 30.0], lon_lim=[120.0, 150.0],
    path="./out",
)

# (b) environment — preferred for scripts / CI
#   export GPORTAL_USERNAME=...  GPORTAL_PASSWORD=...   (bash)
#   $env:GPORTAL_USERNAME = "..."; $env:GPORTAL_PASSWORD = "..."   (PowerShell)
EarthLens(data_source="jaxa", variables=["sgli-l3-nwlr"], ...)
```

The explicit kwargs always win over the environment. The password is
held as a `pydantic.SecretStr`, so it is never echoed in a `repr()` or
in logs.

## 3. The `JaxaAuth` protocol binding

`JaxaAuth` is constructed with a `protocol=` kwarg that binds it to a
single protocol — the backend does this automatically based on the
catalog rows the user requested. This makes the parent contract's
no-arg `AbstractAuth.configure()` (which `AbstractDataSource.authenticate()`
calls) act on the right side:

* `JaxaAuth(creds, protocol="jaxa-earth").configure()` is a no-op.
* `JaxaAuth(creds, protocol="gportal").configure()` resolves and caches
  the credentials, raising :class:`earthlens.jaxa.AuthenticationError`
  on miss.

`EarthLens(...).authenticate()` therefore fails-fast on a `gportal`
request without credentials — the SFTP download is not attempted with
empty auth.

## 4. CI secret pattern

Store the two credentials as CI secrets (e.g. GitHub Actions
repository secrets `GPORTAL_USERNAME` / `GPORTAL_PASSWORD`) and export
them into the job environment. The backend picks them up via the
env-var path with no code change. The gated live e2e tests under
`pytest -m "jaxa and e2e"` skip cleanly when either is absent.

## 5. JAXA Earth API (no credentials)

The `jaxa.earth` SDK that drives the `jaxa-earth` protocol does **not**
require any registration. The STAC catalogue and the COG assets are
served over public HTTPS. `JaxaAuth.configure()` short-circuits for
this protocol and never touches the network.

## 6. Things to watch

- **Maintenance windows.** G-Portal ran a 2024-10 → 2025-03 maintenance
  window with an alternate host (`repo.gportal.jaxa.jp`). The primary
  endpoint `ftp.gportal.jaxa.jp:2051` is operational again as of 2026-06.
- **Single-maintainer SDK.** The community `gportal` package has had no
  releases since 2023-05-11; its classifiers only test Python 3.9-3.11
  upstream. earthlens targets 3.11-3.14 — see the
  [Introduction](introduction.md) for the bus-factor risk note.
- **Do NOT install `gportal[gcomc]`.** That extra pins `numpy<2`, which
  conflicts with this repo's numpy 2.x line. The base `gportal` is
  enough for the JAXA backend; HDF5 readers for SGLI products are
  downstream.
