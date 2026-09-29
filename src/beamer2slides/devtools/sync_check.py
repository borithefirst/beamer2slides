"""Checks on a synced Slides deck, read through presentations.get.

A check is a small JSON record (tools/deck_edits.py returns them as the expectations of its
edits, tests/decks/sync/build.py as what a source change must show):
  {"check": "text", "slide": SEL|null, "text": "...", "count": n}      occurrences in shapes and cells
  {"check": "title", "slide": SEL, "text": "..."}                        title placeholder, first paragraph
  {"check": "style", "slide": SEL, "text": "...", "context": "...", "bold"|"italic"|"color"|"size": ...}
  {"check": "box", "slide": SEL, "target": TARGET, "origin": [x, y], "size": [w, h], "tol": pt}
  {"check": "image", "slide": SEL, "near": [cx, cy], "colour": "#rrggbb", "count": n}   valid pictures
  {"check": "shape", "slide": SEL, "shape_type": "STAR_5", "near": [cx, cy], "color": "#rrggbb", "count": n}
  {"check": "grouped", "slide": SEL, "members": [TARGET, ...], "grouped": bool}
  {"check": "slides", "order": [SEL, ...], "adjacent": bool}             each once, in this order
  {"check": "slide_count", "slide": SEL, "count": n}
  {"check": "notes", "slide": SEL, "text": "..."}
  {"check": "background", "slide": SEL, "color": "#rrggbb"}
  {"check": "fresh", "slide": SEL}                      the slide matches a fresh conversion (compare_fresh)
SEL: {"title": "..."}, {"contains": "..."} (any text on the slide) or {"index": i}; a string is a title;
null in text checks: the whole deck.
TARGET: {"text": "...", "near": [cx, cy]?}, {"image_near": [cx, cy]}, {"image": "largest"} or {"id": objectId}.
Positions are absolute slide pt (720 pt wide), text is compared with whitespace normalised.

A check, a selector and a target are JSON where they come from (a file, an expectation) and are
parsed into records where they are read (`parse_check`, `selector`, `target`), each kind matched
to `assert_never`; a malformed one is a failed check, said as such.

On top: `integrity` (duplicates, orphans, groups intact), `check_report` (sync-report sections),
`compare_fresh` (untouched slides vs a fresh conversion: elements, notes, background, thumbnails)
and `alignment_compare` (tools/alignment.py on the synced deck vs the fresh conversion).

  python tools/sync_check.py <presentation id|url|out folder> checks.json [--report sync-report.json]
"""

import argparse
import io
import json
import re
import shutil
import sys
import urllib.request
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from ..arrays import Floats
from ..google_types import (AffineTransform, Page, PageElement, Presentation, SlidesService, background_fill,
                            children, object_id, part, parts, presentation)
from ..json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_str
from ..typing_compat import assert_never

EMU_PER_PT = 12700
TOL = 1.5            # pt: a box check's default tolerance
NEAR_TOL = 4.0       # pt: how far a centre may be from a `near` point (a target's, a count check's)
FRESH_TOL = 2.0      # pt: how far an element may stand from its fresh conversion's (compare_fresh)
THUMBNAIL_LIMIT = 0.002   # share of a thumbnail's pixels that may differ (thumbnail_diff)
ALIGNMENT_GROWTH = 0.75   # pt an alignment metric may grow past the fresh conversion's (alignment_compare)


class CheckError(Exception):
    pass


NOT_THE_CONVERTER = ("devtools", "agent", "playground", "__pycache__")
_STAMP: list[str] = []


def converter_stamp() -> str:
    """A hash of the code and data a conversion runs on (the package without its harness, agent
    and playground). A cached reference conversion is stale when this moved as surely as when its
    PDF did: one made before a converter change failed six stress scenarios on a box 8 pt narrower
    than a fresh conversion's (2026-09-24)."""
    if not _STAMP:
        import hashlib
        from importlib import resources
        root = Path(str(resources.files("beamer2slides")))
        h = hashlib.sha1()
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if p.is_file() and p.suffix in (".py", ".json", ".tex", ".sty") and not set(rel.parts) & set(NOT_THE_CONVERTER):
                h.update(rel.as_posix().encode())
                h.update(p.read_bytes())
        _STAMP.append(h.hexdigest()[:16])
    return _STAMP[0]


def norm(s: str | None) -> str:
    return " ".join((s or "").replace("\xa0", " ").replace("\x0b", " ").replace("​", "").split())


# ---------------------------------------------------------------- reading JSON

def number(v: Json, where: str) -> float:
    """A JSON number (not a bool), or a CheckError naming `where`."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CheckError(f"{where}: a number was expected, found {v!r}")
    return v


Point = tuple[float, float]


def point(v: Json, where: str) -> Point:
    """[x, y] as JSON."""
    xs = as_array(v, where)
    if len(xs) != 2:
        raise CheckError(f"{where}: [x, y] was expected, found {v!r}")
    return number(xs[0], where), number(xs[1], where)


def _text(v: Json, where: str) -> str:
    if not isinstance(v, str):
        raise CheckError(f"{where}: a string was expected, found {v!r}")
    return v


def _flag(v: Json, where: str) -> bool:
    if not isinstance(v, bool):
        raise CheckError(f"{where}: true or false was expected, found {v!r}")
    return v


def _need(o: JsonObject, key: str, where: str) -> Json:
    if key not in o:
        raise CheckError(f"{where}: no {key!r}")
    return o[key]


def hex_color(c: Json) -> str | None:
    """#rrggbb of a Slides colour ({"rgbColor": {...}}; None: absent, or a theme colour)."""
    rgb = part(c, "color").get("rgbColor")
    if rgb is None:
        return None
    o = as_object(rgb, "rgbColor")
    return "#" + "".join(f"{round(number(o.get(k, 0.0), 'rgbColor') * 255):02x}" for k in ("red", "green", "blue"))


