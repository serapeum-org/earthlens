# NASA Earthdata — authentication

The Earthdata backend authenticates once per process against **Earthdata
Login (EDL)** and reuses that session across every DAAC.

## Installation

The backend needs the `earthdata` extra:

```bash
pip install earthlens[earthdata]
```

This pulls [`earthaccess`](https://earthaccess.readthedocs.io) `>=0.18`,
which **requires Python ≥ 3.12**. On Python 3.11 the dependency will not
resolve — the rest of earthlens still works, but the Earthdata backend
is unavailable until you move to 3.12+. The `earthaccess` import is
lazy, so `import earthlens.earthdata` succeeds without the extra; the
friendly `ImportError` (naming `earthlens[earthdata]`) is only raised
when you actually log in.

## Get an account

Earthdata Login (EDL) is free and takes a couple of minutes. It is a
plain username + password — no API key or token is required for this
backend.

1. Go to [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov) and
   click **Register**.
2. Choose a **username** and **password** and fill in the email, name,
   and affiliation / study-area fields NASA requests.
3. **Verify your email** via the link NASA sends; your account is then
   active.
4. Your **username + password** are the credentials — wire them in via
   the sources below.

Two one-time, dataset-specific steps you may hit:

- **Authorize the DAAC application.** Some DAACs require you to approve
  their application once. After logging in at
  [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov), go to
  **Applications → Authorized Apps** and approve as prompted (or you
  will simply be asked the first time you reach that DAAC).
- **Accept a EULA.** A few collections (e.g. GPM IMERG at GES DISC) ask
  you to accept a licence agreement on first download.

## Credential sources (in priority order)

`EarthData(...)` resolves a login strategy automatically, in this
priority order:

1. **EDL bearer token** — an explicit `EarthData(..., token=...)` or the
   `EARTHDATA_TOKEN` environment variable. A token authenticates without
   a password (the token-equivalent of GEE's service key); generate one
   from your EDL profile under **Generate Token**. Best for CI and
   headless use — no password in plaintext.
2. **Username / password** — `EarthData(..., username=..., password=...)`
   or the `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment
   variables.
3. **`~/.netrc`** — add a machine entry:

   ```text
   machine urs.earthdata.nasa.gov
       login YOUR_USERNAME
       password YOUR_PASSWORD
   ```

   Pass a non-default path with `EarthData(..., netrc_path=...)`.
4. **Interactive prompt** — the last resort when none of the above
   resolves; `earthaccess` prompts and persists a token.

An explicit `token=` / `username=` / `password=` is exported to the
matching environment variable (`EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME`
/ `EARTHDATA_PASSWORD`) that `earthaccess`'s environment strategy reads.
After a successful login, `earthaccess` persists the token so later
processes reuse it.

## In-region S3 access

For cloud-hosted collections, the authenticated session also mints
**rotating, per-provider S3 credentials** (valid roughly one hour). The
backend uses them automatically when streaming in-region — see
[Usage → cloud streaming](usage.md#cloud-streaming-vs-https-download).

## CI secrets

The live e2e suite (`-m "e2e and earthdata"`, run by the `e2e-earthdata`
job under Python ≥ 3.12) reads either an `EARTHDATA_TOKEN` **or** an
`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` repository secret, and skips
cleanly when none are set. Configure with the GitHub CLI — a single
token is the simplest:

```bash
gh secret set EARTHDATA_TOKEN --repo <owner>/<repo>      # paste your EDL bearer token
# …or the username/password pair instead:
gh secret set EARTHDATA_USERNAME --repo <owner>/<repo>   # paste your EDL username
gh secret set EARTHDATA_PASSWORD --repo <owner>/<repo>   # paste your EDL password
```

Until those are set, the live e2e tests (including the example-notebook
execution) skip rather than fail.
