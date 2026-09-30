"""The Google API client library as a *dependency*: optional, injectable, bound in one place.

Three promises, each tested by breaking it:

- nothing in the library reaches `googleapiclient` (or `google.auth`, or the OAuth flow) at
  import time, so a journey that talks to nobody runs where the package is not installed;
- exactly one module binds `HttpError`, because two fallbacks would bind two classes and an
  `except HttpError` somewhere would silently stop matching;
- a caller's own client builder is told whether credentials were passed, and can ask for the
  library's instead of quietly building an unauthenticated client.
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

import pytest

from beamer2slides import gapi, google_auth, interpreter

from .fake_google import NoDocs, NoDrive, NoSlides

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from typing_extensions import Unpack

    from beamer2slides.google_types import DocsService, DriveService, ExecuteOptions, SlidesService

#: The package as it is laid out *where this test runs*, and deliberately not `.resolve()`d:
#: these two tests are the only ones that walk the library's own source files, and under a
#: runfiles tree `__file__` is a symlink into a content store that holds files by hash rather
#: than as a package - follow it and `rglob("*.py")` has the wrong tree, or none, to walk. The
#: parent of the link is the directory where the modules really stand beside each other, which
#: is what both tests are asking about.
SRC = Path(google_auth.__file__).parent
#: What the library must not need until it really calls Google.
GOOGLE = ("googleapiclient", "google_auth_oauthlib", "google.auth", "google.oauth2", "google")


# ---------------------------------------------------------------- the dependency

def _library_sources() -> Iterator[Path]:
    """The library's own modules. Where a build drops the `src/` level, `tests/` stands among
    them, and a test may import the client library at the top: the rule is the library's."""
    return (p for p in sorted(SRC.rglob("*.py")) if "tests" not in p.relative_to(SRC).parts)


def _module_level_imports(path: Path) -> Iterator[str]:
    """Every `import x` / `from x import y` at column 0, as dotted names."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))   # one source in the tree has a BOM
    for node in ast.walk(tree):
        if getattr(node, "col_offset", 0) != 0:
            continue                       # inside a function or a try block: imported on use
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_no_module_but_gapi_reaches_google_at_import_time() -> None:
    """`deck_prepare` "needs no account at all" and pulled in the Google client library through
    snapshot -> emit -> gslides, so it could not be *imported* in a sandbox that has neither an
    account nor the package. The import is what the caller hit; this is the rule that keeps it."""
    offenders: dict[str, list[str]] = {}
    for path in _library_sources():
        if path.name == "gapi.py":
            continue
        named = [m for m in _module_level_imports(path)
                 if m in GOOGLE or m.startswith(tuple(g + "." for g in GOOGLE))]
        if named:
            offenders[path.relative_to(SRC).as_posix()] = named
    assert offenders == {}


def test_only_gapi_binds_the_error_class() -> None:
    """One binding, anywhere in the tree: two would be two different classes, and an `except`
    clause naming the other one would match nothing without ever saying so."""
    binders = [path.relative_to(SRC).as_posix() for path in _library_sources()
               if "from googleapiclient.errors import" in path.read_text(encoding="utf-8-sig")]
    assert binders == ["gapi.py"]


_SANDBOX = """
import sys

class Blocked:
    def find_module(self, name, path=None):
        return self if name.split(".")[0] in ("googleapiclient", "google", "google_auth_oauthlib") else None
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("googleapiclient", "google", "google_auth_oauthlib"):
            raise ImportError(f"{name} is not installed")
        return None

sys.meta_path.insert(0, Blocked())
for name in [n for n in sys.modules if n.split(".")[0] in ("googleapiclient", "google")]:
    del sys.modules[name]

from beamer2slides import doc_sync, emit, gapi, google_auth, gslides, guard, snapshot, sync
from beamer2slides.agent.tools import TOOLS

assert not gapi.installed()
assert gapi.HttpError.__module__ == "beamer2slides.gapi"
assert emit.HttpError is gapi.HttpError is sync.HttpError is doc_sync.HttpError
assert guard.HttpError is gapi.HttpError is snapshot.HttpError is gslides.HttpError
assert "deck_prepare" in TOOLS and len(TOOLS) == 13

try:                                  # what a caller who has not injected anything is told
    google_auth.slides_service()
except ModuleNotFoundError as exc:
    assert "beamer2slides[google]" in str(exc) and "use_services" in str(exc)
else:
    raise AssertionError("a client was built with no client library installed")

with google_auth.use_services({"slides": "SLIDES"}):   # ... and what an injecting one gets
    assert google_auth.slides_service() == "SLIDES"

print("ok")
"""


def test_the_library_works_with_no_google_client_installed(tmp_path: Path) -> None:
    """The whole point, measured the only way it can be: in a process where the import fails.

    A subprocess, not `sys.modules` surgery, because re-importing half the package in this one
    would leave every later test with the crippled copy."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(SRC.parent)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    done = subprocess.run([interpreter.python(), "-c", _SANDBOX], capture_output=True, text=True,
                          cwd=tmp_path, env=interpreter.env_over(env))
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().endswith("ok")


# ---------------------------------------------------------------- reading an error

class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status


