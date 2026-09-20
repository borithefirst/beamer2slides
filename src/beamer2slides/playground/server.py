"""The playground's HTTP server: the standard library's, so it runs wherever beamer2slides does.

API (JSON unless said otherwise):
  GET  /api/config                 what this server can do (TeX engines, Google), examples, gallery
  GET  /api/example/<name>.tex     an example talk's source (.pdf: its compiled PDF, where there is one)
  POST /api/jobs                   {"tex": "..."} or a PDF body (Content-Type: application/pdf) -> {"id"}
  GET  /api/jobs/<id>              state, log, timings and, once done, the slides
  GET  /api/jobs/<id>/source       the talk as it was sent
  GET  /api/jobs/<id>/files/<path> a file the job wrote (deck.json, backgrounds/, debug/, figures/, pages/)
  POST /api/jobs/<id>/slides       convert the job into a Google Slides deck (only with Google enabled;
                                   where visitors sign in, {"access_token": ...} says whose Drive)
  GET  /media/<file>               the front page's pictures (docs/media), for the recorded runs

The workbench (`workbench.py`) is the second half: a folder per visitor and the agent layer's
journeys run inside it, one subprocess each.

  GET  /api/tools                  every journey as JSON Schema, plus the instructions
  POST /api/ws                     open a workspace -> {"id"}
  GET  /api/ws/<sid>               its files, its byte count, its limits and its runs
  GET/PUT/DELETE /api/ws/<sid>/file?path=<ref>    one file (PUT's body is the content)
  POST /api/ws/<sid>/runs          {"tool", "args", "access_token"?} -> {"id"}
  GET  /api/ws/<sid>/runs/<rid>?since=<n>         state, new log lines and, at the end, the Result

Jobs run one at a time on a worker thread (the stages print, and pdflatex is heavy), at most
`MAX_QUEUE` wait, and the last `KEEP_JOBS` stay on disk (job folders of an earlier run on the same
port are swept at start). A talk's TeX is compiled with shell escape off, TeX Live's paranoid file access (no absolute
paths, no `..`) and a time limit: that is what makes it fit for a public host (docs/playground.md).
"""

import contextlib
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import workbench

STATIC = Path(__file__).parent / "static"
# examples and docs/media come from a checkout: this one (src/beamer2slides/playground -> the repo), or another
CHECKOUT = Path(os.environ.get("B2S_PLAYGROUND_ROOT") or Path(__file__).resolve().parents[3])
MEDIA = CHECKOUT / "docs" / "media"
MAX_PAGES = 40
MAX_UPLOAD = 25 * 1024 * 1024
TEX_TIMEOUT = 90                                          # s, per engine run
KEEP_JOBS = 40
MAX_QUEUE = 8
PAGE_PX = 1200
JOB_ID = re.compile(r"[0-9a-f]{12}")
GALLERY = [("hero.png", "The same slide as pdflatex printed it and as Google Slides renders it"),
           ("elements.png", "Every box is a separate Slides object"),
           ("sync.png", "sync: a colleague's edits and a rewritten talk, both kept"),
           ("adopt.png", "adopt: a beamer source written from a deck nobody converted"),
           ("fidelity.png", "Fidelity measured on Google's own renderer"),
           ("stages.gif", "One slide through the four stages")]


def jobs_root() -> Path:
    return Path(os.environ.get("B2S_PLAYGROUND_JOBS") or Path(tempfile.gettempdir()) / "b2s-playground")


def tex_engines() -> list[str]:
    return [e for e in ("pdflatex", "lualatex", "xelatex") if shutil.which(e)]


# What the visitor's browser asks Google for. `drive.file` reaches only the files this app
# itself creates, which is all the deck needs (it is imported as a .pptx and then edited), and
# Google counts it as non-sensitive: a published app asking for it alone needs no review.
WEB_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def google_client_id() -> str:
    """The OAuth *web* client of the host, for visitors signing in. A client id is not a secret."""
    return os.environ.get("B2S_PLAYGROUND_GOOGLE_CLIENT_ID", "").strip()


