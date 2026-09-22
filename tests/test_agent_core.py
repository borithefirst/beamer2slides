"""The agent layer's core: the workspace boundary, the journey wrapper, the credential seam.

These are the promises every tool is built on - that a path cannot climb out of the workspace,
that a forbidden or unauthenticated journey does no work at all, that nothing the library prints
or raises crosses the boundary as anything but data. Each one is tested by breaking it.
"""

import json
import sys

import pytest

from beamer2slides import google_auth
from beamer2slides.agent import (ALL_ACTIONS, LOCAL_ONLY, READS, READS_GOOGLE, WRITES,
                                 WRITES_GOOGLE, AgentContext, LocalWorkspace, NoGoogle, Refused,
                                 Result, TokenFile)
from beamer2slides.agent.context import tool


class _Access:
    """A credential source that hands back a sentinel, or refuses the way a dead token does."""

    def __init__(self, creds=None, refusal=None):
        self.creds = creds
        self.refusal = refusal
        self.asked = 0

    def credentials(self):
        self.asked += 1
        if self.refusal:
            raise self.refusal
        return self.creds

    def describe(self):
        return {"available": not self.refusal, "source": "test"}


def _ctx(tmp_path, **kw):
    kw.setdefault("google", _Access(creds=object()))
    kw.setdefault("allow", ALL_ACTIONS)
    return AgentContext(workspace=LocalWorkspace(tmp_path), **kw)


# -- the workspace boundary --------------------------------------------------------------


def test_a_path_cannot_climb_out_of_the_workspace(tmp_path):
    ws = LocalWorkspace(tmp_path / "work")
    with pytest.raises(Refused) as exc:
        ws.resolve("../secrets.json", write=True)
    assert exc.value.code == "outside_workspace"
    with pytest.raises(Refused):
        ws.resolve(str(tmp_path / "elsewhere.pdf"))


def test_a_readable_folder_may_be_read_but_never_written(tmp_path):
    outside = tmp_path / "library"
    outside.mkdir()
    (outside / "talk.pdf").write_bytes(b"%PDF-1.7")
    ws = LocalWorkspace(tmp_path / "work", readable=(outside,))
    assert ws.resolve(str(outside / "talk.pdf")).exists()
    with pytest.raises(Refused) as exc:
        ws.resolve(str(outside / "talk.pdf"), write=True)
    assert exc.value.code == "outside_workspace"


def test_staging_brings_an_outside_file_in(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "talk.pdf").write_bytes(b"%PDF-1.7")
    ws = LocalWorkspace(tmp_path / "work")
    ref = ws.stage(outside / "talk.pdf")
    assert ref == "inbox/talk.pdf"
    assert ws.resolve(ref).read_bytes() == b"%PDF-1.7"


def test_refs_are_relative_and_forward_slashed(tmp_path):
    ws = LocalWorkspace(tmp_path)
    assert ws.ref(ws.out_dir("talk")) == "out/talk"
    assert ws.ref(ws.root / "a" / "b.json") == "a/b.json"


def test_out_dir_does_not_depend_on_where_beamer2slides_is_installed(tmp_path):
    """`paths.out_root()` answers differently in a checkout; a workspace must not."""
    ws = LocalWorkspace(tmp_path)
    assert ws.out_dir("talk") == tmp_path / "out" / "talk"


# -- the journey wrapper -----------------------------------------------------------------


@tool("noisy", needs=(READS,))
def noisy(j, x: str = "x"):
    print(f"working on {x}")
    print("and a second line")
    j.summary = "done"
    j.data["x"] = x
    j.artifact(j.ctx.workspace.root / "made.json", "json", "a file")
    j.warn("worth knowing", "slide 3")
    j.conflict("could not decide", "slide 7")
    j.suggest("deck_sync", "deck_sync")


def test_a_journey_returns_data_and_keeps_what_the_library_printed(tmp_path):
    ctx = _ctx(tmp_path)
    seen = []
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


@tool("forbidden_body", needs=(WRITES_GOOGLE,))
def forbidden_body(j):
    j.data["ran"] = True                                       # must never happen


def test_a_forbidden_journey_does_no_work_at_all(tmp_path):
    r = forbidden_body(_ctx(tmp_path, allow=LOCAL_ONLY))
    assert (r.ok, r.code) == (False, "forbidden")
    assert "ran" not in r.data


def test_a_workspace_with_no_account_says_offline_rather_than_forbidden(tmp_path):
    """`AgentContext.offline` withholds the permission *and* has nothing to give; say which."""
    r = forbidden_body(AgentContext.offline(tmp_path))
    assert (r.ok, r.code) == (False, "offline")
    assert "ran" not in r.data


@tool("needs_a_token", needs=(READS_GOOGLE,))
def needs_a_token(j):
    j.data["ran"] = True


def test_a_dead_token_refuses_before_the_body_runs(tmp_path):
    access = _Access(refusal=Refused("needs_consent", "run the consent command", command="x"))
    r = needs_a_token(_ctx(tmp_path, google=access))
    assert (r.ok, r.code) == (False, "needs_consent")
    assert r.data["command"] == "x"
    assert "ran" not in r.data
    assert access.asked == 1                                   # asked once, not once per call


