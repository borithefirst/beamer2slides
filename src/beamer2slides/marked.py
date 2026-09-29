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

A page without marks never comes here: its read-back is `classify` unchanged."""

from __future__ import annotations

import math
import re
from collections import Counter

from .classify import Line, PageClassifier, Paragraph, Rect, label_of, span_runs, union_all

ELEMENT, PARAGRAPH, BULLET, CELL, UNDERLINE, STRIKE = "B2S", "B2Sp", "B2Sb", "B2Sc", "B2Su", "B2Ss"
KINDS = ("spans", "drawings", "images")
# deck_ir reads a Slides glyph as a number like this
NUMBERED = re.compile(r"\d|[a-z]\.|[ivx]+\.")
FLIP = {"left": "right", "right": "left"}


def params(item: dict, tag: str) -> dict | None:
    """The parameters of the outermost `tag` mark around a raw item, None outside any."""
    return next((p for t, p in item.get("marks") or () if t == tag), None)


def element_of(item: dict) -> dict | None:
    return params(item, ELEMENT)


def has_marks(page: dict) -> bool:
    return any(element_of(item) is not None for kind in KINDS for item in page.get(kind, ()))


def group_id(p: dict):
    return p.get("n", p.get("k"))


class Group:
    """One marked element: what it drew, by kind."""

    def __init__(self, n, p: dict):
        self.n, self.key, self.kind = n, p.get("k"), p.get("t", "")
        self.params = p
        self.items = {kind: [] for kind in KINDS}

    @property
    def mark(self) -> str:
        return self.key or f"#{self.n}"

    def page(self, page: dict, drop=None) -> dict:
        """The page with this element's objects alone (less those `drop` says)."""
        keep = lambda i: not (drop and drop(i))
        return {**page, **{kind: [i for i in self.items[kind] if keep(i)] for kind in KINDS}}


def split(page: dict) -> tuple[dict, list[Group]]:
    """(the page of unmarked objects, the marked elements in the order the page typeset them)."""
    groups: dict = {}
    rest = {kind: [] for kind in KINDS}
    for kind in KINDS:
        for item in page.get(kind, ()):
            p = element_of(item)
            if p is None:
                rest[kind].append(item)
                continue
            g = groups.setdefault(group_id(p), Group(group_id(p), p))
            g.items[kind].append(item)
    for item in page.get("hidden_spans", ()):  # (words a picture drawn later covers: `extract`)
        p = element_of(item)
        if p is not None and group_id(p) in groups:
            groups[group_id(p)].items["spans"].append(item)
    order = sorted(groups.values(), key=lambda g: (not isinstance(g.n, int), g.n if isinstance(g.n, int) else 0, str(g.n)))
    return {**page, **rest}, order


# ---------------------------------------------------------------------------------------------- text

