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

Earthdata Login (EDL) is free and takes a couple of minutes. One account
gives you **both** a username/password **and** an optional bearer
**token** (generated from your profile) — either authenticates this
backend, with one important exception: **ASF needs username/password**
(see the warning below). The token is not a separate API key; it is an
alternative credential minted from the same account.

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
  their application once, or downloads return **HTTP 401** even with a
  valid token. **ASF (Sentinel-1 / OPERA) is the common case.** To
  authorize it:
    1. Log in at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov).
    2. Top-right menu → **Applications → Authorized Apps**.
    3. Click **Approve More Applications**, find **“Alaska Satellite
       Facility Data Access”**, and authorize it. If it is not listed,
       go to **EULAs → Accept New EULAs** and accept the ASF one (ASF
       gates its datapool behind a EULA).
    4. Alternatively, open any ASF granule once via
       [ASF Vertex](https://search.asf.alaska.edu) while logged in and
       approve when prompted.
  The same flow applies to any DAAC whose download 401s — approve its
  application from **Authorized Apps**.

  !!! warning "ASF needs username/password, not a bearer token"
      Authorizing the ASF app is necessary but **not sufficient** for a
      token. ASF's datapool redirects through an EDL OAuth flow
      (`datapool.asf.alaska.edu` → `cumulus.asf.alaska.edu` →
      `urs.earthdata.nasa.gov`), and a bearer token is dropped across
      that cross-host redirect → **HTTP 401**. For ASF (Sentinel-1 /
      OPERA) downloads, authenticate with **username/password** (or a
      `~/.netrc` entry) so `earthaccess` can hold the OAuth session, or
      stream in-region from S3. A bare `EARTHDATA_TOKEN` works for the
      other DAACs (GES DISC, ORNL, OB.DAAC, NSIDC, …) but not ASF HTTPS.
- **Accept a EULA.** A few collections (e.g. GPM IMERG at GES DISC) ask
  you to accept a licence agreement on first download.

## Credential sources (in priority order)

`EarthData(...)` resolves a login strategy automatically, in this
priority order:

1. **EDL bearer token** — an explicit `EarthData(..., token=...)` or the
   `EARTHDATA_TOKEN` environment variable. A token authenticates without
   a password (the token-equivalent of GEE's service key); generate one
   from your EDL profile under **Generate Token**. Best for CI and
   headless use — no password in plaintext. **Works for every DAAC
   except ASF** — ASF's OAuth-redirecting datapool drops the bearer
   token (see the ASF warning above), so Sentinel-1 / OPERA needs
   username/password.
2. **Username / password** — `EarthData(..., username=..., password=...)`
   or the `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` environment
   variables. Works for **all** DAACs, including ASF. Required for
   ASF / OPERA.
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
# …and/or the username/password pair (required for the ASF / OPERA test):
gh secret set EARTHDATA_USERNAME --repo <owner>/<repo>   # paste your EDL username
gh secret set EARTHDATA_PASSWORD --repo <owner>/<repo>   # paste your EDL password
```

Which secret to set depends on how much you want CI to confirm:

| Secret(s) | e2e tests that run | Notes |
|-----------|--------------------|-------|
| `EARTHDATA_TOKEN` only | the token notebooks (IMERG, GEDI, PACE, SMAP) + the IMERG fetch | The OPERA / ASF test skips. Lower risk — a token is scoped and expires (~60 days). |
| `EARTHDATA_TOKEN` + `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` | **all**, including the OPERA / ASF test | ASF needs the username/password path. Note this puts your **real EDL password** in the Actions secret store. |

Until the relevant secret(s) are set, the live e2e tests (including the
example-notebook execution) **skip** rather than fail. The
`TestEarthdataAsfNotebook` (OPERA) test is gated on
`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` specifically.
