"""The workbench: a folder per visitor, and the eleven journeys run inside it.

The playground's first tab is one road - a talk in, a deck out - and everything else this
library does (sync, pull, adopt, the whole Google Docs side) is a road it does not have. The
agent layer already answers exactly that question: eleven journeys, one `Result` shape, a
`Workspace` that is the only folder a journey may write into, and a gate that refuses *before*
a body runs. So the workbench is that layer with a filesystem the visitor can see and a form
per tool. There is no shell and no arbitrary code on purpose: a hosted playground runs
strangers' input, and the only two things it executes are a TeX engine (fenced) and its own
journeys.

A journey runs in a **subprocess**, for three reasons. `@tool` serialises one journey per
process, so two visitors would otherwise wait on each other's conversion. The library's own
LaTeX compiles (`inverse.Compiler`, which `pull` and `converge` loop over) have no time limit
of their own, and a process can be killed where a thread cannot. And a journey that dies takes
nothing of the server with it. The job - the tool, its arguments, the workspace and, where
there is one, the visitor's access token - goes in on **stdin**, so no token is ever in a
command line; progress lines and the result come back as JSON lines (`runner.py`).

What the visitor may do to the folder is bounded by counts and bytes rather than by trust:
`resolve` is the agent layer's own `LocalWorkspace.resolve`, so a path that climbs out is
refused by the same code a journey's would be.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..agent.types import Refused
from ..agent.workspace import LocalWorkspace

SID = re.compile(r"[0-9a-f]{12}")
RUN_ID = re.compile(r"[0-9a-f]{8}")

MAX_FILES = 3000              # a 40-page conversion leaves backgrounds, debug and figures
MAX_BYTES = 80 * 1024 * 1024  # the whole workspace
MAX_FILE = 25 * 1024 * 1024   # one upload, the same as a job's
MAX_LIST = 1200               # entries the listing shows before it says there are more
KEEP_VERSIONS = 16            # copies of what the editor was handed, kept to merge a save against
MERGE_BYTES = 512 * 1024      # the biggest file a stale save is merged rather than refused
KEEP_SESSIONS = 12
KEEP_RUNS = 30
LOG_LINES = 800
RUN_SLOTS = 1                 # journeys at a time on this server; the rest queue
RUN_TIMEOUT = int(os.environ.get("B2S_WORKBENCH_TIMEOUT") or 420)
TEX_TIMEOUT = int(os.environ.get("B2S_WORKBENCH_TEX_TIMEOUT") or 90)

#: TeX Live's paranoid mode plus shell escape off, for everything a run starts - the playground's
#: own compile sets these, and so must the library's (`inverse.Compiler` passes neither).
FENCE = {"openin_any": "p", "openout_any": "p", "shell_escape": "f"}

COMPILE_TOOL = {
    "name": "tex_compile",
    "description": "Compile a LaTeX source in the workspace with the server's TeX engine and "
                   "leave the PDF beside it.\n\nNot one of the journeys: it is how a source "
                   "becomes the PDF the deck journeys start from. pdflatex, or lualatex when "
                   "the source loads fontspec. Shell escape is off and file access is TeX "
                   "Live's paranoid mode, so the run cannot reach outside the workspace.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tex": {"type": "string",
                    "description": "The .tex file to compile, relative to the workspace."},
            "passes": {"type": "integer", "default": 3,
                       "description": "At most how many times to run the engine. It stops as "
                                      "soon as the auxiliary files stop moving, so a source "
                                      "compiled again in the same folder costs one pass and a "
                                      "fresh one costs the two beamer needs to settle its "
                                      "navigation and labels."},
        },
        "required": ["tex"],
        "additionalProperties": False,
    },
    "needs": ["reads", "writes"],
    "effects": {"reads_local": True, "reads_google": False, "writes_local": True,
                "writes_google": False, "google": False, "writes": True,
                "approval": "recommended"},
}


class Denied(Exception):
    """Something the workbench refuses, carrying the status the page should be told."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class TexError(Exception):
    """A TeX run that did not produce a PDF, in words the page can show as they are."""