def _affine(t: AffineTransform | None) -> Floats:
    if not t:
        t = AffineTransform(scaleX=1, scaleY=1)
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return np.array([[t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit],
                     [t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit], [0, 0, 1]],
                    dtype=np.float64)


def text_elements(obj: JsonObject) -> list[JsonObject]:
    """The textElements of a shape or a table cell (none: it holds no text)."""
    return parts(part(obj.get("text"), "text").get("textElements"), "text.textElements")


def _content(t: JsonObject, key: str) -> str:
    content = part(t.get(key), key).get("content")
    return content if isinstance(content, str) else ""


def raw_text(text_els: Sequence[JsonObject]) -> str:
    return "".join(_content(t, "textRun") or _content(t, "autoText") for t in text_els)


# ---------------------------------------------------------------- the deck as read

Kind = Literal["shape", "image", "table", "line", "group", "other"]
Box = tuple[float, float, float, float]   # x0, y0, x1, y1: absolute, slide pt


@dataclass(frozen=True, kw_only=True)
class Cell:
    """A table cell, by its row and column."""
    row: int
    column: int

    def location(self) -> JsonObject:
        """The cell as a request's `cellLocation`."""
        return {"rowIndex": self.row, "columnIndex": self.column}


@dataclass(frozen=True, kw_only=True)
class Element:
    obj: PageElement
    kind: Kind
    box: Box
    groups: tuple[str, ...]   # enclosing group objectIds, outermost first
    texts: tuple[tuple[str, Cell | None], ...]   # (raw text, cell) per container: a shape's one, a table's cells

    @property
    def id(self) -> str:
        return object_id(self.obj)

    @property
    def parent(self) -> str | None:
        return self.groups[-1] if self.groups else None

    @property
    def top(self) -> str:
        return self.groups[0] if self.groups else self.id

    @property
    def text(self) -> str:
        return norm(" | ".join(t for t, _ in self.texts))

    @property
    def center(self) -> Point:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    @property
    def shape(self) -> JsonObject:
        """The element's `shape` part ({} for any other element)."""
        return part(self.obj.get("shape"), "shape")

    @property
    def shape_type(self) -> str | None:
        t = self.shape.get("shapeType")
        return t if isinstance(t, str) else None

    @property
    def is_placeholder(self) -> bool:
        return "placeholder" in self.shape

    @property
    def placeholder_type(self) -> str | None:
        """TITLE, BODY, SUBTITLE... of a placeholder shape; None for any other element."""
        if self.kind != "shape":
            return None
        t = part(self.shape.get("placeholder"), "shape.placeholder").get("type")
        return t if isinstance(t, str) else None

    @property
    def solid_fill(self) -> JsonObject:
        """The shape's background `solidFill` ({}: none)."""
        props = part(self.shape.get("shapeProperties"), "shape.shapeProperties")
        return part(part(props.get("shapeBackgroundFill"), "shapeBackgroundFill").get("solidFill"), "solidFill")

    @property
    def table_size(self) -> tuple[int, int]:
        """(rows, columns) of a table."""
        table = self.obj.get("table")
        if table is None:
            raise CheckError(f"{self.id} is no table")
        return as_int(table.get("rows"), "table.rows"), as_int(table.get("columns"), "table.columns")

    def text_elements_in(self, cell: Cell | None) -> list[JsonObject]:
        """The textElements of the shape (cell None) or of one cell of the table."""
        if cell is None:
            return text_elements(self.shape)
        rows = parts(part(self.obj.get("table"), "table").get("tableRows"), "table.tableRows")
        return text_elements(parts(rows[cell.row].get("tableCells"), "tableCells")[cell.column])

    def descriptor(self) -> str:
        if self.kind == "shape":
            return f"shape:{self.shape_type}:{self.text[:80]}"
        if self.kind == "table":
            return f"table:{self.text[:80]}"
        return self.kind


def _kind(e: PageElement) -> Kind:
    if "shape" in e:
        return "shape"
    if "image" in e:
        return "image"
    if "table" in e:
        return "table"
    if "line" in e:
        return "line"
    return "group" if "elementGroup" in e else "other"


def _box(e: PageElement, m: Floats) -> Box:
    size = e.get("size")
    if size is None:
        return 0.0, 0.0, 0.0, 0.0
    w, h = (d.get("magnitude", 0.0) / (EMU_PER_PT if d.get("unit", "EMU") == "EMU" else 1)
            for d in (size.get("width", {}), size.get("height", {})))
    pts = m @ np.array([[0, w, 0, w], [0, 0, h, h], [1, 1, 1, 1]], dtype=np.float64)
    return float(pts[0].min()), float(pts[1].min()), float(pts[0].max()), float(pts[1].max())


def _texts(e: PageElement, kind: Kind) -> tuple[tuple[str, Cell | None], ...]:
    if kind == "shape":
        return ((raw_text(text_elements(part(e.get("shape"), "shape"))), None),)
    if kind == "table":
        rows = parts(part(e.get("table"), "table").get("tableRows"), "table.tableRows")
        return tuple((raw_text(text_elements(cell)), Cell(row=r, column=c))
                     for r, row in enumerate(rows) for c, cell in enumerate(parts(row.get("tableCells"), "tableCells")))
    return ()


def flatten(page_elements: Sequence[PageElement], parent_m: Floats, groups: tuple[str, ...], where: str) -> list[Element]:
    """Every element of `page_elements` and of their groups, a group before its children, placed on
    the slide through `parent_m` (the enclosing groups' transform; `np.eye(3)` on a page)."""
    out: list[Element] = []
    for e in page_elements:
        m = parent_m @ _affine(e.get("transform"))
        kind = _kind(e)
        box = _box(e, m)
        oid = object_id(e)
        kids: list[Element] = flatten(children(e, where), m, groups + (oid,), f"{where}/{oid}") if kind == "group" else []
        if kids:
            xs = [c.box for c in kids]
            box = min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs)
        out.append(Element(obj=e, kind=kind, box=box, groups=groups, texts=_texts(e, kind)))
        out += kids
    return out


