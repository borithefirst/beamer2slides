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
the documents it held (PdfError) and is started again for the next one. Every answer is read as
the api type it stands for (`wire`'s narrowings): a worker that answers something else is a
PdfError where it answers, not a wrong value further on."""

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
from typing import Callable, Iterator, Literal, Mapping, Sequence, TypeVar

from ..arrays import Pixels
from .. import interpreter
from . import wire
from .api import (Box, Char, Drawing, DrawingItem, DrawingKind, EmbeddedImage, ImageInfo, Link, Metadata,
                  PageLink, PageObject, PdfBackend, PdfDocument, PdfError, PdfPage, Rgb, UriLink, fill_drawing)
from .wire import (Wire, as_bool, as_box, as_bytes, as_dict, as_float, as_int, as_list, as_pixels, as_point,
                   as_str, as_tuple, item)
from ..typing_compat import assert_never

ENV_CMD = "B2S_PDF_SANDBOX_CMD"
ENV_TIMEOUT = "B2S_PDF_SANDBOX_TIMEOUT"

DocOp = Literal["metadata", "label", "named_dests", "save", "close", "page"]
PageOp = Literal["objects", "object_bounds", "set_active", "chars", "glyph_widths", "drawings", "images",
                 "embedded_image", "links", "render"]
DOC_OPS = {"metadata", "label", "named_dests", "save", "close", "page"}
PAGE_OPS = {"objects", "object_bounds", "set_active", "chars", "glyph_widths", "drawings", "images",
            "embedded_image", "links", "render"}

T = TypeVar("T")


# ---------------------------------------------------------------------- client


def _command(command: Sequence[str] | str | None, inner: str) -> list[str]:
    """How to start the worker: `command`, else $B2S_PDF_SANDBOX_CMD, else this package's worker."""
    given = command if command is not None else os.environ.get(ENV_CMD)
    if isinstance(given, str):
        if given.lstrip().startswith("["):
            words = json.loads(given)
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ValueError(f"{ENV_CMD}: a JSON command is a list of strings")
            given = [w for w in words if isinstance(w, str)]
        else:
            given = shlex.split(given, posix=os.name != "nt")
    if given:
        return list(given)
    return [interpreter.python(), "-m", "beamer2slides.pdf.sandbox", "--backend", inner]


class SandboxBackend:
    def __init__(self, inner: str, command: Sequence[str] | str | None, timeout: float | None) -> None:
        """`inner`: the worker's backend spec; `command` / `timeout` None: the environment's
        ($B2S_PDF_SANDBOX_CMD, $B2S_PDF_SANDBOX_TIMEOUT), else this package's worker and 120 s."""
        self.name = f"sandbox:{inner}"
        self.inner = inner
        self.command = _command(command, inner)
        self.timeout = timeout if timeout is not None else float(os.environ.get(ENV_TIMEOUT) or 120)
        self._proc: subprocess.Popen[bytes] | None = None
        self._generation = 0
        self._lock = threading.RLock()
        atexit.register(self.shutdown)

    def open(self, source: str | Path | bytes) -> SandboxDocument:
        path = None
        if not isinstance(source, bytes):
            path = Path(source)
            source = path.read_bytes()
        doc, pages = self.call("open", None, None, [source], None, _opened)
        return SandboxDocument(self, doc, pages, path, self._generation)

    def _start(self) -> subprocess.Popen[bytes]:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=None, bufsize=0, env=interpreter.env())
            self._generation += 1
        return self._proc

    def call(self, op: str, doc: int | None, page: int | None, args: list[object], generation: int | None,
             parse: Callable[[Wire], T]) -> T:
        """One request, its answer read by `parse`. `generation`: the worker the document was
        opened in (None for open), so a restarted worker's documents are refused."""
        with self._lock:
            proc = self._start()
            if generation is not None and generation != self._generation:
                raise PdfError("the PDF worker was restarted: this document is gone")
            timer = threading.Timer(self.timeout, proc.kill)
            timer.start()
            try:
                if proc.stdin is None or proc.stdout is None:
                    raise OSError("the worker has no pipes")
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
            error = answer["error"]
            parts = [str(p) for p in error] if isinstance(error, (list, tuple)) else [str(error)]
            kind, message = (parts + ["", ""])[:2]
            if kind == "IndexError":
                raise IndexError(message)
            raise PdfError(f"{kind}: {message}")
        try:
            return parse(answer["ok"])
        except wire.WireError as e:
            raise PdfError(f"the PDF worker answered {op} with {e}") from e

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def shutdown(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    if self._proc.stdin is not None:
                        self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._kill()


class SandboxDocument:
    def __init__(self, backend: SandboxBackend, doc: int, count: int, path: Path | None, generation: int) -> None:
        self.backend, self.id = backend, doc
        self.path: Path | None = path
        self._count, self._generation = count, generation
        self._pages: dict[int, SandboxPage] = {}
        self._closed = False

    def _call(self, op: DocOp | PageOp, page: int | None, args: list[object], parse: Callable[[Wire], T]) -> T:
        if self._closed:
            raise PdfError("the document is closed")
        return self.backend.call(op, self.id, page, args, self._generation, parse)

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index: int) -> SandboxPage:
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        if index not in self._pages:
            width, height = self._call("page", None, [index], _size)
            self._pages[index] = SandboxPage(self, index, width, height)
        return self._pages[index]

    def __iter__(self) -> Iterator[SandboxPage]:
        return (self[i] for i in range(len(self)))

    def __enter__(self) -> SandboxDocument:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def metadata(self) -> Metadata:
        return self._call("metadata", None, [], _metadata)

    def label(self, index: int) -> str:
        return self._call("label", None, [index], _str)

    def named_dests(self) -> list[tuple[str, int]]:
        return self._call("named_dests", None, [], _named_dests)

    # The contract's defaults (api.PdfDocument.save): its callers are outside pdf/.
    def save(self, pages: Sequence[int] | None = None, boxes: Mapping[int, Box] | None = None) -> bytes:
        return self._call("save", None, [None if pages is None else list(pages),
                                         None if boxes is None else dict(boxes)], _bytes)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call("close", None, [], _nothing)
        except PdfError:
            pass  # the worker is gone, and the document with it
        self._closed = True
        self._pages.clear()


