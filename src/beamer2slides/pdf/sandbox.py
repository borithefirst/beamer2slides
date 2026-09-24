"""A PDF backend in another process: the PDF library runs in a worker that can be sandboxed.

    B2S_PDF_BACKEND=sandbox                  PDFium in a worker (python -m beamer2slides.pdf.sandbox)
    B2S_PDF_BACKEND=sandbox:<spec>           another backend in the worker (spec as for B2S_PDF_BACKEND)
    B2S_PDF_SANDBOX_CMD=<command>            how to start the worker: a JSON list or a shell-like
                                             string, e.g. a container or jail around
                                             `python -m beamer2slides.pdf.sandbox --backend pdfium`
    B2S_PDF_SANDBOX_TIMEOUT=<seconds>        one request longer than this kills the worker (120)

The worker needs no file system, network or credentials: the client reads the PDF and sends its
bytes, and `save` sends the new file back. Requests and answers are `wire` frames on the worker's
stdin/stdout (data only; nothing the worker says is executed). A worker that dies or hangs costs
the documents it held (PdfError) and is started again for the next one."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

from .. import interpreter
from . import wire
from .api import Box, Char, EmbeddedImage, PageObject, PdfError

ENV_CMD = "B2S_PDF_SANDBOX_CMD"
ENV_TIMEOUT = "B2S_PDF_SANDBOX_TIMEOUT"

DOC_OPS = {"metadata", "label", "named_dests", "save", "close", "page"}
PAGE_OPS = {"objects", "object_bounds", "set_active", "chars", "glyph_widths", "drawings", "images",
            "embedded_image", "links", "render"}


# ---------------------------------------------------------------------- client


class SandboxBackend:
    def __init__(self, inner: str = "pdfium", command: Sequence[str] | str | None = None,
                 timeout: float | None = None):
        self.name = f"sandbox:{inner}"
        self.inner = inner
        command = command if command is not None else os.environ.get(ENV_CMD)
        if isinstance(command, str):
            command = json.loads(command) if command.lstrip().startswith("[") else \
                shlex.split(command, posix=os.name != "nt")
        self.command = list(command) if command else \
            [interpreter.python(), "-m", "beamer2slides.pdf.sandbox", "--backend", inner]
        self.timeout = timeout if timeout is not None else float(os.environ.get(ENV_TIMEOUT) or 120)
        self._proc: subprocess.Popen | None = None
        self._generation = 0
        self._lock = threading.RLock()
        atexit.register(self.shutdown)

    def open(self, source: str | Path | bytes) -> "SandboxDocument":
        path = None
        if not isinstance(source, (bytes, bytearray)):
            path = Path(source)
            source = path.read_bytes()
        info = self.call("open", None, None, [bytes(source)])
        return SandboxDocument(self, info["doc"], info["pages"], path, self._generation)

    def _start(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=None, bufsize=0, env=interpreter.env())
            self._generation += 1
        return self._proc

    def call(self, op: str, doc: int | None, page: int | None, args: list, generation: int | None = None):
        with self._lock:
            proc = self._start()
            if generation is not None and generation != self._generation:
                raise PdfError("the PDF worker was restarted: this document is gone")
            timer = threading.Timer(self.timeout, proc.kill)
            timer.start()
            try:
                wire.write_frame(proc.stdin, {"op": op, "doc": doc, "page": page, "args": args})
                answer = wire.read_frame(proc.stdout)
            except (OSError, EOFError, wire.WireError) as e:
                self._kill()
                why = "took too long" if not timer.is_alive() else f"failed ({type(e).__name__}: {e})"
                raise PdfError(f"the PDF worker {why} on {op}") from e
            finally:
                timer.cancel()
        if not isinstance(answer, dict) or not ("ok" in answer or "error" in answer):
            raise PdfError(f"the PDF worker answered {str(answer)[:80]}")
        if "error" in answer:
            kind, message = (list(answer["error"]) + ["", ""])[:2]
            if kind == "IndexError":
                raise IndexError(message)
            raise PdfError(f"{kind}: {message}")
        return answer["ok"]

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def shutdown(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._kill()


class SandboxDocument:
    def __init__(self, backend: SandboxBackend, doc: int, count: int, path: Path | None, generation: int):
        self.backend, self.id, self.path = backend, doc, path
        self._count, self._generation = count, generation
        self._pages: dict[int, SandboxPage] = {}
        self._closed = False

    def _call(self, op: str, *args, page: int | None = None):
        if self._closed:
            raise PdfError("the document is closed")
        return self.backend.call(op, self.id, page, list(args), self._generation)

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index: int) -> "SandboxPage":
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        if index not in self._pages:
            width, height = self._call("page", index)
            self._pages[index] = SandboxPage(self, index, width, height)
        return self._pages[index]

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def metadata(self) -> dict:
        return self._call("metadata")

    def label(self, index: int) -> str:
        return self._call("label", index)

    def named_dests(self) -> list[tuple[str, int]]:
        return [tuple(d) for d in self._call("named_dests")]

    def save(self, pages: Sequence[int] | None = None, boxes: dict[int, Box] | None = None) -> bytes:
        return self._call("save", None if pages is None else list(pages), boxes)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call("close")
        except PdfError:
            pass  # the worker is gone, and the document with it
        self._closed = True
        self._pages.clear()


class SandboxPage:
    def __init__(self, doc: SandboxDocument, index: int, width: float, height: float):
        self.doc, self.index, self.width, self.height = doc, index, width, height
        self._objects: list[PageObject] | None = None
        self._bounds: list[Box] | None = None

    @property
    def rect(self) -> Box:
        return 0.0, 0.0, self.width, self.height

    def _call(self, op: str, *args):
        return self.doc._call(op, *args, page=self.index)

    def objects(self) -> list[PageObject]:
        if self._objects is None:
            self._objects = self._call("objects")
        return self._objects

    def object_bounds(self) -> list[Box]:
        if self._bounds is None:
            self._bounds = self._call("object_bounds")
        return list(self._bounds)

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        self._call("set_active", [int(o) for o in objects], bool(active))

    def chars(self) -> list[Char]:
        return self._call("chars")

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        return self._call("glyph_widths", [tuple(r) for r in requests]) if requests else []

    def drawings(self) -> list[dict]:
        return self._call("drawings")

    def images(self) -> list[dict]:
        return self._call("images")

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        return self._call("embedded_image", int(obj))

    def links(self) -> list[dict]:
        return self._call("links")

    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False) -> np.ndarray:
        return self._call("render", float(zoom), None if clip is None else tuple(float(v) for v in clip),
                          bool(transparent))


# ---------------------------------------------------------------------- worker


class Worker:
    """Serves one backend: documents by number, the api's methods by name (and no others)."""

    def __init__(self, backend):
        self.backend = backend
        self.docs: dict[int, object] = {}
        self.next_id = 1

    def handle(self, request: dict):
        op, doc, page, args = request["op"], request.get("doc"), request.get("page"), request.get("args") or []
        if op == "open":
            (data,) = args
            if not isinstance(data, bytes):
                raise TypeError("open takes the PDF's bytes")
            d = self.backend.open(data)
            self.docs[self.next_id] = d
            self.next_id += 1
            return {"doc": self.next_id - 1, "pages": len(d)}
        d = self.docs.get(doc)
        if d is None:
            raise PdfError(f"no document {doc!r}")
        if page is None:
            if op not in DOC_OPS:
                raise PdfError(f"unknown document request {op!r}")
            if op == "page":
                p = d[int(args[0])]
                return p.width, p.height
            if op == "metadata":
                return d.metadata
            if op == "close":
                self.docs.pop(doc).close()
                return None
            return getattr(d, op)(*args)
        if op not in PAGE_OPS:
            raise PdfError(f"unknown page request {op!r}")
        return getattr(d[int(page)], op)(*args)

    def serve(self, stdin, stdout) -> None:
        while True:
            try:
                request = wire.read_frame(stdin)
            except EOFError:
                return
            try:
                answer = {"ok": self.handle(request)}
            except Exception as e:  # noqa: BLE001 - every failure goes back as an answer
                answer = {"error": [type(e).__name__, str(e)]}
            wire.write_frame(stdout, answer)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="beamer2slides PDF worker: wire frames on stdin/stdout")
    parser.add_argument("--backend", default="pdfium", help="backend spec as for B2S_PDF_BACKEND (not sandbox)")
    args = parser.parse_args(argv)
    if args.backend.split(":")[0] == "sandbox":
        parser.error("a worker cannot run the sandbox itself")
    from . import resolve
    backend = resolve(args.backend)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print must not corrupt a frame
    Worker(backend).serve(stdin, stdout)


if __name__ == "__main__":
    main()
