"""The playground's server, offline: a job on a built test PDF through the HTTP API, and the requests
it must refuse. No TeX needed (the PDF is uploaded); no Google (the server is not given it)."""
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from beamer2slides.json_types import JsonObject, as_object
from beamer2slides.playground import server

from .json_reads import at, obj, objs, text

HERE = Path(__file__).parent
PDF = HERE / "decks" / "out" / "04_theme_blocks.pdf"


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    jobs = tmp_path_factory.mktemp("jobs")
    mp = pytest.MonkeyPatch()
    mp.setenv("B2S_PLAYGROUND_JOBS", str(jobs))
    mp.delenv("B2S_PLAYGROUND_GOOGLE", raising=False)
    mp.delenv("B2S_PLAYGROUND_GOOGLE_CLIENT_ID", raising=False)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    stale = jobs / str(port) / "0123456789ab"      # a job folder left by an earlier run on this port
    keep = jobs / str(port) / "not-a-job"          # anything else there is not the server's
    other = jobs / str(port + 1) / "0123456789ab"  # a server running beside it
    for folder in (stale, keep, other):
        folder.mkdir(parents=True)
    server.Handler.app = server.Playground(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    assert not stale.exists() and keep.exists() and other.exists()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    mp.undo()


JSON = "application/json"


def fetch(url: str, data: bytes | None, ctype: str) -> tuple[int, bytes]:
    """(status, body) of a GET (`data` None) or a POST of `data` as `ctype`; an HTTP error is an answer."""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call(url: str, data: bytes | None, ctype: str) -> tuple[int, JsonObject]:
    """`fetch`, for the API's answers: every one of them, refusals included, is a JSON object."""
    status, body = fetch(url, data, ctype)
    return status, as_object(json.loads(body), url)


def finished(base: str, jid: str) -> JsonObject:
    for _ in range(300):
        status, job = call(f"{base}/api/jobs/{jid}", None, JSON)
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("the job never finished")


def test_config_says_what_the_server_can_do(base: str):
    status, config = call(f"{base}/api/config", None, JSON)
    assert status == 200 and config["google"] is None      # no token, no client: no deck button
    assert "demo" in [text(e, "name") for e in objs(config, "examples")]
    status, tex = fetch(f"{base}/api/example/demo.tex", None, JSON)
    assert status == 200 and b"\\begin{document}" in tex
    assert fetch(f"{base}/api/example/nothing.tex", None, JSON)[0] == 404


@pytest.mark.skipif(not PDF.exists(), reason="no test PDFs built")
def test_a_host_with_no_token_of_its_own_asks_the_visitor_to_sign_in(base: str, monkeypatch: pytest.MonkeyPatch):
    """A public host converts into the visitor's Drive, never its own: no token, no deck."""
    monkeypatch.setenv("B2S_PLAYGROUND_GOOGLE_CLIENT_ID", "1234.apps.googleusercontent.com")
    status, config = call(f"{base}/api/config", None, JSON)
    assert config["google"] == "signin" and config["google_client_id"] == "1234.apps.googleusercontent.com"
    # drive.file alone reaches the files the deck is made of, and needs no Google review.
    assert config["google_scopes"] == "https://www.googleapis.com/auth/drive.file"
    status, made = call(f"{base}/api/jobs", PDF.read_bytes(), "application/pdf")
    jid = text(made, "id")
    job = finished(base, jid)
    assert job["state"] == "done", job["error"]
    status, answer = call(f"{base}/api/jobs/{jid}/slides", b"{}", JSON)
    assert status == 401 and "sign in" in text(answer, "error")


@pytest.mark.skipif(not PDF.exists(), reason="no test PDFs built")
def test_an_uploaded_pdf_goes_through_every_stage(base: str):
    status, made = call(f"{base}/api/jobs", PDF.read_bytes(), "application/pdf")
    assert status == 201
    jid = text(made, "id")
    job = finished(base, jid)
    assert job["state"] == "done", job["error"]
    assert set(obj(job, "timings")) == {"extract + classify", "render"}      # nothing to compile
    slides = objs(job, "result", "slides")
    assert slides and all(s["elements"] for s in slides)
    kinds = {e["kind"] for s in slides for e in objs(s, "elements")}
    assert {"text", "shape"} <= kinds                                    # the blocks are panels
    first = slides[0]
    for rel in (text(first, "files", "page"), text(first, "files", "debug"),
                text(first, "files", "background"), "deck.json"):
        status, body = fetch(f"{base}/api/jobs/{jid}/files/{rel}", None, JSON)
        assert status == 200 and body, rel
    status, deck = call(f"{base}/api/jobs/{jid}/files/deck.json", None, JSON)
    assert [s["background"] for s in objs(deck, "slides")] == [at(s, "files", "background") for s in slides]
    # the page's own pictures: every one the IR names is there
    for s in objs(deck, "slides"):
        for e in objs(s, "elements"):
            if e.get("file"):
                assert fetch(f"{base}/api/jobs/{jid}/files/{text(e, 'file')}", None, JSON)[0] == 200
    # no Google on this server: converting is refused, not attempted
    assert call(f"{base}/api/jobs/{jid}/slides", b"", JSON)[0] == 403


def test_the_privacy_policy_is_served(base: str):
    """A published OAuth app must link one, and the link must be on the playground's own domain."""
    status, page = fetch(f"{base}/privacy", None, JSON)
    assert status == 200 and b"auth/drive.file" in page


def test_what_it_refuses(base: str):
    assert call(f"{base}/api/jobs", b"not a pdf", "application/pdf")[0] == 400
    assert call(f"{base}/api/jobs", b"{not json", JSON)[0] == 400
    assert call(f"{base}/api/jobs", json.dumps({"tex": ["a list"]}).encode(), JSON)[0] == 400
    assert call(f"{base}/api/jobs/ffffffffffff", None, JSON)[0] == 404
    status, made = call(f"{base}/api/jobs", b"%PDF-1.4 truncated", "application/pdf")
    jid = text(made, "id")
    job = finished(base, jid)                           # a broken PDF is an error, not a dead worker
    assert job["state"] == "error" and job["error"]
    # nothing outside the job's own folders, however the path is spelled
    for rel in ("../talk.pdf", "..%2Ftalk.pdf", "../../../pyproject.toml"):
        assert call(f"{base}/api/jobs/{jid}/files/{rel}", None, JSON)[0] == 404, rel
    assert call(f"{base}/static/../server.py", None, JSON)[0] == 404
    assert call(f"{base}/api/jobs/{jid}/source", None, JSON)[0] == 404       # it was a PDF


def test_a_talk_is_refused_without_tex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(server, "tex_engines", lambda: [])
    with pytest.raises(server.JobError, match="upload a compiled PDF"):
        server.compile_tex(server.Job("0" * 12, tmp_path), "\\documentclass{beamer}")


def test_the_queue_has_a_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("B2S_PLAYGROUND_JOBS", str(tmp_path))
    app = server.Playground.__new__(server.Playground)     # no worker: nothing leaves the queue
    app.jobs = {}
    app.lock, app.queue, app.root = threading.Lock(), server.queue.Queue(), tmp_path
    for _ in range(server.MAX_QUEUE):
        app.submit(None, b"%PDF")
    with pytest.raises(server.Busy):
        app.submit(None, b"%PDF")


def test_finished_jobs_are_pruned_oldest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app = server.Playground.__new__(server.Playground)
    app.jobs = {}
    app.lock, app.queue, app.root = threading.Lock(), server.queue.Queue(), tmp_path
    monkeypatch.setattr(server, "KEEP_JOBS", 2)
    made = []
    for n in range(4):
        job = app.submit(None, b"%PDF")
        job.state = "done" if n != 1 else "classifying"   # a job at work is never pruned
        made.append(job)
    app.prune()
    assert list(app.jobs) == [made[1].id, made[3].id]
    assert not made[0].dir.exists() and made[3].dir.exists()