class SandboxPage:
    def __init__(self, doc: SandboxDocument, index: int, width: float, height: float) -> None:
        self.doc, self.index = doc, index
        self.width: float = width
        self.height: float = height
        self._objects: list[PageObject] | None = None
        self._bounds: list[Box] | None = None

    @property
    def rect(self) -> Box:
        return 0.0, 0.0, self.width, self.height

    def _call(self, op: PageOp, args: list[object], parse: Callable[[Wire], T]) -> T:
        return self.doc._call(op, self.index, args, parse)

    def objects(self) -> list[PageObject]:
        if self._objects is None:
            self._objects = self._call("objects", [], _page_objects)
        return self._objects

    def object_bounds(self) -> list[Box]:
        if self._bounds is None:
            self._bounds = self._call("object_bounds", [], _boxes)
        return list(self._bounds)

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        self._call("set_active", [list(objects), active], _nothing)

    def chars(self) -> list[Char]:
        return self._call("chars", [], _chars)

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        return self._call("glyph_widths", [list(requests)], _widths) if requests else []

    def drawings(self) -> list[Drawing]:
        return self._call("drawings", [], _drawings)

    def images(self) -> list[ImageInfo]:
        return self._call("images", [], _images)

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        return self._call("embedded_image", [obj], _embedded_image)

    def links(self) -> list[Link]:
        return self._call("links", [], _links)

    # The contract's defaults (api.PdfPage.render): its callers are outside pdf/.
    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False) -> Pixels:
        return self._call("render", [float(zoom), None if clip is None else tuple(float(v) for v in clip),
                                     transparent], _pixels)