# ---------------------------------------------------------------- TeX

def tex_engine(source: str, engines: list[str]) -> str:
    return "lualatex" if re.search(r"\\usepackage(\[[^]]*\])?\{fontspec\}", source) \
        and "lualatex" in engines else engines[0]


def run_latex(folder: Path, main: str, engines: list[str], timeout: int = TEX_TIMEOUT,
              passes: int = 3) -> Path:
    """Run a TeX engine on `main` inside `folder` and return the PDF it wrote.

    The fence is the same wherever a compile happens on this server: no shell escape, no file
    outside the folder, and a time limit - which is what makes a source from a stranger safe
    to compile at all (docs/playground.md).

    Up to `passes` runs, and **one more only while the auxiliary files are still moving**
    (`inverse.aux_state`, latexmk's rule, which is what the pull loop's own compile goes by).
    This is the step an agent pays on every turn of edit -> compile -> `deck_sync`, where the
    folder's .aux and .nav are settled before the turn begins and a second pass draws the same
    PDF for nothing.
    """
    from ..inverse import aux_state

    path = folder / main
    if not path.is_file():
        raise TexError(f"there is no {main} in the workspace")
    engine = tex_engine(path.read_text(encoding="utf-8", errors="replace"), engines)
    env = {**os.environ, **FENCE}
    before = aux_state(folder)
    for _ in range(max(1, passes)):
        try:
            r = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error",
                                "-no-shell-escape", path.name],
                               cwd=path.parent, capture_output=True, text=True, errors="replace",
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise TexError(f"{engine} took longer than {timeout} s") from None
        if r.returncode:
            errors = [l for l in r.stdout.splitlines() if l.startswith("!") or l.startswith("l.")]
            raise TexError(f"{engine} failed:\n" + ("\n".join(errors[:12]) or r.stdout[-1500:]))
        before, after = aux_state(folder), before
        if before == after:
            break
    pdf = path.with_suffix(".pdf")
    if not pdf.is_file():
        raise TexError(f"{engine} wrote no {pdf.name}")
    return pdf


# ---------------------------------------------------------------- what a session holds

class Run:
    """One journey in flight, and what the page polls for."""

    def __init__(self, rid: str, tool: str, args: dict) -> None:
        self.id, self.tool, self.args = rid, tool, args
        self.state = "queued"                 # queued | running | done
        self.log: list[str] = []
        self.result: dict | None = None
        self.started = time.time()
        self.seconds = 0.0

    def say(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-LOG_LINES]

    def view(self, since: int = 0) -> dict:
        return {"id": self.id, "tool": self.tool, "state": self.state, "result": self.result,
                "seconds": round(self.seconds, 2), "lines": len(self.log),
                "log": self.log[max(0, since):]}


class Session:
    """A visitor's workspace: a folder, the runs made in it, and when it was last touched."""

    def __init__(self, sid: str, root: Path) -> None:
        self.id, self.root = sid, root
        self.runs: dict[str, Run] = {}
        #: What this server handed the editor, by file and stamp: the *base* a later save is
        #: merged against. The last `KEEP_VERSIONS` of them, and only of files small enough
        #: to merge - a workspace holds 80 MB and this is memory, not disk.
        self.seen: dict[str, bytes] = {}
        self.created = self.touched = time.time()

    @property
    def busy(self) -> bool:
        return any(r.state != "done" for r in self.runs.values())

    def remember(self, ref: str, data: bytes) -> str:
        """Keep what a file said as it went to the editor, and give back its stamp."""
        stamp = digest(data)
        if len(data) <= MERGE_BYTES:
            self.seen.pop(f"{ref}\0{stamp}", None)          # to the end: the newest stay
            self.seen[f"{ref}\0{stamp}"] = data
            for old in list(self.seen)[:max(0, len(self.seen) - KEEP_VERSIONS)]:
                del self.seen[old]
        return stamp

    def view(self) -> dict:
        files, total = listing(self.root)
        return {"id": self.id, "files": files, "bytes": total,
                "limits": {"files": MAX_FILES, "bytes": MAX_BYTES, "file_bytes": MAX_FILE,
                           "seconds": RUN_TIMEOUT},
                "runs": [{"id": r.id, "tool": r.tool, "state": r.state,
                          "ok": None if r.result is None else r.result.get("ok")}
                         for r in self.runs.values()]}