def page_elements(page: Page) -> list[Element]:
    """Every element of a page, groups flattened (`flatten`)."""
    return flatten(page.get("pageElements", []), np.eye(3, dtype=np.float64), (), page.get("objectId", "page"))


@dataclass(frozen=True, kw_only=True)
class Slide:
    obj: Page
    index: int
    elements: tuple[Element, ...]

    @property
    def id(self) -> str:
        return object_id(self.obj)

    @property
    def title(self) -> str | None:
        for e in self.elements:
            if e.placeholder_type in ("TITLE", "CENTERED_TITLE"):
                return norm(e.texts[0][0].split("\n")[0])
        return None

    @property
    def all_text(self) -> str:
        return norm(" ".join(e.text for e in self.elements if e.texts))

    def notes_shape(self) -> PageElement | None:
        props = self.obj.get("slideProperties")
        page = None if props is None else props.get("notesPage")
        if page is None:
            return None
        nid = part(page.get("notesProperties"), "notesProperties").get("speakerNotesObjectId")
        return next((e for e in page.get("pageElements", []) if object_id(e) == nid), None)

    @property
    def notes(self) -> str:
        shape = self.notes_shape()
        body = None if shape is None else shape.get("shape")
        return "" if body is None else norm(raw_text(text_elements(body)))

    @property
    def background(self) -> str | None:
        fill = background_fill(self.obj)
        if fill.get("propertyState", "RENDERED") != "RENDERED":
            return None
        return hex_color(part(fill.get("solidFill"), "pageBackgroundFill.solidFill").get("color"))


# ---------------------------------------------------------------- selectors and targets

@dataclass(frozen=True, kw_only=True)
class ByTitle:
    title: str


@dataclass(frozen=True, kw_only=True)
class Containing:
    text: str


@dataclass(frozen=True, kw_only=True)
class AtIndex:
    index: int


Selector = ByTitle | Containing | AtIndex


def selector(sel: Json) -> Selector:
    """A slide selector (SEL) as JSON: a string is a title."""
    if isinstance(sel, str):
        return ByTitle(title=sel)
    if not isinstance(sel, dict):
        raise CheckError(f"slide selector {sel!r}: a title or an object was expected")
    if "index" in sel:
        return AtIndex(index=as_int(sel["index"], "slide selector index"))
    if "title" in sel:
        return ByTitle(title=_text(sel["title"], "slide selector title"))
    return Containing(text=_text(_need(sel, "contains", "slide selector"), "slide selector contains"))


@dataclass(frozen=True, kw_only=True)
class ById:
    id: str


@dataclass(frozen=True, kw_only=True)
class ByText:
    text: str
    near: Point | None
    tol: float


@dataclass(frozen=True, kw_only=True)
class ImageNear:
    at: Point


@dataclass(frozen=True, kw_only=True)
class LargestImage:
    pass


Target = ById | ByText | ImageNear | LargestImage


def target(t: Json) -> Target:
    """An element target (TARGET) as JSON."""
    o: JsonObject = t if isinstance(t, dict) else {}
    if "id" in o:
        return ById(id=_text(o["id"], "target id"))
    if "text" in o:
        return ByText(text=_text(o["text"], "target text"),
                      near=point(o["near"], "target near") if "near" in o else None,
                      tol=number(o["tol"], "target tol") if "tol" in o else NEAR_TOL)
    if "image_near" in o:
        return ImageNear(at=point(o["image_near"], "target image_near"))
    if o.get("image") == "largest":
        return LargestImage()
    raise CheckError(f"unknown target {t}")


def _near(e: Element, at: Point, tol: float) -> bool:
    return max(abs(e.center[0] - at[0]), abs(e.center[1] - at[1])) <= tol


class Model:
    """A presentations.get result with lookups by content. `pres` is the answer as JSON (what the
    fuzzers and dumps read and write), `presentation` the same object as the API's type."""

    def __init__(self, pres: JsonObject) -> None:
        self.pres = pres
        self.presentation: Presentation = presentation(pres, "presentations.get")
        self.slides = tuple(Slide(obj=s, index=i, elements=tuple(page_elements(s)))
                            for i, s in enumerate(self.presentation.get("slides", [])))

    @property
    def pid(self) -> str:
        pid = self.presentation.get("presentationId")
        if pid is None:
            raise CheckError("a presentation read without its presentationId")
        return pid

    @property
    def revision(self) -> str | None:
        return self.presentation.get("revisionId")

    def find(self, sel: Json) -> list[Slide]:
        """The slides a selector (SEL) picks; None: every slide."""
        if sel is None:
            return list(self.slides)
        match selector(sel):
            case AtIndex(index=i):
                return list(self.slides[i:i + 1])
            case ByTitle(title=title):
                return [s for s in self.slides if s.title == norm(title)]
            case Containing(text=text):
                return [s for s in self.slides if norm(text) in s.all_text]
            case unreachable:
                assert_never(unreachable)

    def one(self, sel: Json) -> Slide:
        found = self.find(sel)
        if len(found) != 1:
            raise CheckError(f"slide {json.dumps(sel, ensure_ascii=False)}: {len(found)} slides match")
        return found[0]

    def element(self, slide: Slide, t: Json) -> Element:
        """The one element of `slide` a target (TARGET) names."""
        match target(t):
            case ById(id=oid):
                found = [e for e in slide.elements if e.id == oid]
            case ByText(text=text, near=near, tol=tol):
                phrase = norm(text)
                found = [e for e in slide.elements if e.kind in ("shape", "table") and phrase in e.text]
                if near is not None:
                    found = [e for e in found if _near(e, near, tol)]
            case ImageNear(at=at):
                found = [e for e in slide.elements if e.kind == "image" and _near(e, at, NEAR_TOL)]
            case LargestImage():
                images = sorted((e for e in slide.elements if e.kind == "image"),
                                key=lambda e: (e.box[2] - e.box[0]) * (e.box[3] - e.box[1]), reverse=True)
                found = images[:1]
            case unreachable:
                assert_never(unreachable)
        if len(found) != 1:
            raise CheckError(f"slide {slide.index + 1} ({slide.title}): {len(found)} elements match {t}")
        return found[0]