# ---------------------------------------------------------------------- reading the answers
# What each request answers, read as the api type (wire's narrowings raise WireError otherwise).


def _nothing(value: Wire) -> None:
    return None


def _opened(value: Wire) -> tuple[int, int]:
    d = as_dict(value, "open")
    return as_int(item(d, "doc", "open"), "open's doc"), as_int(item(d, "pages", "open"), "open's pages")


def _size(value: Wire) -> tuple[float, float]:
    return as_point(value, "a page's size")


def _metadata(value: Wire) -> Metadata:
    d = as_dict(value, "metadata")
    return Metadata(title=as_str(item(d, "title", "metadata"), "metadata's title"),
                    producer=as_str(item(d, "producer", "metadata"), "metadata's producer"))


def _str(value: Wire) -> str:
    return as_str(value, "a label")


def _named_dests(value: Wire) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for dest in as_list(value, "named_dests"):
        pair = as_tuple(dest, "a named destination")
        if len(pair) != 2:
            raise wire.WireError(f"a named destination of {len(pair)} parts")
        out.append((as_str(pair[0], "a destination's name"), as_int(pair[1], "a destination's page")))
    return out


def _bytes(value: Wire) -> bytes:
    return as_bytes(value, "a saved PDF")


def _page_objects(value: Wire) -> list[PageObject]:
    out: list[PageObject] = []
    for po in as_list(value, "objects"):
        if not isinstance(po, PageObject):
            raise wire.WireError(f"objects: {type(po).__name__} where a PageObject goes")
        out.append(po)
    return out


def _boxes(value: Wire) -> list[Box]:
    return [as_box(b, "an object's bounds") for b in as_list(value, "object_bounds")]


def _chars(value: Wire) -> list[Char]:
    out: list[Char] = []
    for ch in as_list(value, "chars"):
        if not isinstance(ch, Char):
            raise wire.WireError(f"chars: {type(ch).__name__} where a Char goes")
        out.append(ch)
    return out


def _widths(value: Wire) -> list[float | None]:
    return [None if w is None else as_float(w, "a glyph width") for w in as_list(value, "glyph_widths")]


def _rgb(value: Wire) -> Rgb | None:
    if value is None:
        return None
    parts = as_tuple(value, "a colour")
    if len(parts) != 3:
        raise wire.WireError(f"a colour of {len(parts)} parts")
    return as_float(parts[0], "a colour"), as_float(parts[1], "a colour"), as_float(parts[2], "a colour")


def _drawing_kind(value: Wire) -> DrawingKind:
    if value == "f":
        return "f"
    if value == "s":
        return "s"
    if value == "fs":
        return "fs"
    raise wire.WireError(f"a drawing of type {value!r}")


def _drawing_item(value: Wire) -> DrawingItem:
    parts = as_tuple(value, "a path item")
    match parts:
        case ("l", p0, p1):
            return "l", as_point(p0, "a line"), as_point(p1, "a line")
        case ("c", p0, p1, p2, p3):
            return "c", as_point(p0, "a curve"), as_point(p1, "a curve"), as_point(p2, "a curve"), \
                as_point(p3, "a curve")
        case ("re", box):
            return "re", as_box(box, "a rectangle")
        case ("qu", corners):
            quad = as_tuple(corners, "a quadrilateral")
            if len(quad) != 4:
                raise wire.WireError(f"a quadrilateral of {len(quad)} corners")
            return "qu", (as_point(quad[0], "a quadrilateral"), as_point(quad[1], "a quadrilateral"),
                          as_point(quad[2], "a quadrilateral"), as_point(quad[3], "a quadrilateral"))
        case _:
            raise wire.WireError(f"a path item {str(parts)[:40]}")


