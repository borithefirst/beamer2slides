"""The agent layer's core: the workspace boundary, the journey wrapper, the credential seam.

These are the promises every tool is built on - that a path cannot climb out of the workspace,
that a forbidden or unauthenticated journey does no work at all, that nothing the library prints
or raises crosses the boundary as anything but data. Each one is tested by breaking it.
"""

from pathlib import Path
import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, NoReturn, TypeVar, get_args, overload

import pytest

from beamer2slides import google_auth
from beamer2slides.agent import (ALL_ACTIONS, LOCAL_ONLY, READS, READS_GOOGLE,
                                 WRITES_GOOGLE, AgentContext, GoogleAccess, Job, LocalWorkspace,
                                 NoGoogle, Refused, Result, TokenFile)
from beamer2slides.agent.context import Tool, tool
from beamer2slides.agent.types import CODES, Code, Need
from beamer2slides.json_types import Json, JsonObject

from .json_reads import text

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from beamer2slides.google_types import (Comments, DocsService, Documents, DriveService, Files,
                                            Permissions, Presentations, SlidesService)

_T = TypeVar("_T")


def _creds() -> "Credentials":
    """Credentials nobody can use: a sentinel the tests compare by identity."""
    from google.oauth2.credentials import Credentials as UserCredentials
    return UserCredentials(token=None)


class _Access:
    """A credential source that hands back a sentinel, or refuses the way a dead token does."""

    def __init__(self, creds: "Credentials", refusal: Refused | None) -> None:
        self.creds = creds
        self.refusal = refusal
        self.asked = 0

    def credentials(self) -> "Credentials":
        self.asked += 1
        if self.refusal:
            raise self.refusal
        return self.creds

    def describe(self) -> JsonObject:
        return {"available": not self.refusal, "source": "test"}


def _context(tmp_path: Path, google: GoogleAccess, allow: frozenset[Need]) -> AgentContext:
    return AgentContext(workspace=LocalWorkspace(tmp_path), google=google, allow=allow)


def _ctx(tmp_path: Path) -> AgentContext:
    """A context with an account and every permission."""
    return _context(tmp_path, _Access(_creds(), None), ALL_ACTIONS)


# -- the workspace boundary --------------------------------------------------------------


def test_a_path_cannot_climb_out_of_the_workspace(tmp_path: Path):
    ws = LocalWorkspace(tmp_path / "work")
    with pytest.raises(Refused) as exc:
        ws.resolve("../secrets.json", write=True)
    assert exc.value.code == "outside_workspace"
    with pytest.raises(Refused):
        ws.resolve(str(tmp_path / "elsewhere.pdf"), write=False)


@pytest.mark.parametrize("ref", ["C:/Windows/win.ini", "D:talk.tex", "//server/share/talk.tex"])
def test_a_drive_or_share_is_outside_on_every_platform(tmp_path: Path, ref: str):
    """On Linux `C:/Windows/win.ini` is a relative path, a folder named `C:` in the workspace:
    the playground's server said yes there to what it refused on Windows."""
    with pytest.raises(Refused) as exc:
        LocalWorkspace(tmp_path / "work").resolve(ref, write=False)
    assert exc.value.code == "outside_workspace"


def test_a_readable_folder_may_be_read_but_never_written(tmp_path: Path):
    outside = tmp_path / "library"
    outside.mkdir()
    (outside / "talk.pdf").write_bytes(b"%PDF-1.7")
    ws = LocalWorkspace(tmp_path / "work", readable=(outside,))
    assert ws.resolve(str(outside / "talk.pdf"), write=False).exists()
    with pytest.raises(Refused) as exc:
        ws.resolve(str(outside / "talk.pdf"), write=True)
    assert exc.value.code == "outside_workspace"


def test_staging_brings_an_outside_file_in(tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "talk.pdf").write_bytes(b"%PDF-1.7")
    ws = LocalWorkspace(tmp_path / "work")
    ref = ws.stage(outside / "talk.pdf", into="inbox")
    assert ref == "inbox/talk.pdf"
    assert ws.resolve(ref, write=False).read_bytes() == b"%PDF-1.7"


