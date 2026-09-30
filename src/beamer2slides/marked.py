"""The read-back of a page that says what it is: `adopt`'s slides.sty opens a /B2S marked-content
sequence around every element it draws (its kind, its number on the page and, from slides-keys.tex,
the deck object it came from), a /B2Sp one around each paragraph of a text box (its alignment and
list level), /B2Sb around a list item's bullet, /B2Sc around each table cell, /B2Su and /B2Ss around
underlined and struck words; a shape's says the box it was drawn in, a line's its two points. `extract` carries
those marks into raw.json (`marks` on spans, drawings and images); here they become the page's
elements (docs/adopt-bench.md, 2026-09-26).

A marked element is built from what it drew and nothing else: a text box from its spans, one
paragraph per paragraph mark, its lines by baseline, its styles from the spans (`MarkedText`, the
classifier's own line and run machinery on the element's objects alone); a picture from its images;
a shape from its paths; a table from its cells. Each carries `mark`: the deck object's id when the
keys file named it, else `#<n>`. What no mark holds - the frame title beamer typesets, anything a
person wrote by hand - goes through `classify` as before, on a page that holds only those objects,
so the classifier's guesses can neither swallow a marked element nor merge two of them.

A page without marks never comes here: its read-back is `classify` unchanged.

The slide and the elements are deck.json as JSON (`JsonObject`), as classify writes them: `ir.py`'s
TypedDicts say their shape, but the classifier's own page (`PageClassifier.classify`) is not typed
yet, and the elements here join it."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from .classify import Line, PageClassifier, Paragraph, Rect, label_of, span_runs, union_all
from .classify_model import Span, new_line, new_paragraph
from .ir import Align, run_json, slide_json
from .json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_optional_str, as_str
from .raw_types import RawDrawing, RawImage, RawItem, RawPage, RawSpan
from .typing_compat import override

ELEMENT, PARAGRAPH, BULLET, CELL, UNDERLINE, STRIKE = "B2S", "B2Sp", "B2Sb", "B2Sc", "B2Su", "B2Ss"
KINDS = ("spans", "drawings", "images")
# deck_ir reads a Slides glyph as a number like this
NUMBERED = re.compile(r"\d|[a-z]\.|[ivx]+\.")
FLIP: dict[Align, Align] = {"left": "right", "right": "left"}


MarkParams = dict[str, str | int]
"""A mark's parameters (`raw_types.RawMark`)."""
GroupId = str | int | None
"""A marked element's number on the page (`/n`), else its key (`/k`)."""
MarkValue = str | int | None
"""One parameter of a mark, or none (`MarkParams.get`)."""
Cell = tuple[int, int]
"""A table cell's (row, column)."""


def _nums(xs: Iterable[float]) -> list[Json]:
    return [x for x in xs]


def _strs(xs: Iterable[str]) -> list[Json]:
    return [x for x in xs]


def _objects(xs: Iterable[JsonObject]) -> list[Json]:
    return [x for x in xs]


def _box_json(b: list[float] | None) -> Json:
    return None if b is None else _nums(b)


def params(item: RawItem, tag: str) -> MarkParams | None:
    """The parameters of the outermost `tag` mark around a raw item, None outside any."""
    return next((p for t, p in item.get("marks") or () if t == tag), None)


def element_of(item: RawItem) -> MarkParams | None:
    return params(item, ELEMENT)


def page_items(page: RawPage) -> list[RawItem]:
    """What a mark can be around on a page, kind by kind (`KINDS`). (A test's hand-built page may
    leave a kind out.)"""
    return [*page.get("spans", []), *page.get("drawings", []), *page.get("images", [])]


def has_marks(page: RawPage) -> bool:
    return any(element_of(item) is not None for item in page_items(page))


def group_id(p: MarkParams) -> GroupId:
    return p.get("n", p.get("k"))


def paragraph_index(item: RawItem) -> int | None:
    """The paragraph mark's `/i` around an item (a number: slides.sty writes it so)."""
    i = (params(item, PARAGRAPH) or {}).get("i")
    return i if isinstance(i, int) else None


class Group:
    """One marked element: what it drew, by kind."""

    def __init__(self, n: GroupId, p: MarkParams) -> None:
        key, kind = p.get("k"), p.get("t", "")
        self.n, self.key, self.kind = n, None if key is None else str(key), str(kind)
        self.params = p
        self.spans: list[RawSpan] = []
        self.drawings: list[RawDrawing] = []
        self.images: list[RawImage] = []

    @property
    def mark(self) -> str:
        return self.key or f"#{self.n}"

    def page(self, page: RawPage, drop: frozenset[str]) -> RawPage:
        """The page with this element's objects alone, less those whose ids `drop` holds."""
        return {**page, "spans": [s for s in self.spans if s["id"] not in drop],
                "drawings": [d for d in self.drawings if d["id"] not in drop],
                "images": [i for i in self.images if i["id"] not in drop]}