def _drawing(value: Wire) -> Drawing:
    """A drawing built as the backends build theirs (api.fill_drawing, then a stroke's keys), so
    its keys come in the order they had in the worker."""
    d = as_dict(value, "a drawing")
    kind = _drawing_kind(item(d, "type", "a drawing"))
    items = [_drawing_item(i) for i in as_list(item(d, "items", "a drawing"), "a drawing's items")]
    rect = as_box(item(d, "rect", "a drawing"), "a drawing's rect")
    obj = as_int(item(d, "object", "a drawing"), "a drawing's object")
    if kind == "s":
        return {"type": "s", "items": items, "rect": rect, "color": _rgb(item(d, "color", "a stroke")),
                "stroke_opacity": as_float(item(d, "stroke_opacity", "a stroke"), "stroke_opacity"),
                "width": as_float(item(d, "width", "a stroke"), "a stroke's width"), "object": obj}
    out = fill_drawing(items=items, rect=rect, even_odd=as_bool(item(d, "even_odd", "a fill"), "even_odd"),
                       fill=_rgb(item(d, "fill", "a fill")),
                       fill_opacity=as_float(item(d, "fill_opacity", "a fill"), "fill_opacity"), obj=obj,
                       soft_mask=as_bool(item(d, "soft_mask", "a fill"), "soft_mask"))
    if kind == "fs":
        out["color"] = _rgb(item(d, "color", "a stroke"))
        out["stroke_opacity"] = as_float(item(d, "stroke_opacity", "a stroke"), "stroke_opacity")
        out["width"] = as_float(item(d, "width", "a stroke"), "a stroke's width")
        out["type"] = "fs"
    elif kind != "f":
        assert_never(kind)
    return out


def _drawings(value: Wire) -> list[Drawing]:
    return [_drawing(d) for d in as_list(value, "drawings")]


def _images(value: Wire) -> list[ImageInfo]:
    out: list[ImageInfo] = []
    for im in as_list(value, "images"):
        d = as_dict(im, "an image")
        out.append(ImageInfo(bbox=as_box(item(d, "bbox", "an image"), "an image's bbox"),
                             width=as_int(item(d, "width", "an image"), "an image's width"),
                             height=as_int(item(d, "height", "an image"), "an image's height"),
                             object=as_int(item(d, "object", "an image"), "an image's object")))
    return out


def _embedded_image(value: Wire) -> EmbeddedImage | None:
    if value is None or isinstance(value, EmbeddedImage):
        return value
    raise wire.WireError(f"embedded_image: {type(value).__name__} where an EmbeddedImage goes")


def _links(value: Wire) -> list[Link]:
    out: list[Link] = []
    for link in as_list(value, "links"):
        d = as_dict(link, "a link")
        bbox = as_box(item(d, "bbox", "a link"), "a link's bbox")
        if "page" in d:
            out.append(PageLink(bbox=bbox, page=as_int(d["page"], "a link's page")))
        else:
            out.append(UriLink(bbox=bbox, uri=as_str(item(d, "uri", "a link"), "a link's uri")))
    return out


def _pixels(value: Wire) -> Pixels:
    return as_pixels(value, "a render")


# ---------------------------------------------------------------------- worker