def test_refs_are_relative_and_forward_slashed(tmp_path: Path):
    ws = LocalWorkspace(tmp_path)
    assert ws.ref(ws.out_dir("talk")) == "out/talk"
    assert ws.ref(ws.root / "a" / "b.json") == "a/b.json"


def test_out_dir_does_not_depend_on_where_beamer2slides_is_installed(tmp_path: Path):
    """`paths.out_root()` answers differently in a checkout; a workspace must not."""
    ws = LocalWorkspace(tmp_path)
    assert ws.out_dir("talk") == tmp_path / "out" / "talk"


# -- the journey wrapper -----------------------------------------------------------------


@tool("noisy", needs=(READS,), local=None)
def noisy(j: Job, x: str = "x") -> None:
    print(f"working on {x}")
    print("and a second line")
    j.summary = "done"
    j.data["x"] = x
    j.artifact(j.ctx.workspace.root / "made.json", "json", "a file")
    j.warn("worth knowing", "slide 3")
    j.conflict("could not decide", "slide 7")
    j.suggest("deck_sync", "deck_sync")


def test_a_journey_returns_data_and_keeps_what_the_library_printed(tmp_path: Path):
    ctx = _ctx(tmp_path)
    seen: list[str] = []
    ctx.progress = seen.append
    r = noisy(ctx, x="talk.pdf")
    assert r.ok and r.code is None
    assert r.data["x"] == "talk.pdf"
    assert r.data["log"] == ["working on talk.pdf", "and a second line"]
    assert seen == ["working on talk.pdf", "and a second line"]
    assert [a.ref for a in r.artifacts] == ["made.json"]
    assert r.counts() == {"warning": 1, "conflict": 1}
    assert r.next_steps == ["deck_sync"]                       # suggested twice, said once
    assert json.loads(r.text())["tool"] == "noisy"


@tool("forbidden_body", needs=(WRITES_GOOGLE,), local=None)
def forbidden_body(j: Job) -> None:
    j.data["ran"] = True                                       # must never happen


def test_a_forbidden_journey_does_no_work_at_all(tmp_path: Path):
    r = forbidden_body(_context(tmp_path, _Access(_creds(), None), LOCAL_ONLY))
    assert (r.ok, r.code) == (False, "forbidden")
    assert "ran" not in r.data


def test_a_workspace_with_no_account_says_offline_rather_than_forbidden(tmp_path: Path):
    """`AgentContext.offline` withholds the permission *and* has nothing to give; say which."""
    r = forbidden_body(AgentContext.offline(tmp_path))
    assert (r.ok, r.code) == (False, "offline")
    assert "ran" not in r.data


@tool("needs_a_token", needs=(READS_GOOGLE,), local=None)
def needs_a_token(j: Job) -> None:
    j.data["ran"] = True


def test_a_dead_token_refuses_before_the_body_runs(tmp_path: Path):
    access = _Access(_creds(), Refused("needs_consent", "run the consent command", command="x"))
    r = needs_a_token(_context(tmp_path, access, ALL_ACTIONS))
    assert (r.ok, r.code) == (False, "needs_consent")
    assert r.data["command"] == "x"
    assert "ran" not in r.data
    assert access.asked == 1                                   # asked once, not once per call


def test_the_context_supplies_credentials_to_the_library(tmp_path: Path):
    """The library calls `google_auth.credentials()` everywhere; the wrapper answers it."""
    creds = _creds()
    ctx = _context(tmp_path, _Access(creds, None), ALL_ACTIONS)

    @tool("inner", needs=(READS_GOOGLE,), local=None)
    def inner(j: Job) -> None:
        j.data["same"] = google_auth.credentials() is creds

    assert inner(ctx).data["same"] is True
    assert google_auth._credentials_hook.get() is None         # and put back afterwards


def test_a_journey_that_needs_no_google_leaves_the_provider_alone(tmp_path: Path):
    @tool("local", needs=(READS,), local=None)
    def local(j: Job) -> None:
        j.data["provider"] = google_auth._credentials_hook.get() is None

    ctx = _context(tmp_path, _Access(_creds(), Refused("needs_consent", "no")), ALL_ACTIONS)
    assert local(ctx).data["provider"] is True                 # and no credential call was made


