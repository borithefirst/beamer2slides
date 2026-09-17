"""OAuth for the Slides and Drive APIs, as an installed desktop app.

The client secret and the cached token are looked for in `$B2S_CLIENT_SECRET` /
`$B2S_TOKEN`, then in a source checkout's root (where they are ignored by git),
then in the user's config folder (`%APPDATA%\\beamer2slides`, `~/.config/beamer2slides`),
which is where a pip-installed beamer2slides keeps them. Both are readable only
by the user who owns them.
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

from .paths import CHECKOUT, in_checkout

ROOT = CHECKOUT  # where a checkout keeps its credentials


def config_dir() -> Path:
    """Where an installed beamer2slides keeps the OAuth client and the token."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "beamer2slides"


def credential_file(env: str, name: str, default: Path | None = None) -> Path:
    """The override, else whichever of the two homes has the file, else `default`."""
    if os.environ.get(env):
        return Path(os.environ[env])
    for folder in (ROOT, config_dir()):
        if (folder / name).exists():
            return folder / name
    return default or config_dir() / name


# Missing everywhere: ask for it where this install would keep it.
CLIENT_SECRET = credential_file("B2S_CLIENT_SECRET", "client_secret.json",
                                ROOT / "client_secret.json" if in_checkout() else None)
# A token written for the first time goes beside the client secret in use, not in the other home.
TOKEN = credential_file("B2S_TOKEN", "token.json", CLIENT_SECRET.with_name("token.json"))

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
            raise FileNotFoundError(
                f"OAuth client secret not found: {CLIENT_SECRET}. Create a desktop-app OAuth client "
                f"in a Google Cloud project with the Slides and Drive APIs enabled, download its JSON "
                f"and save it there (or point $B2S_CLIENT_SECRET at it). See docs/install.md.")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    restrict_to_current_user(TOKEN)
    return creds


def slides_service(creds: Credentials | None = None):
    return build("slides", "v1", credentials=creds or credentials(), cache_discovery=False)


def drive_service(creds: Credentials | None = None):
    """Service objects are not thread-safe: build one per thread, sharing `creds`."""
    return build("drive", "v3", credentials=creds or credentials(), cache_discovery=False)