class Worker:
    """Serves one backend: documents by number, the api's methods by name (and no others)."""

    def __init__(self, backend: PdfBackend) -> None:
        self.backend = backend
        self.docs: dict[int, PdfDocument] = {}
        self.next_id = 1

    def handle(self, request: Wire) -> object:
        """The answer to one request, `{"op", "doc", "page", "args"}`; what the request cannot be
        read as raises (and goes back as an error answer)."""
        r = as_dict(request, "a request")
        op = as_str(r["op"], "a request's op")
        doc, page, args = r.get("doc"), r.get("page"), as_list(r.get("args") or [], "a request's args")
        if op == "open":
            (data,) = args
            if not isinstance(data, bytes):
                raise TypeError("open takes the PDF's bytes")
            d = self.backend.open(data)
            self.docs[self.next_id] = d
            self.next_id += 1
            return {"doc": self.next_id - 1, "pages": len(d)}
        found = self.docs.get(doc) if isinstance(doc, int) else None
        if found is None:
            raise PdfError(f"no document {doc!r}")
        if page is None:
            return self._document(found, as_int(doc, "a request's doc"), op, args)
        return self._page(found[as_int(page, "a request's page")], op, args)

    def _document(self, d: PdfDocument, doc: int, op: str, args: list[Wire]) -> object:
        if op not in DOC_OPS:
            raise PdfError(f"unknown document request {op!r}")
        match op:
            case "page":
                p = d[as_int(args[0], "page's index")]
                return p.width, p.height
            case "metadata":
                return d.metadata
            case "close":
                self.docs.pop(doc).close()
                return None
            case "label":
                (index,) = args
                return d.label(as_int(index, "label's index"))
            case "named_dests":
                _no_args(op, args)
                return d.named_dests()
            case "save":
                pages, boxes = args
                return d.save(None if pages is None else [as_int(p, "save's page") for p in as_list(pages, "save")],
                              None if boxes is None else
                              {as_int(k, "save's page"): as_box(v, "save's box") for k, v in as_dict(boxes, "save").items()})
            case _:
                raise PdfError(f"unknown document request {op!r}")

    def _page(self, p: PdfPage, op: str, args: list[Wire]) -> object:
        if op not in PAGE_OPS:
            raise PdfError(f"unknown page request {op!r}")
        match op:
            case "objects":
                _no_args(op, args)
                return p.objects()
            case "object_bounds":
                _no_args(op, args)
                return p.object_bounds()
            case "set_active":
                objects, active = args
                p.set_active([as_int(o, "set_active's object") for o in as_list(objects, "set_active")],
                             as_bool(active, "set_active's switch"))
                return None
            case "chars":
                _no_args(op, args)
                return p.chars()
            case "glyph_widths":
                (requests,) = args
                return p.glyph_widths([_width_request(r) for r in as_list(requests, "glyph_widths")])
            case "drawings":
                _no_args(op, args)
                return p.drawings()
            case "images":
                _no_args(op, args)
                return p.images()
            case "embedded_image":
                (obj,) = args
                return p.embedded_image(as_int(obj, "embedded_image's object"))
            case "links":
                _no_args(op, args)
                return p.links()
            case "render":
                zoom, clip, transparent = args
                return p.render(as_float(zoom, "render's zoom"), None if clip is None else as_box(clip, "render's clip"),
                                as_bool(transparent, "render's transparency"))
            case _:
                raise PdfError(f"unknown page request {op!r}")

    def serve(self, stdin: wire.Reader, stdout: wire.Writer) -> None:
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


def _no_args(op: str, args: list[Wire]) -> None:
    if args:
        raise PdfError(f"{op} takes no arguments")


def _width_request(value: Wire) -> tuple[int, str, float]:
    if not isinstance(value, (tuple, list)):
        raise wire.WireError(f"a glyph width request: {type(value).__name__}")
    parts = value
    if len(parts) != 3:
        raise wire.WireError(f"a glyph width request of {len(parts)} parts")
    return (as_int(parts[0], "a request's font"), as_str(parts[1], "a request's character"),
            as_float(parts[2], "a request's size"))


def main(argv: list[str] | None) -> None:
    """The worker: `argv` as for the command line (None: sys.argv's)."""
    parser = argparse.ArgumentParser(description="beamer2slides PDF worker: wire frames on stdin/stdout")
    parser.add_argument("--backend", default="pdfium", help="backend spec as for B2S_PDF_BACKEND (not sandbox)")
    args = parser.parse_args(argv)
    spec: str = args.backend
    if spec.split(":")[0] == "sandbox":
        parser.error("a worker cannot run the sandbox itself")
    from . import resolve
    backend = resolve(spec)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print must not corrupt a frame
    Worker(backend).serve(stdin, stdout)


if __name__ == "__main__":
    main(None)