@tool("says_no", needs=(READS,), local=None)
def says_no(j: Job) -> None:
    raise Refused("no_base", "there is no base for this deck", url="https://x")


@tool("exits", needs=(READS,), local=None)
def exits(j: Job) -> None:
    raise SystemExit("the library refused in its own words")


@tool("missing", needs=(READS,), local=None)
def missing(j: Job) -> None:
    raise FileNotFoundError("talk.pdf")


@tool("breaks", needs=(READS,), local=None)
def breaks(j: Job) -> None:
    j.summary = "got halfway"
    raise ValueError("something unforeseen")


class RebuildRefused(Exception):
    pass


@tool("guarded", needs=(READS,), local=None)
def guarded(j: Job) -> None:
    raise RebuildRefused("slides 3, 7 and 9 were edited in Slides")


@pytest.mark.parametrize("fn, code", [(says_no, "no_base"), (exits, "refused"),
                                      (missing, "not_found"), (breaks, "failed"),
                                      (guarded, "deck_edited")])
def test_every_way_of_failing_comes_back_as_a_code(tmp_path: Path, fn: Tool, code: str):
    r = fn(_ctx(tmp_path))
    assert (r.ok, r.code) == (False, code)
    assert r.summary                                           # and always says something
    assert isinstance(r, Result)


def test_a_refusal_carries_its_data(tmp_path: Path):
    assert says_no(_ctx(tmp_path)).data["url"] == "https://x"


@tool("stops", needs=(READS,), local=None)
def stops(j: Job) -> None:
    next(iter(()))


def test_an_unforeseen_failure_says_where_it_happened(tmp_path: Path):
    """A bare StopIteration once reached a caller as "StopIteration: " and nothing else."""
    r = stops(_ctx(tmp_path))
    where, traceback = text(r.data, "where"), text(r.data, "traceback")
    assert r.code == "failed" and "test_agent_core.py" in where and "in stops" in where
    assert where in r.summary and "next(iter(()))" in traceback
    assert ":\\" not in traceback and not traceback.startswith("/")   # no machine paths
    assert "where" not in guarded(_ctx(tmp_path)).data                 # a known refusal stays plain


def test_a_body_that_fails_halfway_keeps_what_it_had_said(tmp_path: Path):
    r = breaks(_ctx(tmp_path))
    assert r.summary.startswith("got halfway")
    assert "ValueError" in r.summary


@tool("writes_for_real", needs=(READS, READS_GOOGLE), local=None)
def writes_for_real(j: Job, dry_run: bool = True) -> None:
    if not dry_run:
        j.require(WRITES_GOOGLE)
    j.data["wrote"] = not dry_run


def test_a_read_only_context_can_plan_but_not_write(tmp_path: Path):
    """`@tool` declares the least a journey does, so a dry run survives a read-only context."""
    ctx = _context(tmp_path, _Access(_creds(), None), frozenset({READS, READS_GOOGLE}))
    assert writes_for_real(ctx, dry_run=True).data["wrote"] is False
    refused = writes_for_real(ctx, dry_run=False)
    assert (refused.ok, refused.code) == (False, "forbidden")


def test_a_bad_argument_is_a_bad_request_not_a_crash(tmp_path: Path):
    r = noisy(_ctx(tmp_path), nonsense=1)
    assert (r.ok, r.code) == (False, "bad_request")


# -- the credential sources --------------------------------------------------------------


def test_no_google_refuses_with_offline():
    with pytest.raises(Refused) as exc:
        NoGoogle(reason="offline", fix=None).credentials()
    assert exc.value.code == "offline"
    assert NoGoogle(reason="offline", fix=None).describe()["available"] is False