def phrase_span(raw: str, phrase: str) -> tuple[int, int] | None:
    """(start, end) of a phrase in raw text, whitespace-insensitive; code point indices."""
    words = norm(phrase).split(" ")
    m = re.search(r"[\s\xa0\x0b​]+".join(map(re.escape, words)), raw)
    return (m.start(), m.end()) if m else None


def utf16(s: str, i: int) -> int:
    """Slides text indices count UTF-16 code units."""
    return len(s[:i].encode("utf-16-le")) // 2


def style_runs(text_els: Sequence[JsonObject], raw: str, start: int, end: int) -> list[JsonObject]:
    """Styles of the runs overlapping [start, end) (code point indices of `raw`)."""
    a, b = utf16(raw, start), utf16(raw, end)
    out: list[JsonObject] = []
    for t in text_els:
        run = t.get("textRun")
        if run is None:
            continue
        run = as_object(run, "textRun")
        if number(t.get("startIndex", 0), "startIndex") < b and number(_need(t, "endIndex", "textElement"), "endIndex") > a \
                and _text(run.get("content"), "textRun.content").strip():
            out.append(part(run.get("style"), "textRun.style"))
    return out


def _download(url: str) -> Image.Image:
    with urllib.request.urlopen(url, timeout=60) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGBA")


