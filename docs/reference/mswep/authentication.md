# MSWEP / MSWX — getting access and configuring credentials

**Read this page first.** Unlike most earthlens backends, `mswep` cannot work out of the box: GloH2O publishes
MSWEP and MSWX with **no anonymous download**, so earthlens automates *your own* approved download. Two
prerequisites: approved access (a one-off request form), and a Google credential — which, because GloH2O
link-shares, can be as simple as your existing `gcloud` login.

## 1. Request access (one form per product)

Non-commercial access is granted by a human reviewing a request form. Submit one for each product you want:

- **MSWEP** (precipitation) — <https://www.gloh2o.org/mswep/>
- **MSWX** (meteorological forcing) — <https://www.gloh2o.org/mswx/>

Each asks for your name, organisation, intended use, and an academic or work email. On approval you receive an
email containing a **Google-Drive share link** and `rclone` download instructions. The two products are shared
separately, so approval for one does not grant the other.

Note the licence before you start: **CC BY-NC 4.0**. Academic, nonprofit, personal and non-revenue
government/NGO use is free; any for-profit, consulting, contract-research, paywalled or product-integration use
needs a separate licence from GloH2O. Every `download()` emits a `LicenseWarning` saying so.

## 2. Configure a credential

GloH2O **link-shares** its folders ("anyone with the link"), so **any** Google credential that knows the folder
id can read them — including a service account. (This was verified against the real shares; the folders show
`shared: True` but do not appear in any account's "Shared with me", which is the signature of link-sharing.)
That makes setup simpler than a per-user share would: pick whichever of the four options below you already have.

### Option A — Application Default Credentials (simplest if you have gcloud)

If the machine is already authenticated to Google Cloud, earthlens uses those credentials with no further
configuration:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/cloud-platform
export MSWEP_DRIVE_FOLDER=<folder-id>    # from the approval email's link
```

A `GOOGLE_APPLICATION_CREDENTIALS` service-account key is picked up the same way.

### Option B — a service-account key

Create a service account (or reuse one), download its JSON key, and point earthlens at it. No browser consent,
no token expiry:

```bash
export MSWEP_TOKEN_FILE=/path/to/service-account.json
export MSWEP_DRIVE_FOLDER=<folder-id>
```

### Option C — reuse your `rclone` remote

GloH2O's approval email describes an `rclone` download, so the OAuth token often already exists:

```bash
rclone config          # new remote -> drive -> scope drive.readonly -> browser consent
export MSWEP_RCLONE_REMOTE=GoogleDrive
export MSWEP_DRIVE_FOLDER=<folder-id>
```

`rclone.conf` is found automatically (`$RCLONE_CONFIG`, `%APPDATA%\rclone` on Windows, `$XDG_CONFIG_HOME` or
`~/.config` on POSIX, and legacy `~/.rclone.conf`); set `MSWEP_RCLONE_CONFIG` to override. The remote must carry
its own `client_id` / `client_secret` (rclone's built-in one cannot be refreshed outside rclone), or earthlens
refuses it.

### Option D — an authorized-user `token.json`

Mint one once with `tools/mswep/mint_token.py` (a Desktop-app OAuth client) and point earthlens at the file:

```bash
pip install google-auth-oauthlib
python tools/mswep/mint_token.py client_secret.json -o token.json
export MSWEP_TOKEN_FILE=/path/to/token.json
export MSWEP_DRIVE_FOLDER=<folder-id>
```

!!! warning "A Testing consent screen expires the token weekly"
    Google expires refresh tokens after **7 days** for OAuth apps in `Testing` publishing status. Set the
    consent screen to **In production** for a token that persists.

### Finding the folder id

The approval email links something like
`https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz`. The trailing segment is the folder id, and
it **is** the version root — GloH2O shares one folder per product and per version (e.g. a separate id for MSWEP
v2.8 and v3.16), so pass the id for the version you want.

## 3. Verify

```python
from earthlens.core import EarthLens

lens = EarthLens(
    "mswep",
    start="2020-04-25",
    end="2020-04-25",
    variables=["precipitation"],
    temporal_resolution="daily",
    path="out",
)
print(lens.download())   # -> [PosixPath('out/MSWEP_V316_test/Past/Daily/2020116.nc')]
```

Or run the gated live tests, which skip cleanly when nothing is configured:

```bash
MSWEP_DRIVE_FOLDER=<folder-id> pytest -m "e2e and mswep"
```

## Configuration reference

| Setting | Environment variable | Purpose |
|---|---|---|
| `folder_id=` | `MSWEP_DRIVE_FOLDER` | Drive id of the shared version-root folder (required) |
| `credentials=MswepCredentials(token_path=…)` | `MSWEP_TOKEN_FILE` | A service-account key **or** an authorized-user `token.json` |
| `credentials=MswepCredentials(rclone_remote=…)` | `MSWEP_RCLONE_REMOTE` | Name of your Drive remote |
| `credentials=MswepCredentials(rclone_config=…)` | `MSWEP_RCLONE_CONFIG` / `RCLONE_CONFIG` | Path to `rclone.conf` |

Resolution order: an explicit credential file, then an `rclone` remote, then the same from the environment, then
**Application Default Credentials** as the final fallback.

## Troubleshooting

| Symptom | Cause |
|---|---|
| *"has no `client_id` / `client_secret`"* | Your `rclone` remote uses rclone's built-in OAuth client, which it never writes to the config, so the token cannot be refreshed outside rclone. Create a personal client and re-run `rclone config`. |
| *"is a service-account key … not an OAuth client ID"* (from `mint_token.py`) | Minting a token needs an OAuth client, not a key. To *read* the share a service-account key is fine — set `MSWEP_TOKEN_FILE` to it directly, no minting. |
| Empty result, only "absent from the share" warnings | The window may predate the variant, or you named a `variant=` that does not cover it — or you passed the folder id for a different version than `version=`. |
| *"per-file download quota is exhausted"* | Drive caps downloads per file across everyone holding the share. Retrying does not help; it clears after ~24 h. Use `rclone sync` for bulk. |