def test_the_context_supplies_credentials_to_the_library(tmp_path):
    """The library calls `google_auth.credentials()` everywhere; the wrapper answers it."""
    creds = object()
    ctx = _ctx(tmp_path, google=_Access(creds=creds))

    @tool("inner", needs=(READS_GOOGLE,))
    def inner(j):
        j.data["same"] = google_auth.credentials() is creds

    assert inner(ctx).data["same"] is True
    assert google_auth._credentials_hook.get() is None         # and put back afterwards


def test_a_journey_that_needs_no_google_leaves_the_provider_alone(tmp_path):
    @tool("local", needs=(READS,))
    def local(j):
        j.data["provider"] = google_auth._credentials_hook.get() is None

    ctx = _ctx(tmp_path, google=_Access(refusal=Refused("needs_consent", "no")))
    assert local(ctx).data["provider"] is True                 # and no credential call was made


@tool("says_no", needs=(READS,))
def says_no(j):
    raise Refused("no_base", "there is no base for this deck", url="https://x")


@tool("exits", needs=(READS,))
def exits(j):
    raise SystemExit("the library refused in its own words")


@tool("missing", needs=(READS,))
def missing(j):
    raise FileNotFoundError("talk.pdf")


@tool("breaks", needs=(READS,))
def breaks(j):
    j.summary = "got halfway"
    raise ValueError("something unforeseen")


class RebuildRefused(Exception):
    pass


@tool("guarded", needs=(READS,))
def guarded(j):
    raise RebuildRefused("slides 3, 7 and 9 were edited in Slides")


@pytest.mark.parametrize("fn, code", [(says_no, "no_base"), (exits, "refused"),
                                      (missing, "not_found"), (breaks, "failed"),
                                      (guarded, "deck_edited")])
def test_every_way_of_failing_comes_back_as_a_code(tmp_path, fn, code):
    r = fn(_ctx(tmp_path))
    assert (r.ok, r.code) == (False, code)
    assert r.summary                                           # and always says something
    assert isinstance(r, Result)


def test_a_refusal_carries_its_data(tmp_path):
    assert says_no(_ctx(tmp_path)).data["url"] == "https://x"


def test_a_body_that_fails_halfway_keeps_what_it_had_said(tmp_path):
    r = breaks(_ctx(tmp_path))
    assert r.summary.startswith("got halfway")
    assert "ValueError" in r.summary


@tool("writes_for_real", needs=(READS, READS_GOOGLE))
def writes_for_real(j, dry_run: bool = True):
    if not dry_run:
        j.require(WRITES_GOOGLE)
    j.data["wrote"] = not dry_run


def test_a_read_only_context_can_plan_but_not_write(tmp_path):
    """`@tool` declares the least a journey does, so a dry run survives a read-only context."""
    ctx = _ctx(tmp_path, allow=frozenset({READS, READS_GOOGLE}))
    assert writes_for_real(ctx, dry_run=True).data["wrote"] is False
    refused = writes_for_real(ctx, dry_run=False)
    assert (refused.ok, refused.code) == (False, "forbidden")


def test_a_bad_argument_is_a_bad_request_not_a_crash(tmp_path):
    r = noisy(_ctx(tmp_path), nonsense=1)
    assert (r.ok, r.code) == (False, "bad_request")


# -- the credential sources --------------------------------------------------------------


def test_no_google_refuses_with_offline():
    with pytest.raises(Refused) as exc:
        NoGoogle().credentials()
    assert exc.value.code == "offline"
    assert NoGoogle().describe()["available"] is False


def test_a_missing_token_asks_for_consent_and_never_opens_a_browser(tmp_path, monkeypatch):
    """The whole point of the source: a harness hangs forever on `run_local_server`."""
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    # The flow is imported where it is used (`gapi`: the Google packages are optional now), so the
    # guard is the import itself - reaching for it at all is the failure this test is about.
    class _NoBrowser:
        def __getattr__(self, name):
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


def test_no_client_at_all_is_a_different_answer(tmp_path):
    source = TokenFile(token=tmp_path / "absent.json", client_secret=tmp_path / "none.json")
    with pytest.raises(Refused) as exc:
        source.credentials()
    assert exc.value.code == "no_credentials"


def test_describing_access_never_says_what_the_token_is(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "SECRET-VALUE", "refresh_token": "ALSO-SECRET",
                                 "client_id": "id", "client_secret": "shh",
                                 "scopes": list(google_auth.SCOPES)}), encoding="utf-8")
    described = json.dumps(TokenFile(token=token, client_secret=tmp_path / "c.json").describe())
    assert "SECRET-VALUE" not in described and "ALSO-SECRET" not in described
    assert "shh" not in described


# -- the two hooks, and what a server needs of them ---------------------------------------