def google_api_key() -> str:
    """The browser API key the Google Picker needs, or "" where the host set none.

    `drive.file` reaches the files this app created and no others, which is what keeps it a
    non-sensitive scope - and what makes a deck somebody else built unreachable by
    `deck_adopt` or `deck_pull`. The Picker is Google's own answer to exactly that: the visitor
    chooses a file in Google's window, and that choice grants this app `drive.file` access to
    that one file. It runs in the browser and needs a key of its own; an API key is not a
    secret (it identifies the project and is restricted by referrer), so it belongs in the
    deployment's environment beside the client id.
    """
    return os.environ.get("B2S_PLAYGROUND_GOOGLE_API_KEY", "").strip()


def google_mode() -> str | None:
    """Whose Drive a deck would go into, or None where the button does not exist.

    `local`: this machine's own token (`B2S_PLAYGROUND_GOOGLE=1`), for running the playground
    at home. `signin`: the visitor's, by signing in with Google in their browser - the only
    one a public host may use, since it must never convert into its owner's Drive.
    """
    if google_client_id():
        return "signin"
    if os.environ.get("B2S_PLAYGROUND_GOOGLE") != "1":
        return None
    from ..google_auth import credential_file
    return "local" if credential_file("B2S_TOKEN", "token.json").exists() else None


SAMPLES = [("demo", "examples/demo/demo.tex", "The demo talk: blocks, a table, a diagram, math, a figure"),
           ("research-talk", "tests/decks/11_research_talk.tex", "A research talk in the Madrid theme"),
           ("inline-math", "tests/decks/13_inline_math.tex", "Formulas inside sentences: runs and holes"),
           ("blocks", "tests/decks/18_blocks_resize.tex", "Beamer blocks rebuilt as shapes that resize"),
           ("graphics-on-words", "tests/decks/19_labels_on_graphics.tex", "Circled numbers, keycaps, badges in prose")]


def examples() -> dict[str, tuple[Path, str]]:
    """The talks the page offers, those of this checkout that exist (an installed package has none)."""
    return {name: (CHECKOUT / rel, about) for name, rel, about in SAMPLES if (CHECKOUT / rel).is_file()}


class Job:
    def __init__(self, jid: str, folder: Path):
        self.id, self.dir = jid, folder
        self.state, self.error, self.log = "queued", None, ""
        self.timings: dict[str, float] = {}
        self.result: dict | None = None
        self.slides_url: str | None = None

    def view(self) -> dict:
        return {"id": self.id, "state": self.state, "error": self.error, "log": self.log[-6000:],
                "timings": self.timings, "result": self.result, "slides_url": self.slides_url}


