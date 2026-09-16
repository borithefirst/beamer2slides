"""OAuth for the Slides and Drive APIs, as an installed desktop app.

The client secret and the cached token live in the project root, are ignored by
git, and are readable only by the current Windows user.
"""

import getpass
import os
import subprocess
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[2]
CLIENT_SECRET = Path(os.environ.get("B2S_CLIENT_SECRET", ROOT / "client_secret.json"))
TOKEN = Path(os.environ.get("B2S_TOKEN", ROOT / "token.json"))

SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    # Only files this app creates or opens, not the user's whole Drive.
    "https://www.googleapis.com/auth/drive.file",
]


def restrict_to_current_user(path: Path) -> None:
    """Remove inherited ACLs so only the current user can read the file."""
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            check=True, capture_output=True,
        )
    else:
        path.chmod(0o600)


def credentials() -> Credentials:
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Testing-mode apps get refresh tokens that expire after 7 days.
            creds = None
    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            raise FileNotFoundError(f"OAuth client secret not found: {CLIENT_SECRET}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    restrict_to_current_user(TOKEN)
    return creds


def slides_service():
    return build("slides", "v1", credentials=credentials(), cache_discovery=False)


def drive_service():
    return build("drive", "v3", credentials=credentials(), cache_discovery=False)
