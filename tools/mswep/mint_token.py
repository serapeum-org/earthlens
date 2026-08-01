"""Mint the `token.json` the MSWEP / MSWX backend reads.

`earthlens.mswep` only ever **consumes** a refresh token — that is why the
`[mswep]` extra needs `google-auth` but not `google-auth-oauthlib`. Minting one
is a one-off, interactive, browser-based step, so it lives here as an operator
script rather than in the shipped package.

You need this only if you are **not** using `rclone`. If you already configured
an rclone Drive remote (which GloH2O's approval email tells you to), point
earthlens at that instead and skip this entirely:

    set MSWEP_RCLONE_REMOTE=GoogleDrive
    set MSWEP_DRIVE_FOLDER=<folder-id>

Usage:

    pip install google-auth-oauthlib
    python tools/mswep/mint_token.py <client-secret.json> [-o token.json]

`<client-secret.json>` is what the Cloud console downloads for an **OAuth client
ID** of type *Desktop app* — not a service-account key. A service account cannot
read the GloH2O share at all: it is a separate principal whose "Shared with me"
is empty, so it returns nothing rather than failing. This script rejects one
rather than letting you find that out later.

The script opens a browser once, you approve read-only Drive access, and it
writes an authorized-user file containing the refresh token. Point earthlens at
it with `MSWEP_TOKEN_FILE`.

Note:
    Leave the OAuth consent screen in `Testing` and Google expires the refresh
    token after **7 days**. Set the app to `In production` for one that persists;
    for personal use you can publish unverified and click through the warning.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Read-only Drive scope — the backend only lists and downloads. Mirrors
#: `earthlens.mswep.auth.DRIVE_SCOPE`.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Argument list; defaults to `sys.argv[1:]`.

    Returns:
        argparse.Namespace: With `client_secrets` and `output` paths.
    """
    parser = argparse.ArgumentParser(
        description="Mint a Drive token.json for the earthlens mswep backend.",
    )
    parser.add_argument(
        "client_secrets",
        type=Path,
        help="OAuth client-ID JSON (Desktop app) downloaded from the Cloud console.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("token.json"),
        help="Where to write the authorized-user file (default: ./token.json).",
    )
    return parser.parse_args(argv)


def check_not_service_account(path: Path) -> None:
    """Reject a service-account key supplied in place of an OAuth client.

    Args:
        path: The JSON file the caller passed.

    Raises:
        SystemExit: When the file is a service-account key, or is not the
            `installed` / `web` shape an OAuth client ID has.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path} could not be read as JSON: {exc}") from exc

    if payload.get("type") == "service_account":
        raise SystemExit(
            f"{path} is a service-account key ({payload.get('client_email')}), not an "
            "OAuth client ID. A service account cannot read the GloH2O share: Drive "
            "authorises per principal, and a service account is a separate identity "
            "whose 'Shared with me' is empty, so listing the folder returns nothing "
            "rather than raising.\n\n"
            "Create the right credential instead: Cloud console -> APIs & Services -> "
            "Credentials -> Create credentials -> OAuth client ID -> Desktop app."
        )
    if not ({"installed", "web"} & set(payload)):
        raise SystemExit(
            f"{path} does not look like an OAuth client ID: expected a top-level "
            "'installed' (Desktop app) or 'web' key."
        )


def mint(client_secrets: Path, output: Path) -> Path:
    """Run the installed-app consent flow and write the authorized-user file.

    Args:
        client_secrets: The OAuth client-ID JSON.
        output: Where to write the token.

    Returns:
        Path: `output`.

    Raises:
        SystemExit: When `google-auth-oauthlib` is not installed.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "this script needs `google-auth-oauthlib`, which the earthlens "
            "[mswep] extra deliberately does not ship (the backend consumes a "
            "token, it never mints one). Install it just for this step:\n"
            "    pip install google-auth-oauthlib"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    # access_type=offline is what makes Google return a refresh token; without it
    # the credential expires within the hour with no way to renew, and
    # `MswepAuth` refuses it.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(credentials.to_json(), encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list; defaults to `sys.argv[1:]`.

    Returns:
        int: Process exit status.
    """
    args = parse_args(argv)
    if not args.client_secrets.exists():
        raise SystemExit(f"{args.client_secrets} does not exist.")

    check_not_service_account(args.client_secrets)
    written = mint(args.client_secrets, args.output)

    payload = json.loads(written.read_text(encoding="utf-8"))
    if not payload.get("refresh_token"):
        raise SystemExit(
            f"{written} carries no refresh token, so it cannot renew. Re-run with a "
            "consent screen set to 'In production', or revoke the app's prior grant "
            "at https://myaccount.google.com/permissions and try again."
        )

    print(f"wrote {written}")
    print("\nPoint earthlens at it:")
    print(f"    set MSWEP_TOKEN_FILE={written}")
    print("    set MSWEP_DRIVE_FOLDER=<folder-id from the GloH2O approval email>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