class Playground:
    def __init__(self, port: int):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.google_lock = threading.Lock()   # one Google conversion at a time (to_slides)
        self.queue: queue.Queue = queue.Queue()
        # One folder per port: the sweep below must not take the jobs of another server running beside this one.
        self.root = jobs_root() / str(port)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sweep()
        self.bench = workbench.Workbench(self.root)
        self.bench.engines = tex_engines()
        threading.Thread(target=self.worker, daemon=True).start()

    # ---- jobs

    def submit(self, tex: str | None, pdf: bytes | None) -> Job:
        if self.queue.qsize() >= MAX_QUEUE:
            raise Busy(f"{MAX_QUEUE} talks are waiting already: try again in a minute")
        jid = uuid.uuid4().hex[:12]
        job = Job(jid, self.root / jid)
        job.dir.mkdir(parents=True)
        with self.lock:
            self.jobs[jid] = job
            self.prune()
        self.queue.put((job, tex, pdf))
        return job

    def prune(self) -> None:
        """The oldest finished jobs go (the dict keeps the order they came in)."""
        finished = [j for j in self.jobs.values() if j.state in ("done", "error")]
        for job in finished[:max(0, len(self.jobs) - KEEP_JOBS)]:
            shutil.rmtree(job.dir, ignore_errors=True)
            del self.jobs[job.id]

    def sweep(self) -> None:
        """Job folders of an earlier run of the server: nothing can reach them any more."""
        for folder in self.root.iterdir():
            if folder.is_dir() and JOB_ID.fullmatch(folder.name):
                shutil.rmtree(folder, ignore_errors=True)

    def worker(self) -> None:
        while True:
            job, tex, pdf = self.queue.get()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    self.run(job, tex, pdf)
                job.state = "done"
            except JobError as e:
                job.state, job.error = "error", str(e)
            except Exception as e:  # a talk that breaks a stage is a finding, not a dead server
                job.state, job.error = "error", f"{type(e).__name__}: {e}"
                buf.write(traceback.format_exc())
            finally:
                job.log += buf.getvalue()

    def stage(self, job: Job, name: str, t0: float) -> float:
        t = time.perf_counter()
        job.timings[name] = round(t - t0, 2)
        return t

    def run(self, job: Job, tex: str | None, pdf_bytes: bytes | None) -> None:
        from ..__main__ import cmd_classify
        from ..pdf import Document
        from ..render import render_backgrounds
        t = time.perf_counter()
        if tex is not None:
            job.state = "compiling"
            pdf = compile_tex(job, tex)
            t = self.stage(job, "compile", t)
        else:
            pdf = job.dir / "talk.pdf"
            pdf.write_bytes(pdf_bytes or b"")
        doc = Document(pdf)
        try:
            pages = len(doc)
        finally:
            doc.close()
        if pages > MAX_PAGES:
            raise JobError(f"{pages} pages: the playground takes up to {MAX_PAGES}")
        job.state = "classifying"
        out = job.dir / "out"
        pdf, raw, deck = cmd_classify(pdf, out, "last", "warn")
        t = self.stage(job, "extract + classify", t)
        job.state = "rendering"
        render_backgrounds(pdf, raw, deck, out)
        (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
        t = self.stage(job, "render", t)
        page_pngs(pdf, deck, out / "pages")
        job.result = summary(deck, pages)

    def to_slides(self, job: Job, token: str | None = None) -> str:
        """Build the deck. `token`: a visitor's access token, which goes no further than this call.

        `google_auth.use_provider` is process-wide, so one conversion runs at a time: two
        visitors converting at once must never build a deck with the other one's credentials.
        """
        from ..emit import emit
        from ..google_auth import use_provider
        from google.oauth2.credentials import Credentials
        out = job.dir / "out"
        deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
        source = next(job.dir.glob("*.pdf"))
        creds = Credentials(token=token, scopes=WEB_SCOPES) if token else None
        buf = io.StringIO()
        with self.google_lock:
            with contextlib.ExitStack() as stack:
                if creds is not None:
                    stack.enter_context(use_provider(lambda: creds))
                stack.enter_context(contextlib.redirect_stdout(buf))
                state = emit(deck, out, f"beamer2slides playground {job.id}", True, True, False, "none", source)
        job.log += buf.getvalue()
        if token is None:                  # one machine, one Drive: the link belongs to the job
            job.slides_url = state["url"]
        return state["url"]


class JobError(Exception):
    """What went wrong in words the page can show as they are."""


class Busy(Exception):
    """The queue is full."""


def compile_tex(job: Job, tex: str) -> Path:
    """The talk, compiled in its own job folder. The fence is `workbench.run_latex`'s.

    One compile on this server, wherever it was asked for: the workbench compiles the same way,
    and a source from a stranger must not be fenced one way here and another way there.
    """
    engines = tex_engines()
    if not engines:
        raise JobError("no TeX distribution on this server: upload a compiled PDF instead")
    (job.dir / "talk.tex").write_text(tex, encoding="utf-8")
    try:                                     # the second run settles navigation
        return workbench.run_latex(job.dir, "talk.tex", engines, TEX_TIMEOUT, passes=2)
    except workbench.TexError as e:
        raise JobError(str(e)) from None


def page_pngs(pdf: Path, deck: dict, folder: Path) -> None:
    from PIL import Image
    from ..pdf import Document
    folder.mkdir(parents=True, exist_ok=True)
    doc = Document(pdf)
    try:
        for s in deck["slides"]:
            page = doc[s["page"]]
            Image.fromarray(page.render(PAGE_PX / page.width)).convert("RGB").save(
                folder / f"page-{s['page'] + 1:03}.png")
    finally:
        doc.close()


def run_text(runs: list[dict]) -> str:
    return "".join(r.get("text", "") for r in runs)


def label_of(el: dict) -> str:
    """A few words saying what an element is, for the page's tooltips."""
    if el["kind"] == "text":
        return " / ".join(run_text(p["runs"]) for p in el.get("paragraphs", []))[:140]
    if el["kind"] == "table":
        return f"{len(el['cells'])} rows × {len(el['columns'])} columns"
    if el["kind"] == "diagram":
        return f"{len(el['nodes'])} nodes, {len(el['lines'])} lines: " + \
            ", ".join(run_text(p) for n in el["nodes"] for p in n.get("paragraphs", []))[:100]
    if el["kind"] == "shape":
        return f"{el.get('shape', '').lower()} {el.get('fill') or ''}"
    return el.get("role") or ""


def summary(deck: dict, pages: int) -> dict:
    slides = []
    for s in deck["slides"]:
        n = f"{s['page'] + 1:03}"
        slides.append({
            "page": s["page"], "frame": s.get("frame"), "size": s["size"],
            "files": {"page": f"pages/page-{n}.png", "debug": f"debug/slide-{n}.png",
                      "background": s.get("background")},
            "elements": [{"id": e["id"], "kind": e["kind"], "role": e.get("role"), "bbox": e["bbox"],
                          "label": label_of(e)} for e in s["elements"]],
            "left": [{"reason": l["reason"], "spans": len(l["spans"])} for l in s.get("left_in_background", [])],
            "notes": s.get("notes")})
    return {"pages": pages, "stats": deck.get("stats", {}), "slides": slides}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    app: Playground
    server_version = "beamer2slides-playground"

    def log_message(self, fmt, *args):
        # stderr: a job's stages print while the worker holds stdout (redirect_stdout is process-wide)
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def send(self, body: bytes, ctype: str, status: int = 200, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status: int = 200) -> None:
        self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json", status)

    def file(self, base: Path, rel: str, cache: bool = False) -> None:
        path = (base / rel).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            return self.json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send(path.read_bytes(), ctype, cache=cache)

    # ---- the workbench: a folder per visitor and the journeys run in it (workbench.py)

    def query(self) -> dict:
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def bench(self, method: str, path: str, body: bytes) -> None:
        """Everything the workbench answers. A `Denied` carries the status; nothing else escapes."""
        try:
            return self.bench_route(method, path, body)
        except workbench.Denied as e:
            return self.json({"error": str(e)}, e.status)

    def bench_route(self, method: str, path: str, body: bytes) -> None:
        bench = self.app.bench
        if path == "/api/tools" and method == "GET":
            return self.json(workbench.catalogue())
        if path == "/api/ws" and method == "POST":
            return self.json({"id": bench.open().id}, 201)
        m = re.fullmatch(r"/api/ws/([0-9a-f]{12})(?:/(file|runs)(?:/(\w+))?)?", path)
        if not m:
            return self.json({"error": "not found"}, 404)
        session = bench.get(m.group(1))
        part, rest = m.group(2), m.group(3)
        if part is None and method == "GET":
            return self.json(session.view())
        if part == "file":
            ref = self.query().get("path", "")
            if method == "GET":
                here = bench.resolve(session, ref)
                if not here.is_file():
                    return self.json({"error": "no such file"}, 404)
                ctype = mimetypes.guess_type(here.name)[0] or "application/octet-stream"
                return self.send(here.read_bytes(), ctype)
            if method == "PUT":
                return self.json(bench.write(session, ref, body))
            if method == "DELETE":
                bench.remove(session, ref)
                return self.json({"deleted": ref})
        if part == "runs":
            if method == "POST" and not rest:
                return self.json({"id": self.start_run(session, body).id}, 201)
            if method == "GET" and rest:
                run = session.runs.get(rest)
                if run is None:
                    return self.json({"error": "no such run"}, 404)
                return self.json(run.view(int(self.query().get("since") or 0)))
        return self.json({"error": "not found"}, 404)

    def start_run(self, session, body: bytes):
        """One journey, asked for by name. The token, where there is one, goes no further than
        the child process it is handed to on stdin."""
        try:
            asked = json.loads(body or b"{}")
            tool, args = asked.get("tool"), asked.get("args") or {}
        except (ValueError, AttributeError):
            tool, args = None, None
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise workbench.Denied('send {"tool": ..., "args": {...}}')
        mode, token = google_mode(), asked.get("access_token")
        if not workbench.needs_google(tool):
            token = None                        # a local journey carries nobody's credentials
        elif mode == "signin" and not isinstance(token, str):
            raise workbench.Denied("sign in with Google first", 401)
        return self.app.bench.start(session, tool, args, mode=mode, token=token)

    # ---- routes

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/tools" or path == "/api/ws" or path.startswith("/api/ws/"):
            return self.bench("GET", path, b"")
        if path == "/":
            return self.file(STATIC, "index.html")
        # A published OAuth app must show the visitor a privacy policy, on its own domain.
        if path == "/privacy":
            return self.file(STATIC, "privacy.html")
        if path.startswith("/static/"):
            return self.file(STATIC, path[len("/static/"):])
        if path.startswith("/media/"):
            return self.file(MEDIA, path[len("/media/"):], cache=True)
        if path == "/api/config":
            return self.json({"engines": tex_engines(), "google": google_mode(),
                              "google_client_id": google_client_id(), "google_scopes": " ".join(WEB_SCOPES),
                              "google_api_key": google_api_key(), "max_pages": MAX_PAGES,
                              "examples": [{"name": n, "about": a, "pdf": t.with_suffix(".pdf").is_file()}
                                           for n, (t, a) in examples().items()],
                              "gallery": [{"file": f, "caption": c} for f, c in GALLERY if (MEDIA / f).exists()]})
        m = re.fullmatch(r"/api/example/([\w-]+)\.(tex|pdf)", path)
        if m:
            tex = examples().get(m.group(1), (None,))[0]
            wanted = tex.with_suffix("." + m.group(2)) if tex else None
            if wanted is None or not wanted.is_file():
                return self.json({"error": "no such example"}, 404)
            return self.send(wanted.read_bytes(), "text/plain; charset=utf-8" if m.group(2) == "tex" else "application/pdf")
        m = re.fullmatch(r"/api/jobs/(\w+)(?:/files/(.+)|/(source))?", path)
        if m:
            job = self.app.jobs.get(m.group(1))
            if job is None:
                return self.json({"error": "no such job"}, 404)
            if m.group(3):
                return self.file(job.dir, "talk.tex")
            if m.group(2):
                return self.file(job.dir / "out", m.group(2), cache=True)
            return self.json(job.view())
        self.json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.json({"error": f"over {MAX_UPLOAD // 2**20} MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = self.rfile.read(length)
        if path == "/api/ws" or path.startswith("/api/ws/"):
            return self.bench("POST", path, body)
        if path == "/api/jobs":
            tex = pdf = None
            if (self.headers.get("Content-Type") or "").startswith("application/pdf"):
                if not body.startswith(b"%PDF"):
                    return self.json({"error": "that is not a PDF"}, 400)
                pdf = body
            else:
                try:
                    tex = json.loads(body or b"{}").get("tex")
                except (ValueError, AttributeError):
                    pass
                if not isinstance(tex, str) or not tex.strip():
                    return self.json({"error": "send {\"tex\": ...} or a PDF"}, 400)
            try:
                return self.json({"id": self.app.submit(tex, pdf).id}, 201)
            except Busy as e:
                return self.json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
        m = re.fullmatch(r"/api/jobs/(\w+)/slides", path)
        if m:
            job = self.app.jobs.get(m.group(1))
            if job is None or job.state != "done":
                return self.json({"error": "no finished job by that id"}, 404)
            mode = google_mode()
            if mode is None:
                return self.json({"error": "this server does not convert into Google Slides"}, 403)
            token = None
            if mode == "signin":
                try:
                    token = json.loads(body or b"{}").get("access_token")
                except ValueError:
                    token = None
                if not isinstance(token, str) or not token:
                    return self.json({"error": "sign in with Google first"}, 401)
            try:
                return self.json({"url": job.slides_url or self.app.to_slides(job, token)})
            except Exception as e:
                # Never echo the token back, whatever the library put in the message.
                message = f"{type(e).__name__}: {e}"
                status = 401 if token and ("invalid_grant" in message or "UNAUTHENTICATED" in message
                                           or "Invalid Credentials" in message) else 500
                return self.json({"error": message.replace(token, "…") if token else message,
                                  "signin": status == 401}, status)
        self.json({"error": "not found"}, 404)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.json({"error": f"over {MAX_UPLOAD // 2**20} MB"},
                             HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.bench("PUT", self.path.split("?")[0], self.rfile.read(length))

    def do_DELETE(self):
        self.bench("DELETE", self.path.split("?")[0], b"")


def serve(host: str = "127.0.0.1", port: int = 7860) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)     # first: a port in use fails before any sweep
    Handler.app = Playground(port)
    print(f"beamer2slides playground on http://{host}:{port}/  (TeX: {', '.join(tex_engines()) or 'none'};"
          f" Google Slides: {google_mode() or 'off'}; jobs in {Handler.app.root})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