def image_problem(el: Element, colour: str | None) -> str | None:
    """None if the picture downloads, decodes, isn't empty and (optionally) shows the colour."""
    url = part(el.obj.get("image"), "image").get("contentUrl")
    if not url:
        return f"picture {el.id} has no contentUrl"
    try:
        img = np.asarray(_download(_text(url, "image.contentUrl"))).astype(np.int16)
    except Exception as e:  # noqa: BLE001
        return f"picture {el.id} doesn't load: {e}"
    if img.shape[0] < 2 or img.shape[1] < 2:
        return f"picture {el.id} is {img.shape[1]}x{img.shape[0]} px"
    opaque = img[..., 3] > 32
    rgb = img[..., :3]
    if not opaque.any() or (opaque.all() and np.ptp(rgb.reshape(-1, 3), axis=0).max() < 8):
        return f"picture {el.id} is blank"
    if colour:
        want = np.array([int(colour[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int16)
        close = (np.abs(rgb - want).max(axis=2) < 70) & opaque
        if close.mean() < 0.001:
            return f"picture {el.id} shows no {colour}"
    return None


# ---------------------------------------------------------------- checks

@dataclass(frozen=True, kw_only=True)
class Fresh:
    """Judged by `compare_fresh`, not here."""


@dataclass(frozen=True, kw_only=True)
class SlideOrder:
    order: tuple[Json, ...]
    adjacent: bool


@dataclass(frozen=True, kw_only=True)
class SlideCount:
    slide: Json
    count: int


@dataclass(frozen=True, kw_only=True)
class TextCount:
    slide: Json          # None: the whole deck
    text: str
    count: int


@dataclass(frozen=True, kw_only=True)
class TitleIs:
    slide: Json
    text: str


@dataclass(frozen=True, kw_only=True)
class NotesAre:
    slide: Json
    text: str


@dataclass(frozen=True, kw_only=True)
class BackgroundIs:
    slide: Json
    color: str


STYLE_FLAGS = ("bold", "italic", "underline", "strikethrough")


@dataclass(frozen=True, kw_only=True)
class StyleIs:
    slide: Json
    text: str
    context: str | None
    flags: tuple[tuple[str, bool], ...]   # (a STYLE_FLAGS key, the value wanted), those the check names
    color: str | None
    size: float | None


@dataclass(frozen=True, kw_only=True)
class BoxAt:
    slide: Json
    target: Json
    origin: Point | None
    size: Point | None
    tol: float


@dataclass(frozen=True, kw_only=True)
class ImageCount:
    slide: Json
    near: Point | None
    tol: float
    colour: str | None
    count: int


@dataclass(frozen=True, kw_only=True)
class ShapeCount:
    slide: Json
    shape_type: str
    color: str | None
    near: Point | None
    tol: float
    count: int


@dataclass(frozen=True, kw_only=True)
class TableCount:
    slide: Json
    near: Point | None
    tol: float
    count: int


@dataclass(frozen=True, kw_only=True)
class Grouped:
    slide: Json
    members: tuple[Json, ...]
    grouped: bool


Check = (Fresh | SlideOrder | SlideCount | TextCount | TitleIs | NotesAre | BackgroundIs | StyleIs | BoxAt
         | ImageCount | ShapeCount | TableCount | Grouped)


def parse_check(c: JsonObject) -> Check:
    """A check record as JSON (the module's docstring), or a CheckError saying what is wrong with it."""
    kind = _text(_need(c, "check", "check"), "check")

    def need(key: str) -> Json:
        return _need(c, key, f"{kind} check")

    def text(key: str) -> str:
        return _text(need(key), f"{kind} check {key}")

    def optional_text(key: str) -> str | None:
        return _text(c[key], f"{kind} check {key}") if key in c else None

    def count() -> int:
        return as_int(c["count"], f"{kind} check count") if "count" in c else 1

    def near() -> Point | None:
        return point(c["near"], f"{kind} check near") if "near" in c else None

    def tol(default: float) -> float:
        return number(c["tol"], f"{kind} check tol") if "tol" in c else default

    if kind == "fresh":
        return Fresh()
    if kind == "slides":
        return SlideOrder(order=tuple(as_array(need("order"), "slides check order")), adjacent=bool(c.get("adjacent")))
    if kind == "slide_count":
        return SlideCount(slide=need("slide"), count=as_int(need("count"), "slide_count check count"))
    if kind == "text":
        return TextCount(slide=c.get("slide"), text=text("text"), count=count())
    if kind == "title":
        return TitleIs(slide=need("slide"), text=text("text"))
    if kind == "notes":
        return NotesAre(slide=need("slide"), text=text("text"))
    if kind == "background":
        return BackgroundIs(slide=need("slide"), color=text("color"))
    if kind == "style":
        return StyleIs(slide=need("slide"), text=text("text"), context=optional_text("context"),
                       flags=tuple((key, _flag(c[key], f"style check {key}")) for key in STYLE_FLAGS if key in c),
                       color=optional_text("color"),
                       size=number(c["size"], "style check size") if "size" in c else None)
    if kind == "box":
        return BoxAt(slide=need("slide"), target=need("target"),
                     origin=point(c["origin"], "box check origin") if "origin" in c else None,
                     size=point(c["size"], "box check size") if "size" in c else None, tol=tol(TOL))
    if kind == "image":
        return ImageCount(slide=need("slide"), near=near(), tol=tol(NEAR_TOL), colour=optional_text("colour"),
                          count=count())
    if kind == "shape":
        return ShapeCount(slide=need("slide"), shape_type=text("shape_type"), color=optional_text("color"),
                          near=near(), tol=tol(NEAR_TOL), count=count())
    if kind == "table":
        return TableCount(slide=need("slide"), near=near(), tol=tol(NEAR_TOL), count=count())
    if kind == "grouped":
        return Grouped(slide=need("slide"), members=tuple(as_array(need("members"), "grouped check members")),
                       grouped=_flag(need("grouped"), "grouped check grouped"))
    raise CheckError(f"unknown check {kind}")


def evaluate(model: Model, check: JsonObject) -> str | None:
    """None if the check holds, else what is wrong."""
    try:
        return _evaluate(model, parse_check(check), json.dumps(check, ensure_ascii=False))
    except (CheckError, JsonShapeError) as e:
        return str(e)


def _style_problem(model: Model, c: StyleIs, what: str) -> str:
    """What is wrong with the style of `c.text` (the empty string: nothing)."""
    context = c.context if c.context is not None else c.text
    el = model.element(model.one(c.slide), {"text": context})
    for raw, cell in el.texts:
        ctx = phrase_span(raw, context)
        if not ctx:
            continue
        span = phrase_span(raw[ctx[0]:ctx[1]], c.text)
        if not span:
            continue
        styles = style_runs(el.text_elements_in(cell), raw, ctx[0] + span[0], ctx[0] + span[1])
        bad: list[str] = []
        for st in styles:
            for key, wanted in c.flags:
                if bool(st.get(key)) != wanted:
                    bad.append(f"{key}={st.get(key)}")
            colour = hex_color(part(st.get("foregroundColor"), "foregroundColor").get("opaqueColor"))
            if c.color is not None and colour != c.color.lower():
                bad.append(f"color={colour}")
            size = part(st.get("fontSize"), "fontSize").get("magnitude")
            if c.size is not None and abs(number(-1 if size is None else size, "fontSize") - c.size) > 0.01:
                bad.append(f"size={size}")
        return f"style {sorted(set(bad))}: {what}" if bad or not styles else ""
    return f"text not found for style: {what}"


def _counted(found: list[Element], near: Point | None, tol: float) -> list[Element]:
    return found if near is None else [e for e in found if _near(e, near, tol)]


def _evaluate(model: Model, c: Check, what: str) -> str | None:
    match c:
        case Fresh():
            return None  # compare_fresh
        case SlideOrder(order=order, adjacent=adjacent):
            idx = [model.one(sel).index for sel in order]
            ok = idx == sorted(set(idx)) and (not adjacent or idx == list(range(idx[0], idx[0] + len(idx))))
            return None if ok else f"slides at {idx}: {what}"
        case SlideCount(slide=slide, count=count):
            n = len(model.find(slide))
            return None if n == count else f"{n} slides match: {what}"
        case TextCount(slide=slide, text=text, count=count):
            phrase = norm(text)
            n = sum(norm(t).count(phrase) for s in model.find(slide) for e in s.elements for t, _ in e.texts)
            return None if n == count else f"text found {n} times: {what}"
        case TitleIs(slide=slide, text=text):
            title = model.one(slide).title
            return None if title == norm(text) else f"title is {title!r}: {what}"
        case NotesAre(slide=slide, text=text):
            notes = model.one(slide).notes
            return None if notes == norm(text) else f"notes are {notes!r}: {what}"
        case BackgroundIs(slide=slide, color=color):
            background = model.one(slide).background
            return None if background == color.lower() else f"background is {background}: {what}"
        case StyleIs():
            return _style_problem(model, c, what) or None
        case BoxAt(slide=slide, target=t, origin=origin, size=size, tol=tol):
            el = model.element(model.one(slide), t)
            bad: list[str] = []
            if origin is not None and max(abs(el.box[0] - origin[0]), abs(el.box[1] - origin[1])) > tol:
                bad.append(f"origin {[round(v, 1) for v in el.box[:2]]}")
            have = [el.box[2] - el.box[0], el.box[3] - el.box[1]]
            if size is not None and max(abs(have[0] - size[0]), abs(have[1] - size[1])) > tol:
                bad.append(f"size {[round(v, 1) for v in have]}")
            return f"{', '.join(bad)}: {what}" if bad else None
        case ImageCount(slide=slide, near=near, tol=tol, colour=colour, count=count):
            found = _counted([e for e in model.one(slide).elements if e.kind == "image"], near, tol)
            problems = [(e, image_problem(e, colour)) for e in found]
            if colour:
                found = [e for e, p in problems if p is None]
            elif any(p for _, p in problems):
                return "; ".join(p for _, p in problems if p) + f": {what}"
            return None if len(found) == count else f"{len(found)} images match: {what}"
        case ShapeCount(slide=slide, shape_type=shape_type, color=color, near=near, tol=tol, count=count):
            found = [e for e in model.one(slide).elements if e.kind == "shape" and e.shape_type == shape_type]
            if color is not None:
                found = [e for e in found if hex_color(e.solid_fill.get("color")) == color.lower()]
            found = _counted(found, near, tol)
            return None if len(found) == count else f"{len(found)} shapes match: {what}"
        case TableCount(slide=slide, near=near, tol=tol, count=count):
            found = _counted([e for e in model.one(slide).elements if e.kind == "table"], near, tol)
            return None if len(found) == count else f"{len(found)} tables match: {what}"
        case Grouped(slide=slide, members=members, grouped=grouped):
            s = model.one(slide)
            els = [model.element(s, t) for t in members]
            sets = [set(m.groups) for m in els]
            together = bool(sets[0].intersection(*sets[1:]))
            return None if together == grouped else f"groups {[m.groups for m in els]}: {what}"
        case unreachable:
            assert_never(unreachable)


def check_all(model: Model, checks: Sequence[JsonObject]) -> list[str]:
    return [p for c in checks if (p := evaluate(model, c))]


# ---------------------------------------------------------------- integrity

def groups(model: Model) -> dict[str, list[tuple[str, frozenset[str]]]]:
    """slide title -> [(group id, member descriptors)] of every group."""
    out: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for s in model.slides:
        out[s.title or f"#{s.index}"] = [
            (e.id, frozenset(c.descriptor() for c in s.elements if c.parent == e.id))
            for e in s.elements if e.kind == "group"]
    return out


def is_formula_picture(e: Element) -> bool:
    """A picture that belongs to a text (inline formula, icon, numbered ball): emit's alt text
    title, or the sync tag of an image/math or image/icon element."""
    tag = e.obj.get("title") or ""
    return e.kind == "image" and (tag in ("Formula", "Icon") or bool(re.search(r"/image/(math|icon)/\d+$", tag)))


def on_a_text_line(slide: Slide, e: Element) -> bool:
    """Whether this picture stands on a line of some text on the slide, which is what tells an
    inline formula from a display equation: both are tagged `image/math/N` (role `math` with and
    without an `anchor` in the IR), and only the inline one is grouped with a text box. A picture
    in a line sits between that line's words, so the text's box holds it top and bottom and the
    two overlap left to right; a display equation stands on its own between paragraphs.

    Measured over the 139 math pictures of the test decks, the stress variants and the converted
    demos: all 44 display equations fail this, 90 of the 95 inline ones pass it. The five it lets
    go are math labels drawn beside a graphic rather than in a line of prose (`19_labels_on_graphics`,
    `13_inline_math`) - the error is a check not made, never a deck accused."""
    x0, y0, x1, y1 = e.box
    return any(t.box[1] - 2 <= y0 and y1 <= t.box[3] + 2 and x0 < t.box[2] + 6 and x1 > t.box[0] - 6
               for t in slide.elements if t.kind in ("shape", "table") and t.text and t.id != e.id)


def integrity(model: Model, *, before: Model | None, base_ids: AbstractSet[str] | None,
              allow_groups_changed: AbstractSet[str], allow_ungrouped: AbstractSet[str]) -> list[str]:
    """Duplicates (same kind, text and box twice on a slide), orphans (sync objects the base
    doesn't know, empty text boxes of ours, formula pictures out of their text's group, empty
    groups) and groups of `before` whose members all survived but no longer form a group.
    `allow_ungrouped` / `allow_groups_changed`: slides where a formula picture may stand alone or a
    group may have come apart, because the person did it on purpose. A slide is named by its title
    or by its objectId - by the id when the source may retitle it in the same step, which is how a
    chained fuzz round accused a slide of the group its own `ungroup` edit had dissolved
    (live seed 607, variant `retitle`).
    A picture standing between paragraphs rather than in a line of them is a display equation,
    which belongs to no text and is never asked for a group (`on_a_text_line`)."""
    problems: list[str] = []
    for s in model.slides:
        name = f"slide {s.index + 1} ({s.title})"
        seen: dict[tuple[str, str, tuple[int, ...]], str] = {}
        for e in s.elements:
            if e.kind == "group":
                if not any(c.parent == e.id for c in s.elements):
                    problems.append(f"{name}: empty group {e.id}")
                continue
            key = (e.descriptor(), e.obj.get("description", ""), tuple(round(v * 2) for v in e.box))
            if key in seen:
                problems.append(f"{name}: duplicate {e.descriptor()} {e.id} and {seen[key]}")
            seen[key] = e.id
            if base_ids is not None and e.id.startswith("b2s_") and e.id not in base_ids:
                problems.append(f"{name}: orphan {e.descriptor()} {e.id} (not in the base)")
            if e.kind == "shape" and e.id.startswith("b2s_") and not e.text and \
                    e.shape_type == "TEXT_BOX" and not e.is_placeholder:
                problems.append(f"{name}: empty text box {e.id}")
            if is_formula_picture(e) and not ({s.title, s.id} & set(allow_ungrouped)) and on_a_text_line(s, e):
                if e.parent is None or not any(c.parent == e.parent and c.kind == "shape" for c in s.elements):
                    problems.append(f"{name}: formula picture {e.id} is not grouped with its text")
    if before is not None:
        now = groups(model)
        members_now = {t: {c.descriptor() for c in s.elements} for s in model.slides for t in [s.title or f"#{s.index}"]}
        sids = {s.title or f"#{s.index}": s.id for s in before.slides}
        for title, gs in groups(before).items():
            if {title, sids.get(title)} & set(allow_groups_changed) or title not in now:
                continue
            for gid, members in gs:
                if members <= members_now[title] and not any(members <= m for _, m in now[title]):
                    problems.append(f"slide {title}: group {gid} of {sorted(members)} came apart")
    return problems


def integrity_alone(model: Model) -> list[str]:
    """`integrity` of the deck on its own: no read before to compare groups with, no base to know
    orphans by, nothing excused."""
    return integrity(model, before=None, base_ids=None, allow_groups_changed=frozenset(), allow_ungrouped=frozenset())


def ids_in(data: Json) -> set[str]:
    """Every string in a JSON value (the object ids a base snapshot knows, whatever its layout)."""
    if isinstance(data, str):
        return {data}
    out: set[str] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            out |= {k} | ids_in(v)
    elif isinstance(data, list):
        for v in data:
            out |= ids_in(v)
    return out


# ---------------------------------------------------------------- report

Section = Literal["conflicts", "converged", "overrides", "applied", "slides", "warnings"]

REPORT_SECTIONS: dict[Section, tuple[str, ...]] = {
    "conflicts": ("conflicts",),
    "converged": ("converged", "converged_overrides"),
    "overrides": ("overrides", "deck_overrides", "deck_edits_kept", "kept"),
    "applied": ("applied", "source_changes", "changes_applied"),
    "slides": ("slides_created", "slides_deleted", "slides_moved", "created", "deleted", "moved"),
    "warnings": ("warnings",),
}


def section(report: JsonObject, name: Section) -> list[Json]:
    out: list[Json] = []
    for key in REPORT_SECTIONS[name]:
        value = report.get(key)
        if isinstance(value, list):
            out += value
        elif isinstance(value, dict):
            out += [value] if name != "slides" else [v for vs in value.values() if isinstance(vs, list) for v in vs]
    return out


def changes(report: JsonObject) -> int:
    """How many writes a report lists (applied source changes and slide operations)."""
    return len(section(report, "applied")) + len(section(report, "slides"))


def check_report(report: JsonObject, *, conflicts: Sequence[Sequence[str]], converged: Sequence[Sequence[str]],
                 warnings: Sequence[Sequence[str]], no_conflicts: bool) -> list[str]:
    """Each expected entry is a list of strings one report entry of that section must all contain
    (e.g. both versions of a conflicting text); `no_conflicts`: the report must list none."""
    problems: list[str] = []
    expected: tuple[tuple[Section, Sequence[Sequence[str]]], ...] = (
        ("conflicts", conflicts), ("converged", converged), ("warnings", warnings))
    for name, wanted in expected:
        entries = [json.dumps(e, ensure_ascii=False) for e in section(report, name)]
        for words in wanted:
            if not any(all(norm(w) in norm(e) for w in words) for e in entries):
                problems.append(f"report: no {name} entry with {list(words)}")
    if no_conflicts and section(report, "conflicts"):
        problems.append(f"report: unexpected conflicts {section(report, 'conflicts')}")
    return problems


# ---------------------------------------------------------------- fresh conversion

def _distance(a: Box, b: Box) -> float:
    return max(abs(x - y) for x, y in zip(a, b))


def compare_fresh(synced: Model, ref: Model, titles: Sequence[str]) -> list[str]:
    """Slides (by title) of the synced deck against a fresh conversion of the same source: the
    same elements (kind, text, box within FRESH_TOL), groups, notes and background."""
    problems: list[str] = []
    for title in titles:
        try:
            s, r = synced.one(title), ref.one(title)
        except CheckError as e:
            problems.append(f"fresh: {e}")
            continue
        free = [e for e in s.elements if e.kind != "group"]
        for e in (e for e in r.elements if e.kind != "group"):
            match = [x for x in free if x.descriptor() == e.descriptor() and _distance(x.box, e.box) <= FRESH_TOL]
            if match:
                free.remove(min(match, key=lambda x: _distance(x.box, e.box)))
            else:
                problems.append(f"fresh {title}: missing {e.descriptor()} at {[round(v) for v in e.box]}")
        problems += [f"fresh {title}: extra {x.descriptor()} {x.id} at {[round(v) for v in x.box]}" for x in free]
        gs = sorted(sorted(m) for _, m in groups(synced)[title])
        gr = sorted(sorted(m) for _, m in groups(ref)[title])
        if gs != gr:
            problems.append(f"fresh {title}: groups {gs} != {gr}")
        if s.notes != r.notes:
            problems.append(f"fresh {title}: notes {s.notes!r} != {r.notes!r}")
        if s.background != r.background:
            problems.append(f"fresh {title}: background {s.background} != {r.background}")
    return problems


def thumbnail_diff(slides_api: SlidesService, a: tuple[str, str], b: tuple[str, str], path: Path) -> str | None:
    """Google's thumbnails of two slides ((presentation id, page id)) differ in more than
    THUMBNAIL_LIMIT of their pixels: the problem, with a diff PNG (red: only in a, blue: only in b)."""
    from beamer2slides.gslides import save_thumbnail
    path.parent.mkdir(parents=True, exist_ok=True)
    pa, pb = path.with_name(path.stem + "-synced.png"), path.with_name(path.stem + "-fresh.png")
    save_thumbnail(slides_api, a[0], a[1], pa)
    save_thumbnail(slides_api, b[0], b[1], pb)
    ia = np.asarray(Image.open(pa).convert("RGB")).astype(np.int16)
    ib = np.asarray(Image.open(pb).convert("RGB")).astype(np.int16)
    if ia.shape != ib.shape:
        return f"thumbnails {pa.name} and {pb.name} differ in size"
    differ = np.abs(ia - ib).max(axis=2) > 60
    share = float(differ.mean())
    if share <= THUMBNAIL_LIMIT:
        return None
    diff = np.full(ia.shape, 255, dtype=np.uint8)
    diff[differ & (ia.sum(axis=2) < ib.sum(axis=2))] = (220, 40, 40)
    diff[differ & (ia.sum(axis=2) >= ib.sum(axis=2))] = (30, 110, 230)
    Image.fromarray(diff).save(path)
    return f"thumbnail differs in {share:.2%} of pixels ({path})"


def _closest(candidates: Sequence[Element], box: Box) -> Element | None:
    return min(candidates, key=lambda e: _distance(e.box, box)) if candidates else None


def alignment_compare(ref_out: Path, synced: Model, ref: Model, pid: str, titles: Sequence[str], work: Path) -> list[str]:
    """tools/alignment.py on the synced deck's slides `titles`, against the same measurement of
    the fresh conversion in `ref_out` (holes, numbers, bullets, overlays). The synced objects are
    found by kind, text and box, so the measurement can use the fresh conversion's deck.json. A
    metric may grow by ALIGNMENT_GROWTH past the fresh conversion's."""
    from dataclasses import replace
    from . import alignment
    from beamer2slides import emit_state
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import save_thumbnail

    read = as_object(json.loads((ref_out / "deck.json").read_text(encoding="utf-8")), "deck.json")
    source = as_object(read["source"], "deck.json source")
    state = emit_state.read(ref_out)
    # The measurement folders refer to the fresh conversion's PDF and pictures (alignment keeps the PDF open).
    pdf = ref_out / "slides.pdf" if (ref_out / "slides.pdf").exists() else Path(as_str(source["pdf"], "deck.json source.pdf"))
    deck_slides: list[JsonObject] = [
        {**s, "elements": [{**e, "file": str(ref_out / as_str(e["file"], "element file"))} if e.get("file") else e
                           for e in as_objects(s["elements"], "slide elements")]}
        for s in as_objects(read["slides"], "deck.json slides")]
    deck: JsonObject = {**read, "source": {**source, "pdf": str(pdf)}}
    api = slides_service()
    reports: dict[str, alignment.Alignment] = {}
    for name, model, presentation_ in (("fresh", ref, state.presentation_id), ("synced", synced, pid)):
        folder = work / name
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "fidelity").mkdir(parents=True, exist_ok=True)
        for old in (folder / "fidelity").glob("*.png"):
            old.unlink()
        slides: list[Json] = []
        emitted: list[emit_state.SlideState] = []
        for dslide, eslide in zip(deck_slides, state.slides):
            r = next((s for s in ref.slides if s.id == eslide.object_id), None)
            title = None if r is None else r.title
            if r is None or title is None or title not in titles:
                continue
            on = model.one(title)
            ids: list[str] = []
            for oid in eslide.elements:
                re_el = next((e for e in r.elements if e.id == oid), None)
                best = None if re_el is None else _closest(
                    [e for e in on.elements if e.descriptor() == re_el.descriptor()], re_el.box)
                ids.append(best.id if best else f"missing_{oid}")
            slides.append(dslide)
            emitted.append(emit_state.SlideState(page=eslide.page, object_id=on.id, elements=tuple(ids),
                                                 objects=None, groups=None, table_margins=None))
            page = as_int(dslide["page"], "slide page")
            save_thumbnail(api, presentation_, on.id, folder / "fidelity" / f"slides-{page + 1:03}.png")
        (folder / "deck.json").write_text(json.dumps({**deck, "slides": slides}, ensure_ascii=False), encoding="utf-8")
        emit_state.write(folder, replace(state, presentation_id=presentation_, slides=tuple(emitted)))
        try:
            reports[name] = alignment.measure(folder, refresh=True, crops="failures")
        except Exception as e:  # noqa: BLE001 (a missing object, say)
            return [f"alignment of the {name} deck failed: {e!r}"]
    fresh: dict[str, dict[alignment.Metric, float]] = {
        key: alignment.metrics(row) for _, key, row in alignment.items(reports["fresh"])}
    fresh_fail = {key for _, key, _ in alignment.failures(reports["fresh"])}
    problems: list[str] = []
    for kind, key, row in alignment.items(reports["synced"]):
        for msg in alignment.row_failures(row):
            if key not in fresh_fail:
                problems.append(f"alignment {kind} {key}: {msg} (fresh conversion passes)")
        for metric, value in alignment.metrics(row).items():
            old = fresh.get(key, {}).get(metric)
            if old is not None and value > old + ALIGNMENT_GROWTH:
                problems.append(f"alignment {kind} {key}: {metric} {value} vs {old} in the fresh conversion")
    return problems


# ---------------------------------------------------------------- command line

def presentation_id(deck: str) -> str:
    path = Path(deck)
    if (path / "emit.json").exists():
        state = as_object(json.loads((path / "emit.json").read_text(encoding="utf-8")), "emit.json")
        return as_str(state["presentationId"], "emit.json presentationId")
    m = re.search(r"/presentation/d/([\w-]+)", deck)
    return m.group(1) if m else deck


def read_with(slides_api: SlidesService, pid: str) -> Model:
    """The deck as presentations.get answers it now, through `slides_api`."""
    from beamer2slides.google_types import as_json
    from beamer2slides.gslides import execute
    return Model(as_json(execute(slides_api.presentations().get(presentationId=pid)), pid))


def read(pid: str) -> Model:
    """The deck as presentations.get answers it now, through the owner's own client."""
    from beamer2slides.google_auth import slides_service
    return read_with(slides_service(), pid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="presentation id, URL or converted out folder")
    ap.add_argument("checks", type=Path, help="JSON list of checks or of expectations (with checks)")
    ap.add_argument("--report", type=Path, help="sync-report.json: its conflicts are listed")
    args = ap.parse_args()
    deck: str = args.deck
    checks_file: Path = args.checks
    report_file: Path | None = args.report
    checks: list[JsonObject] = []
    for item in as_objects(json.loads(checks_file.read_text(encoding="utf-8-sig")), checks_file.name):
        checks += as_objects(item["checks"], "expectation checks") if "checks" in item else [item]
    model = read(presentation_id(deck))
    problems = check_all(model, checks) + integrity_alone(model)
    if report_file is not None:
        report = as_object(json.loads(report_file.read_text(encoding="utf-8")), report_file.name)
        for c in section(report, "conflicts"):
            print("conflict:", json.dumps(c, ensure_ascii=False))
    print("\n".join(problems) or f"all {len(checks)} checks hold")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