class MarkedText(PageClassifier):
    """The classifier on one marked text box: every line is the box's text (no theme, figure or
    display formula), a paragraph is what one paragraph mark holds, with the alignment and list level
    it says, a bullet what the bullet mark holds, and the box is one box."""

    def __init__(self, page: dict, body: float, art: dict | None = None):
        super().__init__(page, body)
        self.para = {s["id"]: params(s, PARAGRAPH) or {} for s in page["spans"]}
        self.in_bullet = {s["id"] for s in page["spans"] if params(s, BULLET) is not None}
        self.art = art or {}   # paragraph index -> the box of a bullet drawn as a picture or a path

    def par_index(self, s) -> int | None:
        return self.para.get(s.id, {}).get("i")

    def build_lines(self, spans):
        by: dict = {}
        for s in spans:
            by.setdefault(self.par_index(s), []).append(s)
        lines = []
        for ss in by.values():
            lines += same_row(super().build_lines(ss))
        return sorted(lines, key=lambda l: (l.baseline, l.rect.x0))

    def detect_bullet(self, line):
        """(the marks say which spans are the bullet: `assign_reasons`)"""

    def text_decorations(self, spans):
        """The classifier's rules under and through words, and what the marks say: words `\\uline`
        or `\\sout` set are underlined or struck whatever their rules look like."""
        super().text_decorations(spans)
        for s in spans:
            raw = self._raw_spans.get(s.id) or {}
            if params(raw, UNDERLINE) is not None:
                s.underline = True
            if params(raw, STRIKE) is not None:
                s.strike = True

    def assign_reasons(self, lines):
        super().assign_reasons(lines)
        limits = {id(l) for o in lines for l in o.limits}  # a formula's limits go into its hole
        firsts: dict = {}
        for line in sorted(lines, key=lambda l: l.baseline):
            if line.reason == "rotated" or id(line) in limits:
                continue
            line.reason, line.bullet, line.bullet_spans = None, None, []
            i = self.line_par(line)
            first = i not in firsts
            firsts.setdefault(i, line)
            marker = [s for s in line.spans if s.id in self.in_bullet]
            if marker and len(marker) < len(line.spans):
                line.tab = None
                marker.sort(key=lambda s: s.rect.x0)
                token = "".join(s.text.strip() for s in marker)
                line.bullet = {"kind": "number" if NUMBERED.search(token) else "glyph", "text": token,
                               "color": marker[0].color, "bbox": union_all(s.rect for s in marker).as_list(),
                               "label": label_of(marker)}
                line.bullet_spans = marker
            elif first and i in self.art:
                line.bullet = {"kind": "glyph", "text": "", "bbox": self.art[i].as_list(), "drawn": True}

    def line_par(self, line) -> int | None:
        return Counter(self.par_index(s) for s in line.spans).most_common(1)[0][0]

    def display_pieces_apart(self, paragraphs):
        return paragraphs

    def build_paragraphs(self, lines):
        self.hfill_hosts = set()
        by: dict = {}
        for line in lines:
            if line.reason is None:
                by.setdefault(self.line_par(line), []).append(line)
        out = []
        for i, ls in sorted(by.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            ls.sort(key=lambda l: l.baseline)
            par = Paragraph(ls)
            p = self.para.get(ls[0].spans[0].id, {})
            a = p.get("a", "left")
            par.justified = a == "justify"
            par.align = a if a in ("center", "right") else "left"
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

    def build_boxes(self, paragraphs):
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
            out[out.index(prev)] = Line(prev.spans + line.spans)
    return out


def unturned(sub: dict) -> float | None:
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
    back = lambda x, y: (cx + (x - cx) * c + (y - cy) * sn, cy - (x - cx) * sn + (y - cy) * c)
    out = []
    for s in spans:
        x0, y0, x1, y1 = s["bbox"]
        W, H = x1 - x0, y1 - y0
        den = c * c - sn * sn
        w = max(0.1, (W * c - H * abs(sn)) / den)
        h = max(0.1, (H * c - W * abs(sn)) / den)
        ox, oy = back(*s["origin"])
        descent = 0.22 * h
        out.append({**s, "dir": [1.0, 0.0], "origin": [ox, oy], "bbox": [ox, oy - h + descent, ox + w, oy + descent]})
    sub["spans"] = out
    return round(math.degrees(a), 2)


def text_elements(g: Group, page: dict, body: float) -> tuple[list[dict], dict]:
    """A marked text box: its text element (with `mark`) and the pictures that go with it (formula
    holes, icon bullets); the rest of the sub-classification (what it left in the background)."""
    art_items = [i for kind in ("drawings", "images") for i in g.items[kind] if params(i, BULLET) is not None]
    art: dict = {}
    for i in art_items:
        k = (params(i, PARAGRAPH) or {}).get("i")
        art[k] = art[k].union(Rect.of(i["bbox"])) if k in art else Rect.of(i["bbox"])
    sub = g.page(page, drop=lambda i: i in art_items)
    turn = unturned(sub)
    res = MarkedText(sub, body, art).classify()
    if turn is not None:
        for e in res["elements"]:
            e["rotation"] = turn
    texts = [e for e in res["elements"] if e["kind"] == "text"]
    hidden = {s["id"] for s in page.get("hidden_spans", ())}
    for e in res["elements"]:
        # words the page hides are the box's words, but no object of the page's text: they stay
        # out of `spans` (render erases what `spans` names)
        if hidden.intersection(e.get("spans", ())):
            e["hidden_spans"] = [i for i in e["spans"] if i in hidden]
            e["spans"] = [i for i in e["spans"] if i not in hidden]
    if not texts:
        return [], res
    main = max(texts, key=lambda e: len(e["spans"]))
    main["mark"] = g.mark
    box = box_of(g.params.get("box"))
    if box:
        # the box adopt set the words in: `\slidetext` puts it at the target's text anchor, `\slidebox`
        # at the target's box, so it is data for a reader who knows which, not the target's bbox
        main["mark_box"] = box
    ids = {e["id"] for e in texts}
    keep = texts + [e for e in res["elements"] if e.get("anchor") in ids]
    return keep, res


# ---------------------------------------------------------------------------------- pictures, shapes

def union_box(items: list[dict], grow: float = 0.0) -> list[float] | None:
    rects = [Rect.of(i["bbox"]).expand(grow) for i in items]
    return union_all(rects).as_list() if rects else None


def picture_element(g: Group, pid: str) -> dict:
    ims, ds, ss = g.items["images"], g.items["drawings"], g.items["spans"]
    bbox = union_box(ims) or union_box(ds, 0.5) or union_box(ss)
    el = {"id": pid, "kind": "image", "role": "figure", "bbox": bbox, "spans": [s["id"] for s in ss],
          "mark": g.mark, "mark_n": g.n}
    if len(ims) == 1 and not ds and not ss:
        el["image"] = ims[0]["id"]  # the picture is the image itself (classify.bare_image)
    return el


def filled(d: dict) -> bool:
    return bool(d.get("fill")) and "f" in d["type"]


def area(d: dict) -> float:
    x0, y0, x1, y1 = d["bbox"]
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def shape_element(g: Group, pid: str) -> dict:
    ds, ims = g.items["drawings"], g.items["images"]
    fills = sorted((d for d in ds if filled(d)), key=area, reverse=True)
    strokes = [d for d in ds if "s" in d["type"]]
    main = fills[0] if fills else (max(strokes, key=lambda d: sum(d["bbox"][2:]) - sum(d["bbox"][:2])) if strokes else None)
    # a line: an open stroke, its arrow heads (small, filled) the only other paths
    shafts = [d for d in strokes if not filled(d) and "re" not in d["items"]]
    reach = max((math.hypot(d["bbox"][2] - d["bbox"][0], d["bbox"][3] - d["bbox"][1]) for d in shafts), default=0.0)
    line = bool(shafts) and not ims and all(
        max(d["bbox"][2] - d["bbox"][0], d["bbox"][3] - d["bbox"][1]) <= max(0.25 * reach, 12.0) for d in fills)
    ends = points_of(g.params.get("line"))
    if ends:
        # `\slideline` says its two points: the line runs to them, to its arrows' tips (the shaft
        # TikZ draws stops at a head's base)
        line = True
        (ax, ay), (bx, by) = ends
        bbox = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
    elif line:
        bbox = union_box(shafts)
    elif fills or ims:
        bbox = union_box(fills + ims)
        if strokes and not fills:
            bbox = union_box(strokes + ims)
    else:
        bbox = union_box(ds) or union_box(g.items["spans"])
    box = None if line else box_of(g.params.get("box"))
    if box and bbox and box[2] - box[0] > 0.5 and box[3] - box[1] > 0.5:
        # The box adopt drew the shape in, unless what it drew stands out of it (a shape turned):
        # a curve's ink falls short of its box, the page's edge cuts one off, a shadow shows it only
        # in part; none of them is another box.
        reach = max([d.get("width") or 0.0 for d in ds] + [0.0]) / 2 + 1.5
        if Rect.of(box).expand(reach).contains_rect(Rect.of(bbox)):
            bbox = box
    stroke = next((d for d in strokes if d.get("stroke")), None)
    el = {"id": pid, "kind": "shape", "role": "line" if line else "panel", "bbox": bbox,
          "fill": None if line or not fills else fills[0]["fill"],
          "outline": {"color": stroke["stroke"], "width": stroke.get("width") or 1.0} if stroke else None,
          "shape": "line" if line else "RECTANGLE" if main and main["items"] == "re" else "custom",
          "flip": False, "radius": 0.0,
          "drawings": [d["id"] for d in ds], "spans": [s["id"] for s in g.items["spans"]], "mark": g.mark}
    if ims and not fills:
        el["picture"] = [i["id"] for i in ims]  # a picture fill
    if fills and fills[0].get("fill_opacity", 1) < 0.99:
        el["opacity"] = fills[0]["fill_opacity"]
    return el


def drawn_natively(el: dict) -> bool:
    """Whether emit has a Slides shape for a marked shape: a filled rectangle. A line (emit draws
    lines only inside diagrams), a `\\slidepath` or `\\slidefreeform` outline (`custom`: no preset
    has its points), a picture fill or an outline alone has none."""
    return el["shape"] == "RECTANGLE" and bool(el.get("fill")) and not el.get("picture")


def pictured_shapes(slide: dict, raw_page: dict) -> None:
    """In place: each marked shape emit has no Slides shape for becomes the picture of what its
    mark draws, where it stands in the drawing order (an adopted deck's freeform, uploaded again,
    raised KeyError 'custom' in the .pptx's template shapes). Only a conversion does this
    (`render.render_backgrounds`): compare pairs the read-back's shapes with the deck's, kind for
    kind, and `adopt_sync.kind_fit` lets a picture stand for a deck's shape."""
    drawings = {d["id"]: d for d in raw_page["drawings"]}
    images = {i["id"]: i for i in raw_page["images"]}
    for k, el in enumerate(slide["elements"]):
        if el["kind"] != "shape" or not el.get("mark") or drawn_natively(el):
            continue
        # (a stroke's ink reaches half its width past its path)
        rects = [Rect.of(d["bbox"]).expand(max(0.5, (d.get("width") or 0.0) / 2))
                 for d in (drawings.get(i) for i in el.get("drawings", ())) if d] + \
                [Rect.of(images[i]["bbox"]) for i in el.get("picture", ()) if i in images]
        slide["elements"][k] = {"id": el["id"], "kind": "image", "role": "figure",
                                "bbox": union_all(rects).as_list() if rects else el["bbox"],
                                "spans": el.get("spans", []), "mark": el["mark"]}


# ---------------------------------------------------------------------------------------------- tables

def table_element(g: Group, page: dict, body: float, pid: str) -> dict:
    """A marked table: its grid from its own mark, each cell's words from what that cell's mark
    holds, and the layout emit writes it by (`table_layout`) from its mark's grid, its cells' words
    and what it drew (`table_grid`)."""
    rows, cols = int(g.params.get("rows") or 0), int(g.params.get("cols") or 0)
    c = PageClassifier(g.page(page), body)
    spans = c.spans()
    cells: dict = {}
    spanning: dict = {}
    for s, raw in zip(spans, c.page["spans"]):
        p = params(raw, CELL)
        if p is not None:
            rc = (int(p.get("r", 0)), int(p.get("c", 0)))
            cells.setdefault(rc, []).append(s)
            spanning[rc] = (int(p.get("rs") or 1), int(p.get("cs") or 1))
    rows = max([rows] + [r + 1 for r, _ in cells])
    cols = max([cols] + [k + 1 for _, k in cells])
    grid = [[[] for _ in range(cols)] for _ in range(rows)]
    for (r, k), ss in cells.items():
        lines = sorted(c.build_lines(ss), key=lambda l: (l.baseline, l.rect.x0))
        runs: list[dict] = []
        for line in lines:
            if runs:
                runs.append({**runs[-1], "text": " ", "script": None})
            runs += span_runs(line.spans)
        grid[r][k] = runs
    bbox = box_of(g.params.get("box")) or union_box(g.items["drawings"] + g.items["spans"])
    el = {"id": pid, "kind": "table", "role": "table", "bbox": bbox, "cells": grid,
          "spans": [s["id"] for s in g.items["spans"]], "drawings": [d["id"] for d in g.items["drawings"]],
          "mark": g.mark}
    if bbox and spans:
        el.update(table_grid(g, bbox, rows, cols, cells, spanning, body))
    return el


def spread(text, n: int, start: float, scale: float = 1.0) -> list[float] | None:
    """A mark's `/xs` or `/ys`: n + 1 edges, each from `start` (bp, or pt when it says so)."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != n + 1:
        return None
    return [start + float(v) * (72 / 72.27 if unit == "pt" else scale) for v, unit in parts]


def split_between(extents: list[tuple[float, float] | None], lo: float, hi: float) -> list[float]:
    """Edges between runs of text, midway between one's end and the next one's start, `lo` and
    `hi` outside; beside a run with no text, evenly between the edges known on either side."""
    n = len(extents)
    edges: list = [lo] + [None] * (n - 1) + [hi]
    for i in range(1, n):
        a, b = extents[i - 1], extents[i]
        if a and b:
            edges[i] = (a[1] + b[0]) / 2 if b[0] >= a[1] else a[1]
    i = 1
    while i < n:
        if edges[i] is None:
            j = next(j for j in range(i, n + 1) if edges[j] is not None)
            for k in range(i, j):
                edges[k] = edges[i - 1] + (edges[j] - edges[i - 1]) * (k - i + 1) / (j - i + 1)
            i = j
        i += 1
    return edges


def table_grid(g: Group, bbox: list[float], rows: int, cols: int, cells: dict, spanning: dict,
               body: float) -> dict:
    """What emit lays a marked table out by, classify's table fields: column `bounds` and row tops
    (`bands`, which place each row) from the mark's `/xs` and `/ys` (a source adopt wrote before
    they were said: from where the cells' words stand), per column its words' extent and
    alignment, per row its first baseline, and the fills and borders it drew, by the cell they
    stand on; `merges` from each cell's `/rs` and `/cs`."""
    x0, y0, x1, y1 = bbox
    single = {rc: ss for rc, ss in cells.items() if spanning.get(rc, (1, 1)) == (1, 1)}

    def extent(ss, lo, hi):
        return (min(getattr(s.rect, lo) for s in ss), max(getattr(s.rect, hi) for s in ss)) if ss else None
    xs = spread(g.params.get("xs"), cols, x0) or split_between(
        [extent([s for (r, k), ss in single.items() if k == c for s in ss], "x0", "x1") for c in range(cols)], x0, x1)
    ys = spread(g.params.get("ys"), rows, y0) or split_between(
        [extent([s for (r, k), ss in single.items() if r == i for s in ss], "y0", "y1") for i in range(rows)], y0, y1)
    sizes = Counter(round(s.size, 1) for ss in cells.values() for s in ss)
    size = sizes.most_common(1)[0][0] if sizes else body
    columns = []
    for c in range(cols):
        mine = [ss for (r, k), ss in single.items() if k == c]
        ext = extent([s for ss in mine for s in ss], "x0", "x1")
        votes = Counter()
        for ss in mine:
            left, right = min(s.rect.x0 for s in ss) - xs[c], xs[c + 1] - max(s.rect.x1 for s in ss)
            votes["center" if abs(left - right) <= max(1.5, 0.15 * (left + right)) and left > 3 else
                  "right" if right < left else "left"] += 1
        a, b = ext or (xs[c] + 0.3 * size, xs[c + 1] - 0.3 * size)
        columns.append({"x0": round(a, 2), "x1": round(b, 2), "align": votes.most_common(1)[0][0] if votes else "left"})
    baselines = []
    for i in range(rows):
        firsts = [min(s.baseline for s in ss) for (r, k), ss in cells.items() if r == i]
        baselines.append(round(min(firsts) if firsts else ys[i] + size, 2))
    heights = [round(ys[i + 1] - ys[i], 2) for i in range(rows)]

    def at(v: float, edges: list[float]) -> int:
        return max(0, min(len(edges) - 2, sum(1 for e in edges[1:-1] if v >= e)))
    fills, borders = [], []
    for d in g.items["drawings"]:
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
    merges = [{"row": r, "col": c, "rows": rs, "cols": cs, "align": columns[c]["align"]}
              for (r, c), (rs, cs) in sorted(spanning.items()) if (rs, cs) != (1, 1)]
    return {"frame": [round(v, 2) for v in (xs[0], ys[0], xs[-1], ys[-1])], "size": size,
            "row_baselines": baselines, "row_heights": heights, "columns": columns,
            "bounds": [round(v, 2) for v in xs], "bands": [[i, round(ys[i], 2), round(ys[i + 1], 2)] for i in range(rows)],
            "merges": merges, "rules": [], "borders": borders, "fills": fills}


BOX_PART = re.compile(r"(-?\d*\.?\d+)\s*(bp|pt)?")


def box_of(text) -> list[float] | None:
    """A mark's `/box (x y w h)`: the box adopt put the element in, from the page's top left, each
    in bp unless it says pt (TeX points: a dimension TeX computed)."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != 4:
        return None
    x, y, w, h = (float(v) * (72 / 72.27 if unit == "pt" else 1.0) for v, unit in parts)
    return [round(x, 2), round(y, 2), round(x + w, 2), round(y + h, 2)]


def points_of(text) -> list[tuple[float, float]] | None:
    """A line mark's `/line (x1 y1 x2 y2)`: its two points, bp from the page's top left."""
    parts = BOX_PART.findall(str(text or ""))
    if len(parts) != 4:
        return None
    x1, y1, x2, y2 = (float(v) * (72 / 72.27 if unit == "pt" else 1.0) for v, unit in parts)
    return [(x1, y1), (x2, y2)]


# ----------------------------------------------------------------------------------------------- page

def fold_parts(elements: list[dict]) -> list[dict]:
    """An element's other calls (`adopt.piece_keys`: `<key>+shape`, a text box's panel) are that
    element's: a text box takes its panel's fill, and neither is an element of its own."""
    keyed = {e["mark"]: e for e in elements if e.get("mark")}
    out = []
    for e in elements:
        base, plus, _ = (e.get("mark") or "").partition("+")
        owner = keyed.get(base) if plus else None
        if owner is None or owner is e:
            out.append(e)
            continue
        owner.setdefault("parts", []).append({k: e[k] for k in ("kind", "bbox", "mark") if k in e})
        if owner["kind"] == "text" and e["kind"] == "shape" and e.get("fill"):
            owner["fill"] = e["fill"]
    return out


def classify_marked(page: dict, body: float) -> dict:
    """A marked page's slide, in `classify_page`'s shape."""
    rest, groups = split(page)
    out = PageClassifier(rest, body).classify()
    n = page["index"]
    marked: list[dict] = []
    native_chars = 0
    for g in groups:
        pid = f"p{n}m{g.n}"
        if g.kind == "text":
            els, res = text_elements(g, page, body)
            for e in res["elements"]:           # ids unique on the page: the mark's number in each
                e["id"] = f"{e['id']}m{g.n}"
                if isinstance(e.get("anchor"), str):
                    e["anchor"] = f"{e['anchor']}m{g.n}"
            marked += els
            out["left_in_background"] += res["left_in_background"]
            native_chars += res["stats"]["chars_native"]
        elif g.kind == "picture":
            marked.append(picture_element(g, pid))
        elif g.kind == "table":
            marked.append(table_element(g, page, body, pid))
            native_chars += sum(len(s["text"].strip()) for s in g.items["spans"])
        else:
            marked.append(shape_element(g, pid))
    out["elements"] += fold_parts(marked)
    out["stats"] = {"chars": sum(len(s["text"].strip()) for s in page["spans"]),
                    "chars_native": out["stats"]["chars_native"] + native_chars}
    out["marked"] = len(groups)
    return out