def _in_its_own_thread(fn):
    """Run `fn` on a thread of its own and give back what it returned, or raise what it raised.

    `threading.Thread` starts in a *fresh* context, so this is exactly what a server's worker
    inherits: nothing.
    """
    import threading

    answer: list = []

    def run():
        try:
            answer.append(("ok", fn()))
        except BaseException as exc:                              # noqa: BLE001 - re-raised below
            answer.append(("raised", exc))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    kind, value = answer[0]
    if kind == "raised":
        raise value
    return value


def test_two_requests_at_once_each_see_their_own_credentials():
    """The question the maintainers asked: is `use_provider` safe for concurrent server use?

    It is a `ContextVar`, so two threads each holding their own block answer with their own
    visitor's token and neither can reach the other's deck. A module-level global could not.
    """
    import threading

    first, second = object(), object()
    ready, seen = threading.Barrier(2), {}

    def visitor(name, creds):
        def run():
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
    creds = object()
    with google_auth.use_provider(lambda: creds):
        assert _in_its_own_thread(google_auth.credentials) is creds


def test_two_different_providers_and_a_thread_that_inherited_neither_is_an_error():
    """The one case with no answer: guessing would hand one visitor another's account."""
    import threading

    first, second = object(), object()
    ready, done = threading.Barrier(2), threading.Event()

    def hold(creds):
        def run():
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


def test_a_prebuilt_client_is_used_and_no_token_is_ever_looked_for(monkeypatch):
    """`use_services` is the same hook one step later: a caller with its own client builder
    (a discovery document per `build()` is what makes them want one) hands it over, and nothing
    goes looking for credentials at all."""
    slides, drive = object(), object()
    monkeypatch.setattr(google_auth.gapi, "build", _explodes("build"))
    monkeypatch.setattr(google_auth, "credentials", _explodes("credentials"))
    with google_auth.use_services({"slides": slides, "drive": drive}):
        assert google_auth.slides_service() is slides
        assert google_auth.drive_service() is drive


def test_a_builder_is_asked_per_api_and_anything_it_declines_is_built_as_before(monkeypatch):
    asked, made = [], object()

    def builder(api, version, creds):
        asked.append((api, version, creds))
        return made if api == "slides" else None

    monkeypatch.setattr(google_auth.gapi, "build", lambda *a, **kw: ("built", a[0]))
    monkeypatch.setattr(google_auth, "credentials", lambda: "CREDS")
    with google_auth.use_services(builder):
        assert google_auth.slides_service() is made
        assert google_auth.docs_service() == ("built", "docs")
    assert [a for a, _, _ in asked] == ["slides", "docs"]
    assert asked[0][1] == "v1"
    # Nobody passed credentials in, so the builder is told so rather than being handed a token
    # fetched on its behalf: a client that carries its own is never a reason to go looking.
    assert asked[0][2] is None
    assert google_auth._services_hook.get() is None


def _explodes(what):
    def boom(*args, **kwargs):
        raise AssertionError(f"{what} was called")
    return boom


# -- the first call ----------------------------------------------------------------------


class _Describes:
    """A credential source whose `describe` says whatever the test needs it to say."""

    def __init__(self, **described):
        self.described = described

    def credentials(self):
        return object()

    def describe(self):
        return self.described


def test_an_expired_token_that_can_refresh_is_not_reported_as_a_problem(tmp_path):
    """The ordinary state between calls: `available` rides on the refresh token alone.

    Saying "good until <a time already past>" reads like a fault, and an agent that
    believes it goes asking a person for consent nobody needs to give.
    """
    from beamer2slides.agent import tools

    access = _Describes(available=True, source="token file", expired=True, refreshable=True,
                        expires="2026-09-19T20:55:00+00:00")
    r = tools.TOOLS["b2s_status"](_ctx(tmp_path, google=access))
    assert r.ok and "good until" not in r.summary
    assert "refreshed on the next call" in r.summary
    assert not [d for d in r.diagnostics if "Google" in d.message]


def test_no_google_is_said_plainly_with_the_command_that_fixes_it(tmp_path):
    from beamer2slides.agent import tools

    access = _Describes(available=False, source="token file", reason="needs_consent",
                        command="python -m beamer2slides.agent.auth")
    r = tools.TOOLS["b2s_status"](_ctx(tmp_path, google=access))
    assert r.ok                                                # status itself never fails
    assert "not reachable" in r.summary and "python -m beamer2slides.agent.auth" in r.summary
    assert any(d.level == "warning" for d in r.diagnostics)


def test_a_host_that_knows_the_way_back_to_google_says_it(tmp_path):
    """Not every absence is a machine with no account: the playground's visitor signs in.

    "offline" there reads as a server that cannot reach Google at all, on a page with a
    sign-in button on it, so a source may carry the one sentence that says what to do.
    """
    from beamer2slides.agent import tools

    source = NoGoogle("no token in this run", "Sign in at the top of the page.")
    assert source.describe() == {"available": False, "reason": "no token in this run",
                                 "scopes": [], "fix": "Sign in at the top of the page."}
    r = tools.TOOLS["b2s_status"](_ctx(tmp_path, google=source))
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