def test_a_missing_token_asks_for_consent_and_never_opens_a_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole point of the source: a harness hangs forever on `run_local_server`."""
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    # The flow is imported where it is used (`gapi`: the Google packages are optional now), so the
    # guard is the import itself - reaching for it at all is the failure this test is about.
    class _NoBrowser:
        def __getattr__(self, name: str) -> NoReturn:
            raise AssertionError(f"the browser consent flow was reached ({name})")

    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", _NoBrowser())
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", _NoBrowser())
    source = TokenFile(token=tmp_path / "absent.json", client_secret=secret)
    with pytest.raises(Refused) as exc:
        source.credentials()
    assert exc.value.code == "needs_consent"
    assert "python -m beamer2slides.agent.auth" in str(exc.value)
    assert source.describe() == {"available": False, "source": "token file",
                                 "token_installed": False, "client_installed": True,
                                 "scopes": list(google_auth.SCOPES),
                                 "reason": "needs_consent",
                                 "command": "python -m beamer2slides.agent.auth"}


def test_no_client_at_all_is_a_different_answer(tmp_path: Path):
    source = TokenFile(token=tmp_path / "absent.json", client_secret=tmp_path / "none.json")
    with pytest.raises(Refused) as exc:
        source.credentials()
    assert exc.value.code == "no_credentials"


def test_describing_access_never_says_what_the_token_is(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "SECRET-VALUE", "refresh_token": "ALSO-SECRET",
                                 "client_id": "id", "client_secret": "shh",
                                 "scopes": list(google_auth.SCOPES)}), encoding="utf-8")
    described = json.dumps(TokenFile(token=token, client_secret=tmp_path / "c.json").describe())
    assert "SECRET-VALUE" not in described and "ALSO-SECRET" not in described
    assert "shh" not in described


# -- the two hooks, and what a server needs of them ---------------------------------------


def _in_its_own_thread(fn: Callable[[], _T]) -> _T:
    """Run `fn` on a thread of its own and give back what it returned, or raise what it raised.

    `threading.Thread` starts in a *fresh* context, so this is exactly what a server's worker
    inherits: nothing.
    """
    import threading

    answered: list[_T] = []
    raised: list[BaseException] = []

    def run() -> None:
        try:
            answered.append(fn())
        except BaseException as exc:                              # noqa: BLE001 - re-raised below
            raised.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    if raised:
        raise raised[0]
    return answered[0]


def test_two_requests_at_once_each_see_their_own_credentials():
    """The question the maintainers asked: is `use_provider` safe for concurrent server use?

    It is a `ContextVar`, so two threads each holding their own block answer with their own
    visitor's token and neither can reach the other's deck. A module-level global could not.
    """
    import threading

    first, second = _creds(), _creds()
    ready = threading.Barrier(2)
    seen: dict[str, Credentials] = {}

    def visitor(name: str, creds: "Credentials") -> Callable[[], None]:
        def run() -> None:
            with google_auth.use_provider(lambda: creds):
                ready.wait(10)                    # both blocks open at once, or the test proves nothing
                seen[name] = google_auth.credentials()
        return run

    threads = [threading.Thread(target=visitor("a", first)),
               threading.Thread(target=visitor("b", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert seen == {"a": first, "b": second}
    assert google_auth._credentials_hook.get() is None


def test_a_worker_thread_that_inherited_nothing_is_still_never_sent_to_the_browser():
    """A thread started inside the block runs in a fresh context and inherits nothing.

    The library's own pools do not care - every one of them resolves `credentials()` on the
    calling thread and hands the answer down - but a thread that asks anyway must not fall
    through to `InstalledAppFlow` and open a browser on a server. While exactly one block is
    open there is an unambiguous answer, so it is given.
    """
    creds = _creds()
    with google_auth.use_provider(lambda: creds):
        assert _in_its_own_thread(google_auth.credentials) is creds


def test_two_different_providers_and_a_thread_that_inherited_neither_is_an_error():
    """The one case with no answer: guessing would hand one visitor another's account."""
    import threading

    first, second = _creds(), _creds()
    ready, done = threading.Barrier(2), threading.Event()

    def hold(creds: "Credentials") -> Callable[[], None]:
        def run() -> None:
            with google_auth.use_provider(lambda: creds):
                ready.wait(10)
                done.wait(10)
        return run

    other = threading.Thread(target=hold(second))
    other.start()
    try:
        with google_auth.use_provider(lambda: first):
            ready.wait(10)
            with pytest.raises(RuntimeError) as exc:
                _in_its_own_thread(google_auth.credentials)
            assert "copy_context" in str(exc.value)              # and it says how to fix it
    finally:
        done.set()
        other.join(10)
    assert google_auth._credentials_hook.get() is None