def listing(root: Path) -> tuple[list[dict], int]:
    """Every file under the workspace, with its size, and the bytes they come to together.

    The count is of everything; the list stops at `MAX_LIST`, because a conversion of forty
    pages writes more rows than a person can read and the page says so rather than sending them.
    """
    rows: list[dict] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            if len(rows) < MAX_LIST:
                rows.append({"path": rel, "dir": True, "bytes": 0})
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        if len(rows) < MAX_LIST:
            rows.append({"path": rel, "dir": False, "bytes": size})
    return rows, total


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def version(path: Path) -> str:
    """What a file says, in sixteen characters: the stamp an editor holds while it types.

    The content and not the mtime: a journey that writes a file back byte for byte has
    taken nothing away, and two writes inside one clock tick are still two files. A file
    that is not there is the empty stamp, so a page that opened nothing and a page whose
    file a run has since created are told apart as well.
    """
    try:
        return digest(path.read_bytes())
    except OSError:
        return ""


def _short(text: str, room: int = 90) -> str:
    """A piece of somebody's file, small enough to stand in a sentence."""
    said = " ".join(text.split())
    return repr(said if len(said) <= room else said[:room - 1] + "…")


def _around(whole: str, piece: str, room: int = 90) -> str:
    """A piece one side changed, in the line it stands in.

    The merge is word-level, so a clash can be one word long - and one word is not a place
    anybody can find again. The line it sits in is.
    """
    at = whole.find(piece) if piece else -1
    if at < 0:
        return _short(piece or "(nothing)", room)
    start = whole.rfind("\n", 0, at) + 1
    end = whole.find("\n", at + len(piece))
    return _short(whole[start:end if end >= 0 else len(whole)], room)


def usage(root: Path) -> tuple[int, int]:
    files = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            files += 1
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return files, total


# ---------------------------------------------------------------- the workbench

