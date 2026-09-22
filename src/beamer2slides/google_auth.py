"""OAuth for the Slides and Drive APIs, as an installed desktop app.

The client secret and the cached token are looked for in `$B2S_CLIENT_SECRET` /
`$B2S_TOKEN`, then in a source checkout's root (where they are ignored by git),
then in the user's config folder (`%APPDATA%\\beamer2slides`, `~/.config/beamer2slides`),
which is where a pip-installed beamer2slides keeps them. Both are readable only
by the user who owns them.

`credentials()` may open a browser, which is the right thing at a terminal and the wrong
thing everywhere else: an agent harness, a server, a CI job. `use_provider` lets a caller
put its own supply in front of that flow for the duration of a block, so nothing in the
library has to learn where credentials come from (`agent/auth.py` is the caller that does).
`use_services` is the same hook one step later: a caller that already holds a Slides, Drive
or Docs client - with its own discovery cache, its own retries - puts it in front of
`build(...)`, which otherwise fetches a discovery document per call.

Both are **per context**, not per process: a server answering two requests at once has two
visitors' tokens in the air, and neither may reach the other's deck.
"""

import getpass
import os
import subprocess
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock

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
    # No "documents" scope: that one reaches every Doc the user has, while
    # drive.file already covers the ones this app created (docs/google-docs.md).
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


class _Hook:
    """Something a caller puts in front of a default, for the length of a block.

    The value lives in a `ContextVar`, so it belongs to the thread or the async task that
    installed it: two requests handled at once in one process each see their own, which is what
    a server needs and what a module-level global cannot give.

    A `ContextVar` has one edge, and it is the reason for everything below it: a thread started
    inside the block runs in a *fresh* context and inherits nothing. The library's own worker
    pools do not care - every one of them resolves `credentials()` on the calling thread and
    hands the answer down (`emit.measure_places`, `deck_ir.slide_thumbnails`,
    `snapshot.sign_pictures`), which is also the rule for a thread anyone else starts, since a
    service object is not thread-safe either. But a thread that asks anyway must not fall
    through to `InstalledAppFlow` and open a browser on a server, so it is given the one
    installed value while there is exactly one, and told what to do when there are two. The
    fallback is deliberately the narrowest thing that cannot be wrong: with a single block
    active there is only one possible answer, and with several there is no guessing at all.
    """

    def __init__(self, name: str, what: str) -> None:
        self._var: ContextVar = ContextVar(name, default=None)
        self._what = what
        self._active: list = []          # every block currently open, in this whole process
        self._lock = Lock()

    @contextmanager
    def use(self, value):
        token = self._var.set(value)
        with self._lock:
            self._active.append(value)
        try:
            yield
        finally:
            self._var.reset(token)
            with self._lock:
                for i in range(len(self._active) - 1, -1, -1):
                    if self._active[i] is value:
                        del self._active[i]
                        break

    def get(self):
        value = self._var.get()
        if value is not None:
            return value
        with self._lock:
            active = list(self._active)
        if not active or all(a is None for a in active):
            return None
        if all(a is active[0] for a in active):
            return active[0]
        raise RuntimeError(
            f"this thread inherited no {self._what} and {len(active)} different ones are "
            f"installed in this process, so there is no answer to give. Resolve it on the "
            f"thread that owns the block and pass the result down (as the library's own worker "
            f"pools do), or start the thread with `contextvars.copy_context().run(...)`.")


#: What supplies credentials instead of the browser flow, while a block asks for it.
_credentials_hook = _Hook("beamer2slides.credentials", "credentials provider")
#: What builds the API clients instead of `googleapiclient.discovery.build`.
_services_hook = _Hook("beamer2slides.services", "service builder")


def use_provider(provider):
    """Take credentials from `provider()` inside this block, never from the browser flow.

    Per context (see `_Hook`): a harness may run this around each of several requests at once,
    and each sees its own. `agent.context.tool` holds one per journey.
    """
    return _credentials_hook.use(provider)


def use_services(services):
    """Take the API clients from `services` inside this block, instead of building them.

    `services` is either a mapping of api name ("slides", "drive", "docs") to a ready client, or
    a callable `(api, version, creds) -> client | None` - a builder, which is the shape that can
    answer for all three and cache the discovery document the library's `build(...)` otherwise
    fetches per call. Anything not answered for is built as before, so a caller may hand over
    one api and leave the rest alone, and `creds` is `None` where nobody passed any: a client
    that carries its own credentials is never a reason to go looking for a token.

    A service object is not thread-safe (`drive_service`), so a caller who hands over one client
    is promising this block is one thread's; a builder is handed the api and may return a fresh
    client per call.
    """
    return _services_hook.use(services)


def shared_service(api: str, version: str = "v1", creds=None) -> bool:
    """True where every thread asking for an `api` client would be handed the *same* object.

    A service object is not thread-safe, so a pass that would run on several threads has to know
    whether it may (`emit.build_deck`). Nothing installed, or a mapping that does not answer for
    this api, means `build(...)` makes a fresh one per call; a mapping that does answer is one
    ready client a caller handed over, which `use_services` says is that caller's own; and a
    builder is asked twice, since one may cache its clients as readily as its discovery document.
    """
    made = _services_hook.get()
    if made is None:
        return False
    if isinstance(made, Mapping):
        return made.get(api) is not None
    first = made(api, version, creds)
    return first is not None and first is made(api, version, creds)


def credentials_for_threads():
    """The credentials worker threads should build their clients from, or None.

    Resolved on the calling thread, because a thread inherits no context (`_Hook`) - and only
    where they are wanted: where a caller's own `use_services` answers for the client, one that
    carries its own credentials is never a reason to go looking for a token.
    """
    return None if _services_hook.get() is not None else credentials()


def _service(api: str, version: str, creds):
    made = _services_hook.get()
    if made is not None:
        service = made.get(api) if isinstance(made, Mapping) else made(api, version, creds)
        if service is not None:
            return service
    return build(api, version, credentials=creds or credentials(), cache_discovery=False)


def credentials() -> Credentials:
    provider = _credentials_hook.get()
    if provider is not None:
        return provider()
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
    return _service("slides", "v1", creds)


def drive_service(creds: Credentials | None = None):
    """Service objects are not thread-safe: build one per thread, sharing `creds`."""
    return _service("drive", "v3", creds)


def docs_service(creds: Credentials | None = None):
    """The Docs API must be enabled in the Cloud project; see docs/google-docs.md."""
    return _service("docs", "v1", creds)