def test_a_prebuilt_client_is_used_and_no_token_is_ever_looked_for(monkeypatch: pytest.MonkeyPatch):
    """`use_services` is the same hook one step later: a caller with its own client builder
    (a discovery document per `build()` is what makes them want one) hands it over, and nothing
    goes looking for credentials at all."""
    slides, drive = _Untouchable(), _Untouchable()
    monkeypatch.setattr(google_auth.gapi, "build", _explodes("build"))
    monkeypatch.setattr(google_auth, "credentials", _explodes("credentials"))
    with google_auth.use_services({"slides": slides, "drive": drive}):
        assert google_auth.slides_service() is slides
        assert google_auth.drive_service() is drive


def test_a_builder_is_asked_per_api_and_anything_it_declines_is_built_as_before(monkeypatch: pytest.MonkeyPatch):
    made = _Untouchable()
    builder = _Builder(made)

    def build(*a: object, **kw: object) -> tuple[str, object]:
        return ("built", a[0])

    def credentials() -> str:
        return "CREDS"

    monkeypatch.setattr(google_auth.gapi, "build", build)
    monkeypatch.setattr(google_auth, "credentials", credentials)
    asked = builder.asked
    with google_auth.use_services(builder):
        assert google_auth.slides_service() is made
        assert google_auth.docs_service() == ("built", "docs")
    assert [a for a, _, _ in asked] == ["slides", "docs"]
    assert asked[0][1] == "v1"
    # Nobody passed credentials in, so the builder is told so rather than being handed a token
    # fetched on its behalf: a client that carries its own is never a reason to go looking.
    assert asked[0][2] is None
    assert google_auth._services_hook.get() is None


def _explodes(what: str) -> Callable[..., NoReturn]:
    def boom(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"{what} was called")
    return boom


class _Untouchable:
    """A ready client of each API that no test may call: what is compared is its identity."""

    def presentations(self) -> "Presentations":
        raise AssertionError("a sentinel client was called")

    def files(self) -> "Files":
        raise AssertionError("a sentinel client was called")

    def permissions(self) -> "Permissions":
        raise AssertionError("a sentinel client was called")

    def comments(self) -> "Comments":
        raise AssertionError("a sentinel client was called")

    def documents(self) -> "Documents":
        raise AssertionError("a sentinel client was called")


class _Builder:
    """A caller's own client builder: `made` for Slides, and every other api left to the library."""

    def __init__(self, made: _Untouchable) -> None:
        self.made = made
        self.asked: list[tuple[str, str, Credentials | None]] = []

    @overload
    def __call__(self, api: Literal["slides"], version: str, creds: "Credentials | None") -> "SlidesService | None": ...
    @overload
    def __call__(self, api: Literal["drive"], version: str, creds: "Credentials | None") -> "DriveService | None": ...
    @overload
    def __call__(self, api: Literal["docs"], version: str, creds: "Credentials | None") -> "DocsService | None": ...

    def __call__(self, api: str, version: str,
                 creds: "Credentials | None") -> "SlidesService | DriveService | DocsService | None":
        self.asked.append((api, version, creds))
        return self.made if api == "slides" else None


# -- the first call ----------------------------------------------------------------------


class _Describes:
    """A credential source whose `describe` says whatever the test needs it to say."""

    def __init__(self, **described: Json) -> None:
        self.described: JsonObject = dict(described)

    def credentials(self) -> "Credentials":
        return _creds()

    def describe(self) -> JsonObject:
        return self.described