class Workbench:
    def __init__(self, root: Path) -> None:
        self.root = root / "ws"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(RUN_SLOTS)
        self.engines: list[str] = []          # filled in by the server, which knows the machine
        self.sweep()

    def sweep(self) -> None:
        """Workspaces of an earlier run of the server: nothing can reach them any more."""
        for folder in self.root.iterdir():
            if folder.is_dir() and SID.fullmatch(folder.name):
                shutil.rmtree(folder, ignore_errors=True)

    # -- sessions ---------------------------------------------------------------------

    def open(self) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(sid, self.root / sid)
        session.root.mkdir(parents=True)
        seed(session.root)
        with self.lock:
            self.sessions[sid] = session
            self.prune()
        return session

    def get(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None:
            raise Denied("no such workspace: this server may have swept it. Open a new one.", 404)
        session.touched = time.time()
        return session

    def prune(self) -> None:
        """The workspaces nobody has touched for longest go, and never one with a run in it."""
        idle = sorted((s for s in self.sessions.values() if not s.busy), key=lambda s: s.touched)
        for session in idle[:max(0, len(self.sessions) - KEEP_SESSIONS)]:
            shutil.rmtree(session.root, ignore_errors=True)
            del self.sessions[session.id]

    # -- files ------------------------------------------------------------------------

    def resolve(self, session: Session, ref: str, *, write: bool = False) -> Path:
        """The agent layer's own boundary: a path that climbs out is refused there, not here."""
        if not ref or ref.strip() in (".", ""):
            raise Denied("name a file")
        try:
            return LocalWorkspace(session.root).resolve(ref, write=write)
        except Refused as exc:
            raise Denied(str(exc), 403) from None

    def read(self, session: Session, ref: str) -> tuple[Path, bytes, str]:
        """One file, and the stamp the editor is to hold while it types - remembered here,
        because what this server handed out is the base a later save is merged against."""
        path = self.resolve(session, ref)
        if not path.is_file():
            raise Denied("no such file", 404)
        data = path.read_bytes()
        return path, data, session.remember(ref, data)

    def write(self, session: Session, ref: str, data: bytes,
              expected: str | None = None) -> dict:
        path = self.resolve(session, ref, write=True)
        if len(data) > MAX_FILE:
            raise Denied(f"over {MAX_FILE // 2**20} MB", 413)
        merged = False
        if expected is not None and expected != version(path):
            # The editor is holding a buffer of an older file. A journey rewrites the files
            # it is pointed at - `doc_sync` regenerates the canonical HTML from the document
            # it has just written - and saving the older text back is not an edit but a
            # revert, which the *next* sync reads as the source dropping whatever the
            # rewrite brought in. So the two are merged, or nothing is written.
            if not expected:
                raise Denied("a file of that name is already here: open it instead", 409)
            data, merged = self.reconcile(session, ref, path, data, expected)
        had = path.stat().st_size if path.is_file() else 0
        files, total = usage(session.root)
        if total - had + len(data) > MAX_BYTES:
            raise Denied(f"the workspace holds {MAX_BYTES // 2**20} MB at most; "
                         f"delete something first", 413)
        if not path.is_file() and files >= MAX_FILES:
            raise Denied(f"the workspace holds {MAX_FILES} files at most", 413)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        session.remember(ref, data)
        return {"path": LocalWorkspace(session.root).ref(path), "bytes": len(data),
                "version": digest(data), "merged": merged}

    def reconcile(self, session: Session, ref: str, path: Path, data: bytes,
                  expected: str) -> tuple[bytes, bool]:
        """A buffer somebody typed against the file a run has since rewritten: merged.

        Here, and only here, a textual merge is the right one. The Docs side cannot have it -
        a paragraph in a document has to be recognised again after the reader reworded and
        dragged it, which is what the named ranges are for, and `files.update` would destroy
        them - so `doc_merge` merges blocks and writes requests. Between the editor and the
        journey the two sides are the same file in the same format, and there is a real base:
        the bytes this server handed the editor, which `expected` names. That is the whole of
        what a three-way merge needs.

        Word-level (`merge.diff3`, the sync's own), so an edit at the start of a line and a
        rewrite at the end of it both land. What both sides changed is **refused**: nothing is
        written and the clash is quoted, because a conflict marker left in a canonical HTML
        file is not a marker to the next sync, it is content - words outside every block,
        which `doc_ir` now stops a sync over. A person settles it by copying their version
        out and editing the file the run wrote.
        """
        from ..merge import diff3          # the pipeline, imported when it is first needed

        stale = (f"{ref} has changed on the server since it was opened - a run rewrote it. "
                 f"Nothing was saved: reload it, and make the edit again on what it says now")
        was = session.seen.get(f"{ref}\0{expected}")
        if was is None:                    # too big to keep, or too long ago
            raise Denied(stale, 409)
        try:
            base, mine, now = (b.decode("utf-8") for b in (was, data, path.read_bytes()))
        except (UnicodeDecodeError, OSError):
            raise Denied(stale, 409) from None
        if max(len(base), len(mine), len(now)) > MERGE_BYTES:
            raise Denied(stale, 409)
        # `ours` is the buffer and `theirs` the file the run wrote, so a clash resolves the
        # way the whole project resolves one - to the other side. It never reaches the file.
        text, clashes = diff3(base, mine, now)
        if clashes:
            first = clashes[0]
            raise Denied(
                f"{ref} was changed here and by a run since it was opened, in "
                f"{len(clashes)} place(s) that overlap, so nothing was saved. Yours says "
                f"{_around(mine, first['ours'])}, the file now says "
                f"{_around(now, first['theirs'])}. Copy your version out, reload the file, "
                f"and make the edit again on what it says now", 409)
        return text.encode("utf-8"), True

    def remove(self, session: Session, ref: str) -> None:
        path = self.resolve(session, ref, write=True)
        if path == session.root:
            raise Denied("the workspace itself stays", 403)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink()
        else:
            raise Denied("no such file", 404)

    # -- runs -------------------------------------------------------------------------

    def start(self, session: Session, tool: str, args: dict, *, mode: str | None = None,
              token: str | None = None) -> Run:
        if sum(r.state != "done" for r in session.runs.values()) >= 2:
            raise Denied("this workspace already has a run waiting", 429)
        run = Run(uuid.uuid4().hex[:8], tool, args)
        session.runs[run.id] = run
        for old in list(session.runs.values())[:max(0, len(session.runs) - KEEP_RUNS)]:
            if old.state == "done":
                del session.runs[old.id]
        threading.Thread(target=self._drive, args=(session, run, mode, token), daemon=True).start()
        return run

    def _drive(self, session: Session, run: Run, mode: str | None, token: str | None) -> None:
        with self.slots:                      # one journey at a time on this server
            started = time.time()
            run.state = "running"
            try:
                if run.tool == COMPILE_TOOL["name"]:
                    run.result = self._compile(session, run)
                else:
                    run.result = self._journey(session, run, mode, token)
            except Exception as exc:          # a run that breaks is a finding, not a dead server
                run.result = failed(run.tool, f"{type(exc).__name__}: {exc}")
            run.seconds = time.time() - started
            run.result.setdefault("seconds", round(run.seconds, 2))
            run.state = "done"
            session.touched = time.time()

    def _compile(self, session: Session, run: Run) -> dict:
        if not self.engines:
            return failed(run.tool, "this server has no TeX distribution: upload a compiled PDF "
                                    "instead", code="refused")
        ref = str(run.args.get("tex") or "")
        self.resolve(session, ref)            # the boundary, before anything runs
        try:
            pdf = run_latex(session.root, ref, self.engines,
                            passes=int(run.args.get("passes") or 3))
        except TexError as exc:
            return failed(run.tool, str(exc), code="compile_failed")
        name = LocalWorkspace(session.root).ref(pdf)
        return {"tool": run.tool, "ok": True, "code": None,
                "summary": f"{ref} compiled; {name} is {pdf.stat().st_size // 1024} kB.",
                "data": {"pdf": name}, "diagnostics": [], "next_steps": [
                    f"deck_inspect with pdf={name} to see what it would convert to",
                    f"deck_convert with pdf={name} to build the deck"],
                "artifacts": [{"ref": name, "kind": "pdf", "description": "the compiled talk"}]}

    def command(self) -> list[str]:
        """How a journey's process is started. A seam: the tests put a stand-in child here."""
        return [sys.executable, "-u", "-m", "beamer2slides.playground.runner"]

    def _journey(self, session: Session, run: Run, mode: str | None, token: str | None) -> dict:
        job = {"tool": run.tool, "args": run.args, "root": str(session.root),
               "google": {"mode": mode, "token": token}}
        proc = subprocess.Popen(
            self.command(),
            cwd=session.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **FENCE}, **_own_group())
        stopped: list[str] = []
        noise: list[str] = []
        watchdog = threading.Timer(RUN_TIMEOUT, lambda: (stopped.append("timeout"), kill(proc)))
        watchdog.start()
        drain = threading.Thread(target=lambda: noise.append(proc.stderr.read() or ""), daemon=True)
        drain.start()
        result: dict | None = None
        try:
            proc.stdin.write(json.dumps(job))
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    run.say(line)
                    continue
                if "progress" in message:
                    run.say(str(message["progress"]))
                elif "result" in message:
                    result = message["result"]
            proc.wait()
        finally:
            watchdog.cancel()
            drain.join(timeout=2)
        if result is not None:
            return result
        if stopped:
            return failed(run.tool, f"the run was stopped after {RUN_TIMEOUT} s. A journey that "
                                    f"takes this long on a shared server is one to run at home.")
        said = ("".join(noise)).strip()[-1500:]
        return failed(run.tool, f"the run died without an answer (exit {proc.returncode})"
                                + (f":\n{said}" if said else ""))


