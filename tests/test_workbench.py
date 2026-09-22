"""The workbench, offline: a workspace, the files in it, and journeys run in a process of their own.

No Google (the server is not given any), so the tools that need it must come back saying `offline`
rather than failing - which is the whole point of the agent layer's vocabulary reaching the page.
"""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from beamer2slides.playground import server, workbench

from .test_playground import call


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    jobs = tmp_path_factory.mktemp("bench-jobs")
    mp = pytest.MonkeyPatch()
    mp.setenv("B2S_PLAYGROUND_JOBS", str(jobs))
    mp.delenv("B2S_PLAYGROUND_GOOGLE", raising=False)
    mp.delenv("B2S_PLAYGROUND_GOOGLE_CLIENT_ID", raising=False)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    stale = jobs / str(port) / "ws" / "0123456789ab"   # a workspace an earlier run on this port left
    stale.mkdir(parents=True)
    server.Handler.app = server.Playground(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    assert not stale.exists()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    mp.undo()


def request(url, method, data=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": ctype} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, json.loads(body) if r.headers["Content-Type"] == "application/json" else body
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def ws(base):
    status, made = request(f"{base}/api/ws", "POST", b"")
    assert status == 201
    return f"{base}/api/ws/{made['id']}"


def run(ws, tool, args=None, seconds=180):
    status, made = request(f"{ws}/runs", "POST",
                           json.dumps({"tool": tool, "args": args or {}}).encode())
    assert status == 201, made
    return _wait(ws, made["id"], seconds)


def _wait(ws, rid, seconds=180):
    for _ in range(seconds * 4):
        status, state = call(f"{ws}/runs/{rid}")
        if state["state"] == "done":
            return state
        time.sleep(0.25)
    raise AssertionError(f"run {rid} never finished")


def test_every_journey_is_offered_with_its_own_schema(base):
    """The form a visitor fills in is read off the tools themselves, so the two cannot drift."""
    status, catalogue = call(f"{base}/api/tools")
    assert status == 200
    names = [t["name"] for t in catalogue["tools"]]
    assert "tex_compile" in names and "deck_convert" in names and "doc_sync" in names
    # the eleven journeys, deck_convert's two halves, and the compile step
    assert len(names) == 14
    # The dropdown reads top to bottom, and what it reads there is the order of operations:
    # compile first, because that is where a talk in this workspace starts.
    from beamer2slides.agent import tools as registry
    assert names == ["tex_compile", *registry.ORDER]
    for tool in catalogue["tools"]:
        assert tool["description"].strip() and tool["input_schema"]["type"] == "object"
        for name, prop in tool["input_schema"]["properties"].items():
            assert prop.get("description"), f"{tool['name']}.{name}"
    assert "never rebuild" in catalogue["instructions"].lower() or catalogue["instructions"].strip()
    convert = next(t for t in catalogue["tools"] if t["name"] == "deck_convert")
    assert convert["effects"]["writes_google"] and convert["effects"]["approval"] == "required"


def test_a_new_workspace_has_something_to_start_from(ws):
    status, view = call(ws)
    assert status == 200
    names = {f["path"] for f in view["files"]}
    assert {"README.md", "talk.tex", "doc.html"} <= names
    assert view["bytes"] > 0 and view["limits"]["files"] == workbench.MAX_FILES
    status, talk = call(f"{ws}/file?path=talk.tex")
    assert status == 200 and b"\\begin{frame}" in talk


def test_files_are_written_read_and_deleted(ws):
    assert request(f"{ws}/file?path=notes/one.txt", "PUT", b"hello", "text/plain")[0] == 200
    status, body = call(f"{ws}/file?path=notes/one.txt")
    assert status == 200 and body == b"hello"
    assert {"notes", "notes/one.txt"} <= {f["path"] for f in call(ws)[1]["files"]}
    assert request(f"{ws}/file?path=notes/one.txt", "DELETE")[0] == 200
    assert call(f"{ws}/file?path=notes/one.txt")[0] == 404


PAGE = b"""<html>
<body>
<p id="a">The editor will change this sentence about hedgerows.</p>
<p id="b">The run will change this sentence about lighthouses.</p>
</body>
</html>
"""


def opened(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read(), r.headers["X-B2S-Version"]


def test_a_save_over_a_file_a_run_rewrote_is_merged_with_it(ws):
    """The editor's buffer is older than the file, and saving it back would be a revert.

    A journey rewrites the files it is pointed at - `doc_sync` regenerates the canonical
    HTML from the document it has just written - so a buffer opened before a run says
    what the file said *before* it. Saving that is not an edit; the next sync reads it as
    the source dropping whatever the rewrite brought in, and deletes those blocks from
    somebody's document. Measured on the playground, on a live document, twice.

    So the two are merged against the bytes the editor was handed, which the session keeps.
    Both edits land and neither side is asked anything.
    """
    url = f"{ws}/file?path=page.html"
    assert request(url, "PUT", PAGE, "text/html")[0] == 200
    was, stamp = opened(url)
    assert was == PAGE and stamp and len(stamp) == 16

    request(url, "PUT", PAGE.replace(b"lighthouses", b"lighthouses and harbours"),
            "text/html")                                            # the run, underneath
    status, said = request(f"{url}&version={stamp}", "PUT",
                           PAGE.replace(b"hedgerows", b"hedgerows and ditches"), "text/html")
    assert status == 200 and said["merged"] is True
    now = call(url)[1]
    assert b"hedgerows and ditches" in now and b"lighthouses and harbours" in now
    assert said["version"] == workbench.digest(now)

    # An upload names no stamp and replaces what is there, which is what an upload means.
    assert request(url, "PUT", b"<p>uploaded</p>", "text/html")[0] == 200


def test_a_part_both_sides_changed_is_refused_and_nothing_is_written(ws):
    """What cannot be merged is not guessed at, and no marker is left in the file.

    A conflict marker in a canonical HTML file is not a marker to the next sync: it is
    words outside every block, which is the one thing `doc_ir` stops a sync over. So the
    save comes back with both versions quoted and the file untouched.
    """
    url = f"{ws}/file?path=clash.html"
    assert request(url, "PUT", PAGE, "text/html")[0] == 200
    was, stamp = opened(url)
    ran = PAGE.replace(b"hedgerows", b"hedgerows and quicksets")
    request(url, "PUT", ran, "text/html")

    status, answer = request(f"{url}&version={stamp}", "PUT",
                             PAGE.replace(b"hedgerows", b"hedgerows and ditches"), "text/html")
    assert status == 409 and "overlap" in answer["error"]
    # Each side's version, in the line it stands in: one word is not a place anybody can find.
    assert "about hedgerows and ditches." in answer["error"]
    assert "about hedgerows and quicksets." in answer["error"]
    assert call(url)[1] == ran                                  # nothing was written

    # And the save that names what the file says now goes through, as it always did.
    mine = b"<p>settled by hand</p>"
    assert request(f"{url}&version={opened(url)[1]}", "PUT", mine, "text/html")[0] == 200
    assert call(url)[1] == mine


def test_a_stale_save_the_server_cannot_merge_is_refused(ws):
    """The base is memory, and memory is bounded: what is no longer there is not guessed at.

    A stamp this server has forgotten (or a file that is not text) leaves nothing to merge
    against, and then the only safe answer is the one from before there was a merge at all.
    """
    bench = server.Handler.app.bench
    session = bench.get(ws.rsplit("/", 1)[1])
    path = session.root / "forgotten.txt"
    path.write_bytes(b"as it was")
    stamp = session.remember("forgotten.txt", b"as it was")
    path.write_bytes(b"as the run left it")

    session.seen.clear()
    with pytest.raises(workbench.Denied) as refused:
        bench.write(session, "forgotten.txt", b"as I typed it", stamp)
    assert refused.value.status == 409 and "reload it" in str(refused.value)
    assert path.read_bytes() == b"as the run left it"

    # Bounded, and the newest kept: one file opened many times does not fill the workspace.
    for n in range(workbench.KEEP_VERSIONS + 8):
        session.remember("forgotten.txt", f"version {n}".encode())
    assert len(session.seen) == workbench.KEEP_VERSIONS
    assert f"forgotten.txt\0{workbench.digest(b'version 23')}" in session.seen


def test_a_new_file_never_lands_on_one_that_is_already_there(ws):
    """The empty stamp is "there was nothing of that name when I asked"."""
    status, answer = request(f"{ws}/file?path=talk.tex&version=", "PUT", b"", "text/plain")
    assert status == 409 and "already here" in answer["error"]
    assert b"\\begin{frame}" in call(f"{ws}/file?path=talk.tex")[1]
    assert request(f"{ws}/file?path=fresh.txt&version=", "PUT", b"x", "text/plain")[0] == 200


def test_the_page_saves_with_the_stamp_it_opened_the_file_with():
    """The other half: the server can only merge a save that says which file it read."""
    js = (server.STATIC / "workbench.js").read_text(encoding="utf-8")
    assert 'stamp = r.headers.get("X-B2S-Version")' in js
    assert "`&version=${encodeURIComponent(version)}`" in js
    assert "await afterRun();" in js                    # and the open file is read again
    # What nothing may do is throw away what somebody has typed: only an untouched
    # buffer is replaced by what the run wrote.
    assert 'if ($("#filetext").value === loaded) {' in js
    # A merged save is not the buffer: the editor shows what the merge really put there.
    assert "if (said.merged) {" in js


def test_nothing_reaches_outside_the_workspace(ws, base):
    """The same boundary a journey's own path crosses: `LocalWorkspace.resolve` draws it once."""
    for ref in ("../escape.txt", "../../pyproject.toml", "/etc/passwd", "C:/Windows/win.ini"):
        status, answer = request(f"{ws}/file?path={urllib.parse.quote(ref)}", "PUT", b"x", "text/plain")
        assert status == 403 and "outside the workspace" in answer["error"], ref
        assert call(f"{ws}/file?path={urllib.parse.quote(ref)}")[0] in (403, 404)
    assert call(f"{base}/api/ws/ffffffffffff")[0] == 404


def test_a_local_journey_runs_in_the_workspace(ws):
    """`b2s_status` needs nothing but the folder, and says what is in it."""
    state = run(ws, "b2s_status")
    result = state["result"]
    assert result["ok"] and result["tool"] == "b2s_status"
    assert "LaTeX source" in result["summary"]
    assert result["data"]["workspace"].endswith(ws.rsplit("/", 1)[1])
    assert result["data"]["google"]["available"] is False
    assert sorted(result["data"]["allows"]) == ["reads", "writes"]   # no account: no Google actions


def test_a_google_journey_on_a_server_with_no_account_says_so(ws):
    """Not a crash and not a traceback: the refusal vocabulary reaches the page as a code."""
    state = run(ws, "deck_convert", {"pdf": "talk.pdf"})
    assert state["result"]["ok"] is False
    assert state["result"]["code"] == "offline"
    assert "Google" in state["result"]["summary"]


def test_a_tool_nobody_has_is_a_bad_request(ws):
    assert request(f"{ws}/runs", "POST", json.dumps({"tool": "rm_rf"}).encode())[0] == 404
    state = run(ws, "b2s_status", {"nonsense": 1})
    assert state["result"]["code"] == "bad_request" and "does not take" in state["result"]["summary"]


def test_a_compile_with_no_tex_engine_is_refused_in_the_result(ws, monkeypatch):
    monkeypatch.setattr(server.Handler.app.bench, "engines", [])
    state = run(ws, "tex_compile", {"tex": "talk.tex"})
    assert state["result"]["ok"] is False and "no TeX distribution" in state["result"]["summary"]


def fake_engine(monkeypatch, folder, writes):
    """A TeX engine that writes `writes[n]` into the folder on its n-th run (the last one over)."""
    runs = []

    def run(cmd, **kw):
        state = writes[min(len(runs), len(writes) - 1)]
        for name, text in state.items():
            (folder / name).write_text(text, encoding="utf-8")
        (folder / "talk.pdf").write_bytes(b"%PDF-1.4\n")
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(workbench.subprocess, "run", run)
    return runs


def test_a_compile_stops_when_the_auxiliary_files_stop_moving(tmp_path, monkeypatch):
    # The loop an agent runs: the folder's .aux and .nav are settled before the turn begins, so
    # one pass draws the PDF and a second would draw the same one.
    (tmp_path / "talk.tex").write_text(r"\documentclass{beamer}", encoding="utf-8")
    settled = {"talk.aux": "settled", "talk.nav": "settled"}
    for name, text in settled.items():        # what the turn before this one left
        (tmp_path / name).write_text(text, encoding="utf-8")
    runs = fake_engine(monkeypatch, tmp_path, [settled])
    workbench.run_latex(tmp_path, "talk.tex", ["pdflatex"])
    assert len(runs) == 1


def test_a_compile_runs_again_while_they_move(tmp_path, monkeypatch):
    # A fresh folder: the first pass writes the .nav from nothing, and beamer's frame total comes
    # out of the .nav the *next* pass reads - which no line of the log asks for
    # (`inverse.aux_state`, the rule the pull loop's own compile goes by).
    (tmp_path / "talk.tex").write_text(r"\documentclass{beamer}", encoding="utf-8")
    runs = fake_engine(monkeypatch, tmp_path,
                       [{"talk.nav": "one"}, {"talk.nav": "two"}, {"talk.nav": "two"}])
    workbench.run_latex(tmp_path, "talk.tex", ["pdflatex"])
    assert len(runs) == 3
    # and never past the cap, however long they keep moving
    runs = fake_engine(monkeypatch, tmp_path,
                       [{"talk.nav": "a"}, {"talk.nav": "b"}, {"talk.nav": "c"}])
    workbench.run_latex(tmp_path, "talk.tex", ["pdflatex"], passes=2)
    assert len(runs) == 2


@pytest.mark.skipif(not server.tex_engines(), reason="no TeX distribution")
def test_a_talk_compiles_and_then_inspects(ws):
    state = run(ws, "tex_compile", {"tex": "talk.tex"})
    assert state["result"]["ok"], state["result"]["summary"]
    assert state["result"]["data"]["pdf"] == "talk.pdf"
    assert call(f"{ws}/file?path=talk.pdf")[1][:4] == b"%PDF"
    state = run(ws, "deck_inspect", {"pdf": "talk.pdf"})
    assert state["result"]["ok"], state["result"]["summary"]
    assert state["result"]["data"]["pages"] == 3
    assert "native" in state["result"]["summary"]


CHILD = """\
import json, sys, time
job = json.loads(sys.stdin.read())
{body}
"""
SAYS = CHILD.format(body="""
for n in range(3):
    print(json.dumps({"progress": f"line {n}"}), flush=True)
print(json.dumps({"result": {"tool": job["tool"], "ok": True, "summary": "said so",
                             "data": {"args": job["args"], "google": job["google"],
                                      "root": job["root"]},
                             "artifacts": [], "diagnostics": [], "next_steps": []}}), flush=True)
""")
HANGS = CHILD.format(body="time.sleep(600)")
DIES = CHILD.format(body="""
print("a traceback nobody meant to write", file=sys.stderr)
sys.exit(3)
""")


def child(tmp_path, source):
    path = tmp_path / "child.py"
    path.write_text(source, encoding="utf-8")
    return [sys.executable, "-u", str(path)]


def test_the_child_streams_its_progress_and_its_result(ws, tmp_path, monkeypatch):
    """What the page shows while a conversion runs: the lines the library printed, as they come."""
    bench = server.Handler.app.bench
    monkeypatch.setattr(bench, "command", lambda: child(tmp_path, SAYS))
    state = run(ws, "b2s_status", {"out": "somewhere"})
    assert state["log"] == ["line 0", "line 1", "line 2"]
    assert state["result"]["ok"] and state["result"]["data"]["args"] == {"out": "somewhere"}
    assert state["result"]["data"]["root"].endswith(ws.rsplit("/", 1)[1])
    assert state["seconds"] >= 0


def test_a_visitors_token_goes_to_the_child_and_no_further(base, ws, tmp_path, monkeypatch):
    """It is handed over on stdin - never a command line, never the run record - and only to a
    journey that needs Google."""
    monkeypatch.setenv("B2S_PLAYGROUND_GOOGLE_CLIENT_ID", "1234.apps.googleusercontent.com")
    bench = server.Handler.app.bench
    monkeypatch.setattr(bench, "command", lambda: child(tmp_path, SAYS))

    status, answer = request(f"{ws}/runs", "POST",
                             json.dumps({"tool": "deck_convert", "args": {"pdf": "t.pdf"}}).encode())
    assert status == 401 and "sign in" in answer["error"]   # a deck needs a Drive to go into

    asked = {"tool": "deck_convert", "args": {"pdf": "t.pdf"}, "access_token": "ya29.thesecret"}
    status, made = request(f"{ws}/runs", "POST", json.dumps(asked).encode())
    state = _wait(ws, made["id"])
    assert state["result"]["data"]["google"] == {"mode": "signin", "token": "ya29.thesecret"}
    assert "thesecret" not in json.dumps(call(ws)[1])       # not in the workspace's own view

    asked["tool"] = "deck_inspect"                          # a local journey carries nobody's
    status, made = request(f"{ws}/runs", "POST", json.dumps(asked).encode())
    assert _wait(ws, made["id"])["result"]["data"]["google"] == {"mode": "signin", "token": None}


def test_where_the_credentials_come_from(monkeypatch):
    """The third `GoogleAccess`: one access token, no refresh, nothing stored."""
    from beamer2slides.agent.auth import NoGoogle, TokenFile
    from beamer2slides.playground import runner
    assert isinstance(runner.access({}), NoGoogle)
    assert runner.access({}).describe()["reason"] == "offline"   # a host with no account at all
    assert isinstance(runner.access({"mode": "signin", "token": None}), NoGoogle)
    monkeypatch.delenv("B2S_AGENT_OFFLINE", raising=False)
    assert isinstance(runner.access({"mode": "local"}), TokenFile)
    visitor = runner.access({"mode": "signin", "token": "ya29.x"})
    assert visitor.describe() == {"available": True, "source": "the visitor's own Google sign-in",
                                  "scopes": runner.SCOPES}
    assert "ya29.x" not in json.dumps(visitor.describe())
    assert runner.SCOPES == server.WEB_SCOPES


def test_a_token_the_page_holds_on_to_is_held_in_the_page_and_nowhere_else():
    """Asking Google at every click put a window in somebody's face for nothing - a journey, the
    Picker, then the next journey - so a granted token is kept for the hour it lasts. Kept in the
    page's own memory: it is a bearer token for that person's Drive files, and a store that
    outlives the tab is a copy of it sitting on the machine with nothing to end it. A reload is
    the price, and it is cheap.

    The page also decides whether to send one by the very field the server decides by
    (`effects.google`, which `needs_google` reads), so a journey that needs no account is handed
    nobody's credentials by either side."""
    from beamer2slides.agent import schema, tools
    stores = ("localStorage", "sessionStorage", "document.cookie", "indexedDB")
    granting = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "granted" in granting and not [s for s in stores if s in granting.replace(
        "not in localStorage, not in a cookie", "")]          # the promise is written there too
    bench = (server.STATIC / "workbench.js").read_text(encoding="utf-8")
    assert [line.split('"')[1] for line in bench.splitlines() if "localStorage" in line] \
        == ["b2s-ws"] * 3                                     # the workspace id, and nothing else
    assert "t.effects?.google" in bench
    published = {t["name"]: t for t in schema.all_schemas(tools.TOOLS)}
    assert published["deck_convert"]["effects"]["google"] is True
    assert published["tex_label"]["effects"]["google"] is False
    for name, tool in published.items():
        assert workbench.needs_google(name) is tool["effects"]["google"]
    assert workbench.needs_google(workbench.COMPILE_TOOL["name"]) is False


def test_a_local_journey_on_a_signin_host_does_not_report_a_server_that_cannot_reach_google():
    """`b2s_status` is the tool that answers "is Google reachable?", and on a `signin` host it
    is never handed a token - a local journey carries nobody's credentials. Saying "offline"
    there describes a machine, on a page with a sign-in button on it; what is true is the
    arrangement, which this process can see, and not whether somebody is signed in, which it
    cannot."""
    from beamer2slides.playground import runner
    described = runner.access({"mode": "signin", "token": None}).describe()
    assert described["available"] is False and described["reason"] == "no token in this run"
    assert described["fix"] == runner.SIGN_IN and "sign-in at the top of this page" in runner.SIGN_IN


def test_a_run_that_will_not_end_is_stopped(ws, tmp_path, monkeypatch):
    """A journey's own LaTeX has no time limit of its own; a process can be killed where a
    thread cannot."""
    bench = server.Handler.app.bench
    monkeypatch.setattr(bench, "command", lambda: child(tmp_path, HANGS))
    monkeypatch.setattr(workbench, "RUN_TIMEOUT", 2)
    state = run(ws, "b2s_status", seconds=60)
    assert state["result"]["ok"] is False and "stopped after" in state["result"]["summary"]


def test_a_child_that_dies_says_what_it_said(ws, tmp_path, monkeypatch):
    bench = server.Handler.app.bench
    monkeypatch.setattr(bench, "command", lambda: child(tmp_path, DIES))
    state = run(ws, "b2s_status", seconds=60)
    assert state["result"]["ok"] is False
    assert "exit 3" in state["result"]["summary"]
    assert "a traceback nobody meant to write" in state["result"]["summary"]


def test_a_workspace_is_swept_when_too_many_are_open(base, monkeypatch):
    bench = server.Handler.app.bench
    monkeypatch.setattr(workbench, "KEEP_SESSIONS", 2)
    opened = [bench.open() for _ in range(4)]
    assert len(bench.sessions) == 2 and not opened[0].root.exists() and opened[-1].root.exists()
    for session in opened[-2:]:
        bench.sessions.pop(session.id, None)