def typeset_order(g: Group) -> tuple[bool, int, str]:
    return not isinstance(g.n, int), g.n if isinstance(g.n, int) else 0, str(g.n)


def split(page: RawPage) -> tuple[RawPage, list[Group]]:
    """(the page of unmarked objects, the marked elements in the order the page typeset them)."""
    groups: dict[GroupId, Group] = {}

    def group(p: MarkParams) -> Group:  # (its first item's marks say what the element is)
        return groups.setdefault(group_id(p), Group(group_id(p), p))
    spans: list[RawSpan] = []
    drawings: list[RawDrawing] = []
    images: list[RawImage] = []
    for s in page.get("spans", []):
        p = element_of(s)
        if p is None:
            spans.append(s)
        else:
            group(p).spans.append(s)
    for d in page.get("drawings", []):
        p = element_of(d)
        if p is None:
            drawings.append(d)
        else:
            group(p).drawings.append(d)
    for i in page.get("images", []):
        p = element_of(i)
        if p is None:
            images.append(i)
        else:
            group(p).images.append(i)
    for s in page.get("hidden_spans", []):  # (words a picture drawn later covers: `extract`)
        p = element_of(s)
        if p is not None and group_id(p) in groups:
            groups[group_id(p)].spans.append(s)
    return {**page, "spans": spans, "drawings": drawings, "images": images}, sorted(groups.values(), key=typeset_order)


# ---------------------------------------------------------------------------------------------- text