def failed(tool: str, summary: str, code: str = "failed") -> dict:
    """A refusal in the shape every tool answers in, so the page renders it like any other."""
    return {"tool": tool, "ok": False, "code": code, "summary": summary, "data": {},
            "artifacts": [], "diagnostics": [], "next_steps": [], "seconds": 0.0}


def _own_group() -> dict:
    """Start the child in a group of its own, so killing it kills the TeX run under it."""
    return {} if os.name == "nt" else {"start_new_session": True}


def kill(proc: subprocess.Popen) -> None:
    """Take down the child *and what it started*: a journey's LaTeX run is not its last breath."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            os.killpg(proc.pid, 9)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


# ---------------------------------------------------------------- what a new workspace holds

TALK = """\\documentclass{beamer}
\\usetheme{Madrid}
\\title{A talk to take apart}
\\author{The workbench}
\\begin{document}
\\frame{\\titlepage}

\\begin{frame}[label=native]{What becomes native}
  \\begin{itemize}
    \\item Text becomes real text boxes
    \\item Math like $e^{i\\pi} + 1 = 0$ becomes runs
    \\item A block becomes a shape you can move
  \\end{itemize}
  \\begin{block}{And this is a block}
    Its panel, title bar and shadow are rebuilt as Slides objects.
  \\end{block}
