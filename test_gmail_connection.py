"""
Isolated Gmail connectivity test for the new (prod) OAuth client.

Verifies, without sending or modifying anything:
  1. OAuth works with the new prod credentials (refresh or one-time browser login)
  2. The authenticated account is the one you expect
  3. The 'Finanzas Personales' label exists
  4. Unread messages in that label can be listed

Run:  python test_gmail_connection.py
"""

import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# gmail.modify is the scope production uses (read + mark-as-read).
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
LABEL_NAME = "Finanzas Personales"
AUTH_PORT = 8080

_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_DIR, "token.json")
CREDENTIALS_FILE = os.path.join(
    _DIR,
    "client_secret_925122271245-564ppadkhjetdhjphgjo2g55htnmimqe"
    ".apps.googleusercontent.com.json",
)


def get_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(f"[FAIL] Credentials file not found:\n  {CREDENTIALS_FILE}")

    creds = None
    if os.path.exists(TOKEN_FILE):
        print(f"[ok]   Found token file: {os.path.basename(TOKEN_FILE)}")
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    else:
        print("[info] No token.json yet — will open a browser for one-time login.")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[info] Token expired — refreshing with the saved refresh token...")
            creds.refresh(Request())
            print("[ok]   Refresh succeeded (refresh token is still valid).")
        else:
            print("[info] Launching browser OAuth flow...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=AUTH_PORT)
            print("[ok]   Browser login succeeded.")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"[ok]   Saved token to {os.path.basename(TOKEN_FILE)}")
    else:
        print("[ok]   Existing token is valid — no refresh needed.")

    return creds


def main():
    print("=== Gmail connectivity test (prod client) ===\n")
    print(f"Credentials: {os.path.basename(CREDENTIALS_FILE)}\n")

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    # 1) Who are we authenticated as?
    profile = service.users().getProfile(userId="me").execute()
    print(f"\n[ok]   Authenticated as: {profile.get('emailAddress')}")
    print(f"       Total messages in mailbox: {profile.get('messagesTotal')}")

    # 2) Does the expected label exist?
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next(
        (l["id"] for l in labels if l["name"].lower() == LABEL_NAME.lower()),
        None,
    )
    if not label_id:
        print(f"\n[WARN] Label '{LABEL_NAME}' not found. Available labels:")
        for l in sorted(l["name"] for l in labels):
            print(f"         - {l}")
        sys.exit(1)
    print(f"[ok]   Label '{LABEL_NAME}' found (id={label_id})")

    # 3) List unread messages in that label (read-only, no modification).
    result = service.users().messages().list(
        userId="me",
        labelIds=[label_id, "UNREAD"],
        maxResults=5,
    ).execute()
    msgs = result.get("messages", [])
    print(f"[ok]   Unread messages in '{LABEL_NAME}': {len(msgs)}"
          f"{' (showing up to 5)' if msgs else ''}")

    for m in msgs:
        meta = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in meta["payload"]["headers"]}
        print(f"         • {headers.get('Subject', '(no subject)')}"
              f"  —  {headers.get('From', '?')}")

    print("\n=== All checks passed. Prod Gmail access is working. ===")


if __name__ == "__main__":
    main()