class MarkedText(PageClassifier):
    """The classifier on one marked text box: every line is the box's text (no theme, figure or
    display formula), a paragraph is what one paragraph mark holds, with the alignment and list level
    it says, a bullet what the bullet mark holds, and the box is one box."""

    def __init__(self, page: RawPage, body: float, art: dict[int | None, Rect]) -> None:
        super().__init__(page, body)
        self.para: dict[str, MarkParams] = {s["id"]: params(s, PARAGRAPH) or {} for s in page["spans"]}
        self.par_of = {s["id"]: paragraph_index(s) for s in page["spans"]}
        self.in_bullet = {s["id"] for s in page["spans"] if params(s, BULLET) is not None}
        self.art = art   # paragraph index -> the box of a bullet drawn as a picture or a path

    def par_index(self, s: Span) -> int | None:
        return self.par_of.get(s.id)

    @override
    def build_lines(self, spans: list[Span]) -> list[Line]:
        by: dict[int | None, list[Span]] = {}
        for s in spans:
            by.setdefault(self.par_index(s), []).append(s)
        lines: list[Line] = []
        for ss in by.values():
            lines += same_row(super().build_lines(ss))
        return sorted(lines, key=lambda l: (l.baseline, l.rect.x0))

    @override
    def detect_bullet(self, line: Line) -> None:
        """(the marks say which spans are the bullet: `assign_reasons`)"""

    @override
    def text_decorations(self, spans: list[Span]) -> None:
        """The classifier's rules under and through words, and what the marks say: words `\\uline`
        or `\\sout` set are underlined or struck whatever their rules look like."""
        super().text_decorations(spans)
        for s in spans:
            raw = self.raw_spans.get(s.id)
            if raw is None:
                continue
            if params(raw, UNDERLINE) is not None:
                s.underline = True
            if params(raw, STRIKE) is not None:
                s.strike = True

    @override
    def assign_reasons(self, lines: list[Line]) -> None:
        super().assign_reasons(lines)
        limits = {id(l) for o in lines for l in o.limits}  # a formula's limits go into its hole
        firsts: dict[int | None, Line] = {}
        for line in sorted(lines, key=lambda l: l.baseline):
            if line.reason == "rotated" or id(line) in limits:
                continue
            no_spans: list[Span] = []
            line.reason, line.bullet, line.bullet_spans = None, None, no_spans
            i = self.line_par(line)
            first = i not in firsts
            firsts.setdefault(i, line)
            marker = [s for s in line.spans if s.id in self.in_bullet]
            if marker and len(marker) < len(line.spans):
                line.tab = None
                marker.sort(key=lambda s: s.rect.x0)
                token = "".join(s.text.strip() for s in marker)
                box = union_all(s.rect for s in marker).as_list()
                if NUMBERED.search(token):
                    line.bullet = {"kind": "number", "text": token, "color": marker[0].color, "bbox": box,
                                   "label": label_of(marker)}
                else:
                    line.bullet = {"kind": "glyph", "text": token, "color": marker[0].color, "bbox": box,
                                   "label": label_of(marker)}
                line.bullet_spans = marker
            elif first and i in self.art:
                line.bullet = {"kind": "glyph", "text": "", "bbox": self.art[i].as_list(), "drawn": True}

    def line_par(self, line: Line) -> int | None:
        return Counter(self.par_index(s) for s in line.spans).most_common(1)[0][0]

    @staticmethod
    @override
    def display_pieces_apart(paragraphs: list[Paragraph]) -> list[Paragraph]:
        return paragraphs

    @override
    def build_paragraphs(self, lines: list[Line]) -> list[Paragraph]:
        self.hfill_hosts = set()
        by: dict[int | None, list[Line]] = {}
        for line in lines:
            if line.reason is None:
                by.setdefault(self.line_par(line), []).append(line)
        out: list[Paragraph] = []
        for i, ls in sorted(by.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            ls.sort(key=lambda l: l.baseline)
            par = new_paragraph(ls, align="left", reason=None)
            p = self.para.get(ls[0].spans[0].id, {})
            a = p.get("a", "left")
            par.justified = a == "justify"
            par.align = "center" if a == "center" else "right" if a == "right" else "left"
            if par.direction == "rtl":            # LuaTeX's skips are logical (adopt.box_latex)
                par.align = FLIP.get(par.align, par.align)
            par.level = max(0, int(p["l"]) - 1) if p.get("l") else 0
            if par.align == "left" and len(ls) > 1 and not par.bullet:
                par.indent = max(0.0, ls[0].x0 - min(l.x0 for l in ls[1:]))
                par.indent = par.indent if par.indent > 0.5 else 0.0
            if par.size >= 1.15 * self.body and par.rect.y0 < 0.2 * self.H:
                par.role = "title"
            out.append(par)
        if out and any(p.role == "title" for p in out):
            for p in out:
                p.role = "title" if out[0].role == "title" else p.role
        return out

    @override
    def build_boxes(self, paragraphs: list[Paragraph]) -> list[list[Paragraph]]:
        return [paragraphs] if paragraphs else []


def same_row(lines: list[Line]) -> list[Line]:
    """Pieces of one paragraph on one baseline are one line (a TeX paragraph's line is one line,
    however wide its spaces: justified, or a word set apart by a tab stop)."""
    out: list[Line] = []
    for line in sorted(lines, key=lambda l: (l.baseline, l.rect.x0)):
        prev = next((o for o in out if abs(o.baseline - line.baseline) <= 0.3 * max(o.size, line.size)
                     and min(o.rect.y1, line.rect.y1) > max(o.rect.y0, line.rect.y0)), None)
        if prev is None:
            out.append(line)
        else:
            out[out.index(prev)] = new_line(prev.spans + line.spans)
    return out


def unturned(sub: RawPage) -> float | None:
    """A text box Slides turned (adopt's \\adoptturned): its words set back level about the middle
    of their ink, as they stand in the deck's own unturned box - the classifier reads level lines
    only. Rewrites `sub`'s spans in place; the angle turned (degrees, clockwise on the page as
    Slides counts it), or None for a level box."""
    spans = sub["spans"]
    turned = [s for s in spans if abs(s["dir"][1]) > 0.01]
    if not turned:
        return None
    dx, dy = turned[0]["dir"]
    a = math.atan2(dy, dx)
    if any(abs(math.atan2(s["dir"][1], s["dir"][0]) - a) > 0.02 for s in spans) or abs(a) >= math.pi / 4:
        return None  # (not one box turned by less than 45 degrees: left as it reads)
    box = union_all(Rect.of(s["bbox"]) for s in spans)
    cx, cy = box.cx, box.cy
    c, sn = math.cos(a), math.sin(a)

    def back(x: float, y: float) -> tuple[float, float]:
        return cx + (x - cx) * c + (y - cy) * sn, cy - (x - cx) * sn + (y - cy) * c
    out: list[RawSpan] = []
    for s in spans:
        x0, y0, x1, y1 = s["bbox"]
        W, H = x1 - x0, y1 - y0
        den = c * c - sn * sn
        w = max(0.1, (W * c - H * abs(sn)) / den)
        h = max(0.1, (H * c - W * abs(sn)) / den)
        ox, oy = back(s["origin"][0], s["origin"][1])
        descent = 0.22 * h
        out.append({**s, "dir": [1.0, 0.0], "origin": [ox, oy], "bbox": [ox, oy - h + descent, ox + w, oy + descent]})
    sub["spans"] = out
    return round(math.degrees(a), 2)


def span_ids(e: JsonObject, where: str) -> list[str]:
    """An element's `spans`, none when it names none."""
    ids = e.get("spans")
    return [] if ids is None else [as_str(i, f"{where}: spans") for i in as_array(ids, f"{where}: spans")]


def text_elements(g: Group, page: RawPage, body: float) -> tuple[list[JsonObject], JsonObject]:
    """A marked text box: its text element (with `mark`) and the pictures that go with it (formula
    holes, icon bullets); the rest of the sub-classification (what it left in the background)."""
    art_items: list[RawDrawing | RawImage] = [i for i in [*g.drawings, *g.images] if params(i, BULLET) is not None]
    art: dict[int | None, Rect] = {}
    for i in art_items:
        k = paragraph_index(i)
        art[k] = art[k].union(Rect.of(i["bbox"])) if k in art else Rect.of(i["bbox"])
    sub = g.page(page, frozenset(i["id"] for i in art_items))
    turn = unturned(sub)
    res = slide_json(MarkedText(sub, body, art).classify())
    elements = as_objects(res["elements"], f"marked box {g.mark}: elements")
    if turn is not None:
        for e in elements:
            e["rotation"] = turn
    texts = [e for e in elements if e["kind"] == "text"]
    hidden = {s["id"] for s in page.get("hidden_spans", [])}
    for e in elements:
        # words the page hides are the box's words, but no object of the page's text: they stay
        # out of `spans` (render erases what `spans` names)
        ids = span_ids(e, f"marked box {g.mark}")
        if hidden.intersection(ids):
            e["hidden_spans"] = _strs(i for i in ids if i in hidden)
            e["spans"] = _strs(i for i in ids if i not in hidden)
    if not texts:
        return [], res
    main = max(texts, key=lambda e: len(as_array(e["spans"], f"marked box {g.mark}: spans")))
    main["mark"] = g.mark
    box = box_of(g.params.get("box"))
    if box:
        # the box adopt set the words in: `\slidetext` puts it at the target's text anchor, `\slidebox`
        # at the target's box, so it is data for a reader who knows which, not the target's bbox
        main["mark_box"] = _nums(box)
    ids = {as_str(e["id"], f"marked box {g.mark}: id") for e in texts}
    keep = texts + [e for e in elements if e.get("anchor") in ids]
    return keep, res


# ---------------------------------------------------------------------------------- pictures, shapes

def union_box(items: Sequence[RawItem], grow: float) -> list[float] | None:
    rects = [Rect.of(i["bbox"]).expand(grow) for i in items]
    return union_all(rects).as_list() if rects else None


def picture_element(g: Group, pid: str) -> JsonObject:
    ims, ds, ss = g.images, g.drawings, g.spans
    bbox = union_box(ims, 0.0) or union_box(ds, 0.5) or union_box(ss, 0.0)
    el: JsonObject = {"id": pid, "kind": "image", "role": "figure", "bbox": _box_json(bbox),
                      "spans": _strs(s["id"] for s in ss), "mark": g.mark, "mark_n": g.n}
    if len(ims) == 1 and not ds and not ss:
        el["image"] = ims[0]["id"]  # the picture is the image itself (classify.bare_image)
    return el


def filled(d: RawDrawing) -> bool:
    return bool(d.get("fill")) and "f" in d["type"]


def area(d: RawDrawing) -> float:
    x0, y0, x1, y1 = d["bbox"]
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def outline_json(stroke: RawDrawing | None) -> JsonObject | None:
    return {"color": stroke["stroke"], "width": stroke.get("width") or 1.0} if stroke else None


def shape_element(g: Group, pid: str) -> JsonObject:
    ds, ims = g.drawings, g.images
    fills = sorted((d for d in ds if filled(d)), key=area, reverse=True)
    strokes = [d for d in ds if "s" in d["type"]]
    main = fills[0] if fills else (max(strokes, key=lambda d: sum(d["bbox"][2:]) - sum(d["bbox"][:2])) if strokes else None)
    # a line: an open stroke, its arrow heads (small, filled) the only other paths
    shafts = [d for d in strokes if not filled(d) and "re" not in d["items"]]
    reach = max((math.hypot(d["bbox"][2] - d["bbox"][0], d["bbox"][3] - d["bbox"][1]) for d in shafts), default=0.0)
    line = bool(shafts) and not ims and all(
        max(d["bbox"][2] - d["bbox"][0], d["bbox"][3] - d["bbox"][1]) <= max(0.25 * reach, 12.0) for d in fills)
    ends = points_of(g.params.get("line"))
    bbox: list[float] | None
    if ends:
        # `\slideline` says its two points: the line runs to them, to its arrows' tips (the shaft
        # TikZ draws stops at a head's base)
        line = True
        (ax, ay), (bx, by) = ends
        bbox = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
    elif line:
        bbox = union_box(shafts, 0.0)
    elif fills or ims:
        bbox = union_box([*fills, *ims], 0.0)
        if strokes and not fills:
            bbox = union_box([*strokes, *ims], 0.0)
    else:
        bbox = union_box(ds, 0.0) or union_box(g.spans, 0.0)
    box = None if line else box_of(g.params.get("box"))
    if box and bbox and box[2] - box[0] > 0.5 and box[3] - box[1] > 0.5:
        # The box adopt drew the shape in, unless what it drew stands out of it (a shape turned):
        # a curve's ink falls short of its box, the page's edge cuts one off, a shadow shows it only
        # in part; none of them is another box.
        reach = max([d.get("width") or 0.0 for d in ds] + [0.0]) / 2 + 1.5
        if Rect.of(box).expand(reach).contains_rect(Rect.of(bbox), tol=0.5):
            bbox = box
    stroke = next((d for d in strokes if d.get("stroke")), None)
    el: JsonObject = {"id": pid, "kind": "shape", "role": "line" if line else "panel", "bbox": _box_json(bbox),
                      "fill": None if line or not fills else fills[0]["fill"],
                      "outline": outline_json(stroke),
                      "shape": "line" if line else "RECTANGLE" if main and main["items"] == "re" else "custom",
                      "flip": False, "radius": 0.0,
                      "drawings": _strs(d["id"] for d in ds), "spans": _strs(s["id"] for s in g.spans), "mark": g.mark}
    if ims and not fills:
        el["picture"] = _strs(i["id"] for i in ims)  # a picture fill
    if fills and fills[0].get("fill_opacity", 1) < 0.99:
        el["opacity"] = fills[0]["fill_opacity"]
    return el


def drawn_natively(el: JsonObject) -> bool:
    """Whether emit has a Slides shape for a marked shape: a filled rectangle. A line (emit draws
    lines only inside diagrams), a `\\slidepath` or `\\slidefreeform` outline (`custom`: no preset
    has its points), a picture fill or an outline alone has none."""
    return el["shape"] == "RECTANGLE" and bool(el.get("fill")) and not el.get("picture")


def pictured_shapes(slide: JsonObject, raw_page: RawPage, keep: frozenset[str]) -> None:
    """In place: each marked shape emit has no Slides shape for becomes the picture of what its
    mark draws, where it stands in the drawing order (an adopted deck's freeform, uploaded again,
    raised KeyError 'custom' in the .pptx's template shapes). Only a conversion does this
    (`render.render_backgrounds`): compare pairs the read-back's shapes with the deck's, kind for
    kind, and `adopt_sync.kind_fit` lets a picture stand for a deck's shape.

    `keep`: marks that stay shapes, those an adopt base written before this recorded as shapes
    (`shape_marks`). Sync pairs a base's element with ours kind for kind, so the same mark as a
    picture read as the source removing the person's freeform and adding a picture: an unchanged
    source deleted four of china's freeforms and stacked pictures on them (audit, 2026-09-29)."""
    drawings = {d["id"]: d for d in raw_page["drawings"]}
    images = {i["id"]: i for i in raw_page["images"]}
    elements = as_array(slide["elements"], "slide: elements")
    for k, item in enumerate(elements):
        el = as_object(item, f"slide: elements[{k}]")
        mark = el.get("mark")
        if el["kind"] != "shape" or not mark or drawn_natively(el) or mark in keep:
            continue
        where = f"slide: elements[{k}]"
        # (a stroke's ink reaches half its width past its path)
        rects = [Rect.of(d["bbox"]).expand(max(0.5, (d.get("width") or 0.0) / 2))
                 for d in (drawings.get(as_str(i, f"{where}: drawings")) for i in as_array(el.get("drawings", []), where))
                 if d] + \
                [Rect.of(images[i]["bbox"]) for i in (as_str(p, f"{where}: picture") for p in as_array(el.get("picture", []), where))
                 if i in images]
        picture: JsonObject = {"id": el["id"], "kind": "image", "role": "figure",
                               "bbox": _nums(union_all(rects).as_list()) if rects else el["bbox"],
                               "spans": el.get("spans", []), "mark": el["mark"]}
        elements[k] = picture


def shape_marks(base: JsonObject) -> frozenset[str]:
    """The marks a sync base records as shapes: what `pictured_shapes` keeps shapes for that base's
    deck. Only an adopt base has marks; one written since 688ebf4 has its pictured ones as images."""
    if base.get("adopt") is None:
        return frozenset()
    marks: list[str] = []
    for s in as_objects(base.get("slides", []), "base: slides"):
        for e in as_objects(s.get("elements", []), "base: slide elements"):
            if e.get("kind") != "shape":
                continue
            ir = e.get("ir")
            mark = as_object(ir, "base: element ir").get("mark") if ir else None
            if mark:
                marks.append(as_str(mark, "base: element ir: mark"))
    return frozenset(marks)


# ---------------------------------------------------------------------------------------------- tables

def table_element(g: Group, page: RawPage, body: float, pid: str) -> JsonObject:
    """A marked table: its grid from its own mark, each cell's words from what that cell's mark
    holds, and the layout emit writes it by (`table_layout`) from its mark's grid, its cells' words
    and what it drew (`table_grid`)."""
    rows, cols = int(g.params.get("rows") or 0), int(g.params.get("cols") or 0)
    c = PageClassifier(g.page(page, frozenset()), body)
    spans = c.spans()
    cells: dict[Cell, list[Span]] = {}
    spanning: dict[Cell, Cell] = {}
    for s, raw in zip(spans, c.raw["spans"]):
        p = params(raw, CELL)
        if p is not None:
            rc = (int(p.get("r", 0)), int(p.get("c", 0)))
            cells.setdefault(rc, []).append(s)
            spanning[rc] = (int(p.get("rs") or 1), int(p.get("cs") or 1))
    rows = max([rows] + [r + 1 for r, _ in cells])
    cols = max([cols] + [k + 1 for _, k in cells])
    grid: list[list[Json]] = [[[] for _ in range(cols)] for _ in range(rows)]
    for (r, k), ss in cells.items():
        lines = sorted(c.build_lines(ss), key=lambda l: (l.baseline, l.rect.x0))
        runs: list[Json] = []
        for line in lines:
            if runs:
                runs.append({**as_object(runs[-1], "table cell run"), "text": " ", "script": None})
            runs += [run_json(run) for run in span_runs(line.spans)]
        grid[r][k] = runs
    bbox = box_of(g.params.get("box")) or union_box([*g.drawings, *g.spans], 0.0)
    el: JsonObject = {"id": pid, "kind": "table", "role": "table", "bbox": _box_json(bbox),
                      "cells": [row for row in grid],
                      "spans": _strs(s["id"] for s in g.spans), "drawings": _strs(d["id"] for d in g.drawings),
                      "mark": g.mark}
    if bbox:  # (a table of empty cells too: emit reads its columns, KeyError 'columns' - audit)
        el.update(table_grid(g, bbox, rows, cols, cells, spanning, body))
    return el


def spread(text: MarkValue, n: int, start: float) -> list[float] | None:
    """A mark's `/xs` or `/ys`: n + 1 edges, each from `start` (bp, or pt when it says so)."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != n + 1:
        return None
    return [start + float(v) * (72 / 72.27 if unit == "pt" else 1.0) for v, unit in parts]


def split_between(extents: list[tuple[float, float] | None], lo: float, hi: float) -> list[float]:
    """Edges between runs of text, midway between one's end and the next one's start, `lo` and
    `hi` outside; beside a run with no text, evenly between the edges known on either side."""
    n = len(extents)
    edges: list[float | None] = [lo] + [None] * (n - 1) + [hi]
    for i in range(1, n):
        a, b = extents[i - 1], extents[i]
        if a and b:
            edges[i] = (a[1] + b[0]) / 2 if b[0] >= a[1] else a[1]
    i = 1
    while i < n:
        if edges[i] is None:
            j = next(j for j in range(i, n + 1) if edges[j] is not None)
            before, after = edges[i - 1], edges[j]
            # (the edges are filled left to right: the one before a gap is known)
            assert before is not None and after is not None
            for k in range(i, j):
                edges[k] = before + (after - before) * (k - i + 1) / (j - i + 1)
            i = j
        i += 1
    return [e for e in edges if e is not None]


def merge_json(r: int, c: int, rs: int, cs: int, align: str) -> JsonObject:
    return {"row": r, "col": c, "rows": rs, "cols": cs, "align": align}


def table_grid(g: Group, bbox: list[float], rows: int, cols: int, cells: dict[Cell, list[Span]],
               spanning: dict[Cell, Cell], body: float) -> JsonObject:
    """What emit lays a marked table out by, classify's table fields: column `bounds` and row tops
    (`bands`, which place each row) from the mark's `/xs` and `/ys` (a source adopt wrote before
    they were said: from where the cells' words stand), per column its words' extent and
    alignment, per row its first baseline, and the fills and borders it drew, by the cell they
    stand on; `merges` from each cell's `/rs` and `/cs`."""
    x0, y0, x1, y1 = bbox
    single = {rc: ss for rc, ss in cells.items() if spanning.get(rc, (1, 1)) == (1, 1)}

    def extent(ss: list[Span] | None, across: bool) -> tuple[float, float] | None:
        """Where words run: across the page (x), else down it (y)."""
        if not ss:
            return None
        if across:
            return min(s.rect.x0 for s in ss), max(s.rect.x1 for s in ss)
        return min(s.rect.y0 for s in ss), max(s.rect.y1 for s in ss)
    xs = spread(g.params.get("xs"), cols, x0) or split_between(
        [extent([s for (r, k), ss in single.items() if k == c for s in ss], True) for c in range(cols)], x0, x1)
    ys = spread(g.params.get("ys"), rows, y0) or split_between(
        [extent([s for (r, k), ss in single.items() if r == i for s in ss], False) for i in range(rows)], y0, y1)
    sizes = Counter(round(s.size, 1) for ss in cells.values() for s in ss)
    size = sizes.most_common(1)[0][0] if sizes else body
    def align(ss: list[Span], lo: float, hi: float) -> str:  # one cell's words between its edges
        left, right = min(s.rect.x0 for s in ss) - lo, hi - max(s.rect.x1 for s in ss)
        return "center" if abs(left - right) <= max(1.5, 0.15 * (left + right)) and left > 3 else \
            "right" if right < left else "left"

    def words(ss: list[Span] | None, lo: float, hi: float) -> list[float]:  # (no words: a padding in from its edges)
        a, b = extent(ss, True) or (lo + 0.3 * size, hi - 0.3 * size)
        return [round(a, 2), round(b, 2)]
    columns: list[JsonObject] = []
    aligns: list[str] = []
    for c in range(cols):
        mine = [ss for (r, k), ss in single.items() if k == c and ss]
        votes = Counter(align(ss, xs[c], xs[c + 1]) for ss in mine)
        a, b = words([s for ss in mine for s in ss], xs[c], xs[c + 1])
        aligns.append(votes.most_common(1)[0][0] if votes else "left")
        columns.append({"x0": a, "x1": b, "align": aligns[-1]})
    baselines: list[float] = []
    for i in range(rows):
        firsts = [min(s.baseline for s in ss) for (r, k), ss in cells.items() if r == i]
        baselines.append(round(min(firsts) if firsts else ys[i] + size, 2))
    heights = [round(ys[i + 1] - ys[i], 2) for i in range(rows)]

    def at(v: float, edges: list[float]) -> int:
        return max(0, min(len(edges) - 2, sum(1 for e in edges[1:-1] if v >= e)))
    fills: list[JsonObject] = []
    borders: list[JsonObject] = []
    for d in g.drawings:
        dx0, dy0, dx1, dy1 = d["bbox"]
        if filled(d) and d.get("fill_opacity", 1) >= 0.99:  # (the cell its corner stands in: a merge's first)
            fills.append({"row": at(dy0 + min(4.0, (dy1 - dy0) / 4), ys), "col": at(dx0 + min(4.0, (dx1 - dx0) / 4), xs),
                          "color": d["fill"]})
        elif "s" in d["type"] and d.get("stroke"):
            w = d.get("width") or 1.0
            if dx1 - dx0 >= dy1 - dy0:  # a row edge: every column it runs along
                k = min(range(rows + 1), key=lambda i: abs(ys[i] - (dy0 + dy1) / 2))
                for c in range(cols):
                    if dx0 - 1 <= (xs[c] + xs[c + 1]) / 2 <= dx1 + 1:
                        borders.append({"row": min(k, rows - 1), "col": c, "position": "TOP" if k < rows else "BOTTOM",
                                        "color": d["stroke"], "weight": w, "y": round(ys[k], 2)})
            else:
                k = min(range(cols + 1), key=lambda i: abs(xs[i] - (dx0 + dx1) / 2))
                for r in range(rows):
                    if dy0 - 1 <= (ys[r] + ys[r + 1]) / 2 <= dy1 + 1:
                        borders.append({"row": r, "col": min(k, cols - 1), "position": "LEFT" if k < cols else "RIGHT",
                                        "color": d["stroke"], "weight": w})
    # A merged cell is aligned by its own words across the columns it spans, as classify's are
    # (`place_cells`): its first column's alignment wrote a centred spanning head START (audit).
    spans_of = [((r, c), (rs, cs)) for (r, c), (rs, cs) in sorted(spanning.items()) if (rs, cs) != (1, 1)]
    merges = [merge_json(r, c, rs, cs, align(cells[(r, c)], xs[c], xs[min(c + cs, cols)]) if cells.get((r, c)) else aligns[c])
              for (r, c), (rs, cs) in spans_of]
    grid: JsonObject = {
        "frame": _nums(round(v, 2) for v in (xs[0], ys[0], xs[-1], ys[-1])), "size": size,
        "row_baselines": _nums(baselines), "row_heights": _nums(heights), "columns": _objects(columns),
        "bounds": _nums(round(v, 2) for v in xs), "bands": [_nums((i, round(ys[i], 2), round(ys[i + 1], 2))) for i in range(rows)],
        "merges": _objects(merges)}
    if merges:
        grid["merge_x"] = [_nums(words(cells.get((r, c)), xs[c], xs[min(c + cs, cols)])) for (r, c), (rs, cs) in spans_of]
    grid.update({"rules": [], "borders": _objects(borders), "fills": _objects(fills)})
    return grid


BOX_PART = re.compile(r"(-?\d*\.?\d+)\s*(bp|pt)?")


def box_of(text: MarkValue) -> list[float] | None:
    """A mark's `/box (x y w h)`: the box adopt put the element in, from the page's top left, each
    in bp unless it says pt (TeX points: a dimension TeX computed)."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != 4:
        return None
    x, y, w, h = (float(v) * (72 / 72.27 if unit == "pt" else 1.0) for v, unit in parts)
    return [round(x, 2), round(y, 2), round(x + w, 2), round(y + h, 2)]


def points_of(text: MarkValue) -> list[tuple[float, float]] | None:
    """A line mark's `/line (x1 y1 x2 y2)`: its two points, bp from the page's top left."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != 4:
        return None
    x1, y1, x2, y2 = (float(v) * (72 / 72.27 if unit == "pt" else 1.0) for v, unit in parts)
    return [(x1, y1), (x2, y2)]


# ----------------------------------------------------------------------------------------------- page

def fold_parts(elements: list[JsonObject]) -> list[JsonObject]:
    """An element's other calls (`adopt.piece_keys`: `<key>+shape`, a text box's panel) are that
    element's: a text box takes its panel's fill, and neither is an element of its own."""
    keyed: dict[str, JsonObject] = {}
    for e in elements:
        mark = e.get("mark")
        if mark:
            keyed[as_str(mark, "marked element: mark")] = e
    out: list[JsonObject] = []
    for e in elements:
        base, plus, _ = (as_optional_str(e.get("mark"), "marked element: mark") or "").partition("+")
        owner = keyed.get(base) if plus else None
        if owner is None or owner is e:
            out.append(e)
            continue
        part: JsonObject = {k: e[k] for k in ("kind", "bbox", "mark") if k in e}
        as_array(owner.setdefault("parts", []), "marked element: parts").append(part)
        if owner["kind"] == "text" and e["kind"] == "shape" and e.get("fill"):
            owner["fill"] = e["fill"]
    return out


def classify_marked(page: RawPage, body: float) -> JsonObject:
    """A marked page's slide, in `classify_page`'s shape."""
    rest, groups = split(page)
    out = slide_json(PageClassifier(rest, body).classify())
    left = as_array(out["left_in_background"], "page: left_in_background")
    n = page["index"]
    marked: list[JsonObject] = []
    native_chars = 0
    for g in groups:
        pid = f"p{n}m{g.n}"
        if g.kind == "text":
            els, res = text_elements(g, page, body)
            for e in as_objects(res["elements"], f"marked box {g.mark}: elements"):
                # ids unique on the page: the mark's number in each
                e["id"] = f"{as_str(e['id'], f'marked box {g.mark}: id')}m{g.n}"
                anchor = e.get("anchor")
                if isinstance(anchor, str):
                    e["anchor"] = f"{anchor}m{g.n}"
            marked += els
            left += as_array(res["left_in_background"], f"marked box {g.mark}: left_in_background")
            native_chars += as_int(as_object(res["stats"], f"marked box {g.mark}: stats")["chars_native"],
                                   f"marked box {g.mark}: chars_native")
        elif g.kind == "picture":
            marked.append(picture_element(g, pid))
        elif g.kind == "table":
            marked.append(table_element(g, page, body, pid))
            native_chars += sum(len(s["text"].strip()) for s in g.spans)
        else:
            marked.append(shape_element(g, pid))
    as_array(out["elements"], "page: elements").extend(fold_parts(marked))
    out["stats"] = {"chars": sum(len(s["text"].strip()) for s in page["spans"]),
                    "chars_native": as_int(as_object(out["stats"], "page: stats")["chars_native"], "page: chars_native")
                    + native_chars}
    out["marked"] = len(groups)
    return out