\\end{frame}

\\begin{frame}[label=table]{A table}
  \\begin{tabular}{lr}
    \\hline
    Stage & Seconds \\\\
    \\hline
    extract & 0.3 \\\\
    classify & 0.6 \\\\
    \\hline
  \\end{tabular}
\\end{frame}
\\end{document}
"""

DOCUMENT = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>A document to take apart</title>
</head>
<body>
<h1>A document to take apart</h1>
<p>This is the canonical file the Google Docs side works from: what the source says.
Push it with <code>doc_push</code>, edit the document in your browser, edit this file
here, and <code>doc_sync</code> merges the two.</p>
<h2>What the merge is about</h2>
<p>Where both sides moved, the document wins: it is what the reader is looking at.</p>
<ul>
<li>A block is a paragraph, a heading, an item or a table.</li>
<li>Each one gets a named range in the document, which is how it is recognised again.</li>
<li>A second sync with nothing changed writes zero requests.</li>
</ul>
</body>
</html>
"""

README = """# This workspace

Your own folder on this server. The tools on the right are the eleven journeys of
beamer2slides plus `tex_compile`, and every one of them reads and writes here and
nowhere else.

A first round trip:

1. `tex_compile` with `tex=talk.tex` -> `talk.pdf`
2. `deck_inspect` with `pdf=talk.pdf` -> what each page would become
3. `deck_convert` with `pdf=talk.pdf` -> a deck in your own Drive (sign in first)
4. edit the deck in Slides, edit `talk.tex` here, then `deck_sync` with `dry_run=true`

The Docs side starts at `doc_push` with `path=doc.html`.

Nothing here is kept: the workspace goes when the server sweeps it.
"""


def seed(root: Path) -> None:
    """What a new workspace starts with: something to compile, something to push, and why."""
    (root / "README.md").write_text(README, encoding="utf-8")
    (root / "talk.tex").write_text(TALK, encoding="utf-8")
    (root / "doc.html").write_text(DOCUMENT, encoding="utf-8")


# ---------------------------------------------------------------- the catalogue

_CATALOGUE: dict | None = None


def catalogue() -> dict:
    """The tools as the page draws them: the published schemas, plus the compile step.

    `agent.schema` reads these off the tools themselves, so the form a visitor fills in and the
    signature that runs are the same text. Built once: importing the registry pulls in the whole
    pipeline.
    """
    global _CATALOGUE
    if _CATALOGUE is None:
        from ..agent import schema, tools
        _CATALOGUE = {"tools": [COMPILE_TOOL, *schema.all_schemas(tools.TOOLS)],
                      "instructions": tools.INSTRUCTIONS}
    return _CATALOGUE


def needs_google(tool: str) -> bool:
    for published in catalogue()["tools"]:
        if published["name"] == tool:
            return bool(published["effects"]["google"])
    raise Denied(f"there is no tool called {tool!r}", 404)
