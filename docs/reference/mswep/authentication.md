# MSWEP / MSWX — getting access and configuring credentials

**Read this page first.** Unlike most earthlens backends, `mswep` cannot work out of the box: GloH2O publishes
MSWEP and MSWX with **no anonymous download**, so earthlens automates *your own* approved download rather than
fetching on your behalf. There are two prerequisites, and both are one-off human steps.

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

### A service account will not work

This is the single most common way to get stuck. A Google **service account** is a separate principal with its
own empty Drive; GloH2O shares the folder with **your personal Google account**, and a service account has no
view of anyone else's "Shared with me". Pointed at the share it does not raise — `files.list` simply returns
nothing, which would look like an empty date range.

`MswepAuth` therefore detects a service-account key at load time and refuses it with an explanation rather than
letting the request degrade. You need a **user** credential: a refresh token for the account the share was
granted to.

### Option A — reuse your `rclone` remote (recommended)

GloH2O's approval email already tells you to download via `rclone`, so this route needs no second consent flow.

```bash
# One-off: create a personal OAuth client (rclone's built-in one cannot be
# refreshed outside rclone, so earthlens will refuse a remote without one).
#   1. Google Cloud Console -> APIs & Services -> enable the Google Drive API
#   2. OAuth consent screen -> External -> add yourself as a Test user
#   3. Credentials -> Create credentials -> OAuth client ID -> Desktop app
rclone config          # new remote -> drive -> paste client_id/secret
                       # -> scope: drive.readonly -> browser consent
rclone lsd --drive-shared-with-me GoogleDrive:   # confirms the share is visible
```

Then point earthlens at that remote:

```bash
export MSWEP_RCLONE_REMOTE=GoogleDrive          # the remote name you chose
export MSWEP_DRIVE_FOLDER=<folder-id>           # from the approval email's link
```

`rclone.conf` is found automatically (`$RCLONE_CONFIG`, `%APPDATA%\rclone` on Windows, `$XDG_CONFIG_HOME` or
`~/.config` on POSIX, and legacy `~/.rclone.conf`); set `MSWEP_RCLONE_CONFIG` to override.

!!! warning "Leave the consent screen in *Testing* and your token expires weekly"
    Google expires refresh tokens after **7 days** for apps in `Testing` publishing status. Set the OAuth
    consent screen to **In production** for a token that persists. Drive is a sensitive scope, so Google offers
    verification — for personal use you can publish unverified and click through the warning.

### Option B — an authorized-user `token.json`

If you would rather not use `rclone`, mint a token once with `google-auth-oauthlib`'s `InstalledAppFlow` and
point earthlens at the file:

```bash
export MSWEP_TOKEN_FILE=/path/to/token.json
export MSWEP_DRIVE_FOLDER=<folder-id>
```

The file must be an **authorized-user** JSON carrying `client_id`, `client_secret` and `refresh_token`. A token
without a refresh token is refused — it would expire within the hour with no way to renew.

### Finding the folder id

The approval email links something like
`https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz`. The trailing segment is the folder id.

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
print(lens.download())   # -> [PosixPath('out/2020116.nc')]
```

Or run the gated live tests, which skip cleanly when nothing is configured:

```bash
pytest -m "e2e and mswep"
```

## Configuration reference

| Setting | Environment variable | Purpose |
|---|---|---|
| `folder_id=` | `MSWEP_DRIVE_FOLDER` | Drive id of the shared folder (required) |
| `credentials=MswepCredentials(token_path=…)` | `MSWEP_TOKEN_FILE` | Authorized-user `token.json` |
| `credentials=MswepCredentials(rclone_remote=…)` | `MSWEP_RCLONE_REMOTE` | Name of your Drive remote |
| `credentials=MswepCredentials(rclone_config=…)` | `MSWEP_RCLONE_CONFIG` / `RCLONE_CONFIG` | Path to `rclone.conf` |

Resolution order is: an explicit `token_path`, then an `rclone` remote, then the same two from the environment.

## Troubleshooting

| Symptom | Cause |
|---|---|
| *"is a Google **service-account** key"* | See above — use a user credential. |
| *"has no `client_id` / `client_secret`"* | Your `rclone` remote uses rclone's built-in OAuth client, which it never writes to the config, so the token cannot be refreshed outside rclone. Create a personal client and re-run `rclone config`. |
| *"root folder … is not in the shared folder"* | GloH2O stamps the version into the folder name and renames it between releases. The error lists the roots actually present — pick a matching `version=`. |
| *"marked provisional in the MSWEP catalog"* | The value could not be verified without a live share, so earthlens refuses to guess. Confirm the real name inside your share and drop the flag from `mswep_data_catalog.yaml`. |
| *"per-file download quota is exhausted"* | Drive caps downloads per file across everyone holding the share. Retrying does not help; it clears after ~24 h. Use `rclone sync` for bulk. |
| Empty result, only "absent from the share" warnings | The window may predate the variant, or you named a `variant=` that does not cover it. |