def test_an_expired_token_that_can_refresh_is_not_reported_as_a_problem(tmp_path: Path):
    """The ordinary state between calls: `available` rides on the refresh token alone.

    Saying "good until <a time already past>" reads like a fault, and an agent that
    believes it goes asking a person for consent nobody needs to give.
    """
    from beamer2slides.agent import tools

    access = _Describes(available=True, source="token file", expired=True, refreshable=True,
                        expires="2026-09-19T20:55:00+00:00")
    r = tools.TOOLS["b2s_status"](_context(tmp_path, access, ALL_ACTIONS))
    assert r.ok and "good until" not in r.summary
    assert "refreshed on the next call" in r.summary
    assert not [d for d in r.diagnostics if "Google" in d.message]


def test_no_google_is_said_plainly_with_the_command_that_fixes_it(tmp_path: Path):
    from beamer2slides.agent import tools

    access = _Describes(available=False, source="token file", reason="needs_consent",
                        command="python -m beamer2slides.agent.auth")
    r = tools.TOOLS["b2s_status"](_context(tmp_path, access, ALL_ACTIONS))
    assert r.ok                                                # status itself never fails
    assert "not reachable" in r.summary and "python -m beamer2slides.agent.auth" in r.summary
    assert any(d.level == "warning" for d in r.diagnostics)


def test_a_host_that_knows_the_way_back_to_google_says_it(tmp_path: Path):
    """Not every absence is a machine with no account: the playground's visitor signs in.

    "offline" there reads as a server that cannot reach Google at all, on a page with a
    sign-in button on it, so a source may carry the one sentence that says what to do.
    """
    from beamer2slides.agent import tools

    source = NoGoogle("no token in this run", "Sign in at the top of the page.")
    assert source.describe() == {"available": False, "reason": "no token in this run",
                                 "scopes": [], "fix": "Sign in at the top of the page."}
    r = tools.TOOLS["b2s_status"](_context(tmp_path, source, ALL_ACTIONS))
    assert "no token in this run" in r.summary and "Sign in at the top of the page." in r.summary
    with pytest.raises(Refused) as exc:
        source.credentials()
    assert exc.value.code == "offline" and "Sign in at the top" in str(exc.value)


# -- the order they are met in -----------------------------------------------------------


def test_the_registry_is_in_the_order_of_operations():
    """A dropdown, a model's tool list and the guide all read top to bottom.

    Iterating the modules put `deck_convert` above `deck_inspect` and `doc_adopt` above
    `doc_push` - the two orders INSTRUCTIONS.md spends a section telling people not to follow.
    """
    from beamer2slides.agent import tools

    assert list(tools.TOOLS) == list(tools.ORDER)
    order = list(tools.TOOLS)
    assert order[0] == "b2s_status"
    assert order.index("deck_inspect") < order.index("deck_convert") < order.index("deck_sync")
    assert order.index("tex_label") < order.index("deck_convert")
    assert order.index("doc_push") < order.index("doc_sync") < order.index("doc_adopt")


def test_a_journey_missing_from_the_order_is_a_failure_not_a_tool_that_sorts_last():
    from beamer2slides.agent import tools

    with pytest.raises(RuntimeError, match="not in ORDER: deck_teleport"):
        tools._ordered({**tools.TOOLS, "deck_teleport": lambda: None})
    with pytest.raises(RuntimeError, match="no such tool: b2s_status"):
        tools._ordered({n: f for n, f in tools.TOOLS.items() if n != "b2s_status"})


# -- the guide ---------------------------------------------------------------------------


def test_the_instructions_travel_with_the_package():
    from beamer2slides.agent import tools

    text = tools.instructions()
    assert "Never rebuild a deck somebody has edited" in text
    for code in ("deck_edited", "no_base", "needs_consent", "base_choice_needed"):
        assert code in text, f"{code} is a refusal an agent will meet and the guide omits it"


def test_the_refusal_vocabulary_is_the_code_type_and_nothing_more() -> None:
    """`Code` is what a refusal can be typed as and `CODES` what the model is told each one
    means: one without the other is a code the checker allows and nobody explained, or one
    explained that no journey can raise."""
    assert set(CODES) == set(get_args(Code))
    assert all(said.strip() for said in CODES.values())