class _Err(Exception):
    def __init__(self, status: int, *, content: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.resp, self.content = _Resp(status), content


def test_an_error_is_read_through_functions_rather_than_attributes() -> None:
    err = _Err(429, content=b'{"error": {"message": "Quota exceeded for quota metric"}}')
    assert gapi.status_of(err) == 429
    assert gapi.is_transient(err)
    assert gapi.message_of(err) == "Quota exceeded for quota metric"
    assert not gapi.is_transient(_Err(400, content=b""))
    # Nothing about the shape is assumed: an error carrying neither says so rather than raising.
    assert gapi.status_of(ValueError("no")) is None
    assert gapi.message_of(ValueError("no")) == "no"


def test_a_slow_export_waits_on_a_connection_of_its_own() -> None:
    """Drive's .pptx export embeds the deck's fonts and took 115 s over Noto Sans SC: past the
    library's 60 s, every retry timed out and no backup could be made. The export gets a patient
    connection for itself; an injected client's request (no library connection) runs as it is."""
    pytest.importorskip("google_auth_httplib2")
    from beamer2slides import gslides

    class Creds:
        def before_request(self, *a: object) -> None:
            pass

    class Http:
        """The library's own connection on a request: what carries the credentials."""

        def __init__(self) -> None:
            self.credentials = Creds()

    class Request:
        methodId = "drive.files.export"

        def __init__(self) -> None:
            self.http = Http()
            self.used: object = None

        def execute(self, **options: Unpack[ExecuteOptions]) -> bytes:
            self.used = options.get("http")
            return b"pptx"

    r = Request()
    assert gslides.execute_with(r, retries=gslides.RETRIES, timeout=300) == b"pptx"
    used = r.used
    assert getattr(getattr(used, "http", None), "timeout", None) == 300
    assert isinstance(getattr(used, "credentials", None), Creds)

    class Injected:
        def execute(self, **options: Unpack[ExecuteOptions]) -> bytes:
            return b"theirs"
    assert gapi.patient_http(Injected(), 300) is None
    assert gslides.execute_with(Injected(), retries=gslides.RETRIES, timeout=300) == b"theirs"


# ---------------------------------------------------------------- the builder's credentials

def _creds() -> Credentials:
    """Credentials nobody can use: a sentinel the tests compare by identity."""
    from google.oauth2.credentials import Credentials as UserCredentials
    return UserCredentials(token=None)


class _Client(NoSlides, NoDrive, NoDocs):
    """A ready client of every api that no test calls: what is compared is its identity."""


#: What a builder was asked: the api, and the credentials it was handed.
Seen = list[tuple[str, "Credentials | None"]]


class _Builder:
    """A caller's own builder: records each call in `seen` and answers `made` for every api."""

    def __init__(self, *, seen: Seen, made: _Client) -> None:
        self.seen = seen
        self.made = made

    @overload
    def __call__(self, api: Literal["slides"], version: str, creds: Credentials | None) -> SlidesService | None: ...
    @overload
    def __call__(self, api: Literal["drive"], version: str, creds: Credentials | None) -> DriveService | None: ...
    @overload
    def __call__(self, api: Literal["docs"], version: str, creds: Credentials | None) -> DocsService | None: ...

    def __call__(self, api: str, version: str,
                 creds: Credentials | None) -> SlidesService | DriveService | DocsService | None:
        self.seen.append((api, creds))
        return self.made


def test_a_builder_is_handed_no_credentials_unless_it_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented default, and the edge a caller reported: `emit()` opens with
    `slides_service(), drive_service()` and passes nothing, so a builder that trusts the argument
    builds an unauthenticated client while the library's own fallback would have fetched a token."""
    seen: Seen = []
    token = _creds()
    monkeypatch.setattr(google_auth, "credentials", lambda: token)
    with google_auth.use_services(_Builder(seen=seen, made=_Client())):
        google_auth.slides_service()
    assert seen == [("slides", None)]


def test_a_builder_that_asks_for_credentials_is_given_the_librarys(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Seen = []
    asked: list[int] = []
    token, own = _creds(), _creds()

    def credentials() -> Credentials:
        asked.append(1)
        return token

    monkeypatch.setattr(google_auth, "credentials", credentials)
    with google_auth.use_services(_Builder(seen=seen, made=_Client()), needs_credentials=True):
        google_auth.slides_service()
        google_auth.drive_service(own)               # a caller's own still wins
        assert google_auth.credentials_for_threads() is token
        # Asked twice to find out whether it caches, but credentials are resolved once for both.
        before = len(asked)
        assert google_auth.shared_service("slides", "v1")
        assert len(asked) == before + 1
    assert seen[0][0] == "slides" and seen[0][1] is token
    assert seen[1][0] == "drive" and seen[1][1] is own


def test_an_injected_client_is_still_no_reason_to_look_for_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The promise `needs_credentials` must not weaken: a caller handing over ready clients, or a
    builder that carries its own credentials, never makes the library go looking for a token."""
    monkeypatch.setattr(google_auth, "credentials",
                        lambda: pytest.fail("a token was looked for"))
    ready = _Client()
    with google_auth.use_services({"slides": ready}):
        assert google_auth.slides_service() is ready
        assert google_auth.credentials_for_threads() is None
    made = _Client()
    with google_auth.use_services(_Builder(seen=[], made=made)):
        assert google_auth.slides_service() is made
        assert google_auth.credentials_for_threads() is None
