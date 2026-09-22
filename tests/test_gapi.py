"""The Google API client library as a *dependency*: optional, injectable, bound in one place.

Three promises, each tested by breaking it:

- nothing in the library reaches `googleapiclient` (or `google.auth`, or the OAuth flow) at
  import time, so a journey that talks to nobody runs where the package is not installed;
- exactly one module binds `HttpError`, because two fallbacks would bind two classes and an
  `except HttpError` somewhere would silently stop matching;
- a caller's own client builder is told whether credentials were passed, and can ask for the
  library's instead of quietly building an unauthenticated client.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from beamer2slides import gapi, google_auth

SRC = Path(google_auth.__file__).resolve().parent
#: What the library must not need until it really calls Google.
GOOGLE = ("googleapiclient", "google_auth_oauthlib", "google.auth", "google.oauth2", "google")


# ---------------------------------------------------------------- the dependency

def _module_level_imports(path: Path):
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


def test_no_module_but_gapi_reaches_google_at_import_time():
    """`deck_prepare` "needs no account at all" and pulled in the Google client library through
    snapshot -> emit -> gslides, so it could not be *imported* in a sandbox that has neither an
    account nor the package. The import is what the caller hit; this is the rule that keeps it."""
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "gapi.py":
            continue
        named = [m for m in _module_level_imports(path)
                 if m in GOOGLE or m.startswith(tuple(g + "." for g in GOOGLE))]
        if named:
            offenders[path.relative_to(SRC).as_posix()] = named
    assert offenders == {}


def test_only_gapi_binds_the_error_class():
    """One binding, anywhere in the tree: two would be two different classes, and an `except`
    clause naming the other one would match nothing without ever saying so."""
    binders = [path.relative_to(SRC).as_posix() for path in sorted(SRC.rglob("*.py"))
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


def test_the_library_works_with_no_google_client_installed(tmp_path):
    """The whole point, measured the only way it can be: in a process where the import fails.

    A subprocess, not `sys.modules` surgery, because re-importing half the package in this one
    would leave every later test with the crippled copy."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(SRC.parent)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    done = subprocess.run([sys.executable, "-c", _SANDBOX], capture_output=True, text=True,
                          cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().endswith("ok")


# ---------------------------------------------------------------- reading an error

class _Resp:
    def __init__(self, status):
        self.status = status


class _Err(Exception):
    def __init__(self, status, content=b""):
        super().__init__(f"HTTP {status}")
        self.resp, self.content = _Resp(status), content


def test_an_error_is_read_through_functions_rather_than_attributes():
    err = _Err(429, b'{"error": {"message": "Quota exceeded for quota metric"}}')
    assert gapi.status_of(err) == 429
    assert gapi.is_transient(err)
    assert gapi.message_of(err) == "Quota exceeded for quota metric"
    assert not gapi.is_transient(_Err(400))
    # Nothing about the shape is assumed: an error carrying neither says so rather than raising.
    assert gapi.status_of(ValueError("no")) is None
    assert gapi.message_of(ValueError("no")) == "no"


# ---------------------------------------------------------------- the builder's credentials

def _builder(seen, made="CLIENT"):
    def build(api, version, creds):
        seen.append((api, creds))
        return made
    return build


def test_a_builder_is_handed_no_credentials_unless_it_asks(monkeypatch):
    """The documented default, and the edge a caller reported: `emit()` opens with
    `slides_service(), drive_service()` and passes nothing, so a builder that trusts the argument
    builds an unauthenticated client while the library's own fallback would have fetched a token."""
    seen = []
    monkeypatch.setattr(google_auth, "credentials", lambda: "TOKEN")
    with google_auth.use_services(_builder(seen)):
        google_auth.slides_service()
    assert seen == [("slides", None)]


def test_a_builder_that_asks_for_credentials_is_given_the_librarys(monkeypatch):
    seen, asked = [], []

    def credentials():
        asked.append(1)
        return "TOKEN"

    monkeypatch.setattr(google_auth, "credentials", credentials)
    with google_auth.use_services(_builder(seen), needs_credentials=True):
        google_auth.slides_service()
        google_auth.drive_service("OWN")             # a caller's own still wins
        assert google_auth.credentials_for_threads() == "TOKEN"
        # Asked twice to find out whether it caches, but credentials are resolved once for both.
        before = len(asked)
        assert google_auth.shared_service("slides", "v1")
        assert len(asked) == before + 1
    assert seen[0] == ("slides", "TOKEN") and seen[1] == ("drive", "OWN")


def test_an_injected_client_is_still_no_reason_to_look_for_a_token(monkeypatch):
    """The promise `needs_credentials` must not weaken: a caller handing over ready clients, or a
    builder that carries its own credentials, never makes the library go looking for a token."""
    monkeypatch.setattr(google_auth, "credentials",
                        lambda: pytest.fail("a token was looked for"))
    with google_auth.use_services({"slides": "S"}):
        assert google_auth.slides_service() == "S"
        assert google_auth.credentials_for_threads() is None
    with google_auth.use_services(_builder([])):
        assert google_auth.slides_service() == "CLIENT"
        assert google_auth.credentials_for_threads() is None
