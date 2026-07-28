# EUMETSAT Data Store — authentication

The EUMETSAT Data Store uses **OAuth2**: a single **consumer key** +
**consumer secret** pair mints a short-lived (~1 hour) bearer token that
authorises every Data Store request. `eumdac` refreshes the bearer
internally; `earthlens` re-mints a fresh token only once the cached one
has expired.

## Get a consumer key and secret

1. Create a free EUMETSAT account and sign in at
   <https://eoportal.eumetsat.int/>.
2. Open the API-key page: <https://api.eumetsat.int/api-key/>.
3. Copy the **Consumer Key** and **Consumer Secret** shown there. They
   are the only credentials the backend needs.

## Supplying the credentials

`EumetsatAuth` resolves the pair in this order — the first source that
yields **both** halves wins:

### 1. Environment variables (recommended for CI)

```bash
export EUMETSAT_CONSUMER_KEY="your-consumer-key"
export EUMETSAT_CONSUMER_SECRET="your-consumer-secret"
```

### 2. Constructor / facade keyword arguments

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="eumetsat",
    variables={"msg-hrseviri": ["HRSEVIRI"]},
    start="2024-06-01", end="2024-06-01",
    lat_lim=[0, 10], lon_lim=[0, 10],
    consumer_key="your-consumer-key",
    consumer_secret="your-consumer-secret",
)
```

The `consumer_secret` is held as a `pydantic.SecretStr`, so it never
appears in `repr()` or logs.

### 3. A `~/.eumdac/credentials` file

This is the file the `eumdac` CLI writes with `eumdac set-credentials`.
It is a single line:

```text
your-consumer-key,your-consumer-secret
```

The directory is overridable with the `EUMDAC_CONFIG_DIR` environment
variable (the same variable `eumdac` itself honours), or you can point at
an explicit file with the `credentials_file=` keyword:

```python
el = EarthLens(
    data_source="eumetsat",
    variables={"msg-hrseviri": ["HRSEVIRI"]},
    start="2024-06-01", end="2024-06-01",
    lat_lim=[0, 10], lon_lim=[0, 10],
    credentials_file="/path/to/credentials",
)
```

## Token lifecycle

* `EumetsatAuth.configure()` is **idempotent** — it mints a token once
  and short-circuits on subsequent calls while the token is still valid.
* `is_authenticated()` is a cheap, network-free predicate. It returns
  `False` once the cached token's `expiration` (a `datetime`) has passed,
  so the next `configure()` re-mints.
* No credentials resolved → `earthlens.eumetsat.AuthenticationError`
  (a subclass of `earthlens.base.AuthenticationError`) naming the API-key
  page.

## Prerequisite

The `eumetsat` extra installs `eumdac`:

```bash
pip install earthlens[eumetsat]
```

A missing extra surfaces as a friendly `ImportError` naming
`earthlens[eumetsat]` the first time you authenticate or download.

## CI secrets

The live end-to-end test and the `e2e-eumetsat` GitHub Actions lane read
the `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` repository
secrets and skip cleanly when they are unset.
