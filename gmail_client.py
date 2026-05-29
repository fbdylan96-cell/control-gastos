import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# gmail.modify is required to mark messages as read
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_DIR, "token.json")
CREDENTIALS_FILE = os.path.join(
    _DIR,
    "client_secret_925122271245-564ppadkhjetdhjphgjo2g55htnmimqe"
    ".apps.googleusercontent.com.json",
)
AUTH_PORT = 8080
MAX_PER_RUN = 5


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=AUTH_PORT)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def build_service():
    return build("gmail", "v1", credentials=get_credentials())


def _get_label_id(service, label_name):
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    raise ValueError(f"Gmail label '{label_name}' not found")


def find_unread_label_messages(service, label_name, max_results=MAX_PER_RUN):
    """
    Return unread messages from a label, sorted by internalDate descending.
    Each item: {'id': str, 'internalDate': int}.
    """
    label_id = _get_label_id(service, label_name)
    result = service.users().messages().list(
        userId="me",
        labelIds=[label_id, "UNREAD"],
        maxResults=max_results + 3,
    ).execute()
    msgs = result.get("messages", [])
    if not msgs:
        return []

    candidates = []
    for m in msgs:
        meta = service.users().messages().get(
            userId="me",
            id=m["id"],
            format="metadata",
            metadataHeaders=["Date"],
        ).execute()
        candidates.append({
            "id": meta["id"],
            "internalDate": int(meta.get("internalDate") or 0),
        })

    candidates.sort(key=lambda x: x["internalDate"], reverse=True)
    return candidates[:max_results]


def get_message_full(service, msg_id):
    return service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()


def send_email(service, to_email: str, subject: str, html_body: str):
    """Send an HTML email via the Gmail API using the authenticated account."""
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["to"] = to_email
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def mark_as_read(service, msg_id):
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        print(f"[WARN] Could not mark {msg_id} as read: {e}")
