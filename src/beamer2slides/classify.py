"""Stage 2: turn raw page content into native text boxes plus background leftovers (deck.json).

Pipeline per page:
  drawings/images -> panels (theme bars, blocks), figure regions, short bars, small images
  spans           -> lines (baseline clustering, scripts attached)
  lines           -> reasons: rotated | figure | theme | math, and bullets
  lines           -> paragraphs (continuation, alignment, TeX paragraph-break test)
  paragraphs      -> text boxes (same panel, same column, close vertically)
Every span ends up in exactly one text element or in `left_in_background`.
"""

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .fonts import FontInfo, font_info

BULLET_GLYPHS = set("▶►▸‣•◦▪■□○●★⋆✓∗–")
ENUM_RE = re.compile(r"^(\(?\d{1,2}[.)]|\(?[a-z][.)]|\([a-z]\)|\(?[ivx]{1,4}[.)])$")
EQ_NUMBER_RE = re.compile(r"^\(\d+(\.\d+)*[a-z]?\)$")
MATH_OPERATORS = set("=+−<>≤≥×·/∑∏∫∈∉⊂⊆∪∩→←⇒⇔≈≠±∞")
SMALL_IMAGE_PT = 12


# ---------------------------------------------------------------- geometry

@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, values) -> "Rect":
        return cls(*values)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def expand(self, d: float) -> "Rect":
        return Rect(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def intersects(self, o: "Rect") -> bool:
        return self.x0 < o.x1 and o.x0 < self.x1 and self.y0 < o.y1 and o.y0 < self.y1

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def contains_rect(self, o: "Rect", tol: float = 0.5) -> bool:
        return self.x0 - tol <= o.x0 and self.y0 - tol <= o.y0 and o.x1 <= self.x1 + tol and o.y1 <= self.y1 + tol

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.x0, o.x0), min(self.y0, o.y0), max(self.x1, o.x1), max(self.y1, o.y1))

    def distance(self, o: "Rect") -> float:
        dx = max(0.0, o.x0 - self.x1, self.x0 - o.x1)
        dy = max(0.0, o.y0 - self.y1, self.y0 - o.y1)
        return max(dx, dy)

    def as_list(self) -> list[float]:
        return [round(v, 2) for v in (self.x0, self.y0, self.x1, self.y1)]


def union_all(rects) -> Rect:
    rects = list(rects)
    out = rects[0]
    for r in rects[1:]:
        out = out.union(r)
    return out


def cluster_rects(rects: list[Rect], gap: float) -> list[Rect]:
    """Merge rectangles that come within `gap` of each other, transitively."""
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if rects[i].expand(gap).intersects(rects[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[Rect]] = {}
    for i, r in enumerate(rects):
        groups.setdefault(find(i), []).append(r)
    return [union_all(g) for g in groups.values()]


# ---------------------------------------------------------------- text model

@dataclass(eq=False)
class Span:
    id: str
    text: str
    font: str
    size: float
    color: str
    rect: Rect
    baseline: float
    horizontal: bool
    info: FontInfo
    link: str | None = None


@dataclass(eq=False)
class Line:
    spans: list[Span]
    bullet: dict | None = None
    bullet_spans: list[Span] = field(default_factory=list)
    reason: str | None = None

    def __post_init__(self):
        self.spans.sort(key=lambda s: s.rect.x0)

    @property
    def rect(self) -> Rect:
        return union_all(s.rect for s in self.spans)

    @property
    def main(self) -> Span:
        top = max(s.size for s in self.spans)
        return max((s for s in self.spans if s.size >= 0.9 * top), key=lambda s: len(s.text.strip()))

    @property
    def baseline(self) -> float:
        return self.main.baseline

    @property
    def size(self) -> float:
        return self.main.size

    @property
    def content(self) -> list[Span]:
        return [s for s in self.spans if s not in self.bullet_spans]

    @property
    def x0(self) -> float:
        return min(s.rect.x0 for s in self.content)

    @property
    def x1(self) -> float:
        return max(s.rect.x1 for s in self.content)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.spans)


@dataclass(eq=False)
class Paragraph:
    lines: list[Line]
    align: str = "left"
    reason: str | None = None
    role: str = "body"
    level: int = 0

    @property
    def first(self) -> Line:
        return self.lines[0]

    @property
    def last(self) -> Line:
        return self.lines[-1]

    @property
    def size(self) -> float:
        return self.first.size

    @property
    def bullet(self) -> dict | None:
        return self.first.bullet

    @property
    def x0(self) -> float:
        return self.first.x0

    @property
    def rect(self) -> Rect:
        return union_all(l.rect for l in self.lines)

    @property
    def spans(self) -> list[Span]:
        return [s for l in self.lines for s in l.spans]


def is_mono(spans: list[Span]) -> bool:
    return bool(spans) and all(s.info.family == "mono" for s in spans)


def code_indent(par: Paragraph, box_x0: float) -> str:
    """Leading spaces that reproduce a code line's indentation (monospace advance per char)."""
    span = max(par.first.content, key=lambda s: len(s.text))
    advance = span.rect.w / max(1, len(span.text))
    return " " * max(0, round((par.x0 - box_x0) / advance))


# ---------------------------------------------------------------- document-level statistics

def body_size(raw: dict) -> float:
    counts = Counter()
    for page in raw["pages"]:
        for s in page["spans"]:
            if font_info(s["font"]).family != "math":
                counts[round(s["size"], 1)] += len(s["text"].strip())
    return counts.most_common(1)[0][0] if counts else 10.0


# ---------------------------------------------------------------- page analysis

class PageClassifier:
    def __init__(self, page: dict, body: float):
        self.page = page
        self.body = body
        self.W, self.H = page["size"]
        self.panels: list[dict] = []
        self.regions: list[Rect] = []
        self.bars: list[Rect] = []
        self.small_images: list[tuple[dict, Rect]] = []
        self.leftovers: list[dict] = []

    # -- graphics -------------------------------------------------------------

    def analyse_graphics(self) -> None:
        graphics = []
        rules: dict[tuple[int, int], list[Rect]] = {}
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if r.w * r.h >= 0.95 * self.W * self.H:
                continue  # page background
            fill_only = d["type"] == "f" and set(d["items"]) <= set("relcq")
            if fill_only and r.w >= 0.25 * self.W and r.h >= 3:
                self.panels.append({"bbox": r, "fill": d["fill"], "id": d["id"],
                                    "rounded": "c" in d["items"]})
            elif d["type"] == "s" and r.h <= 1.0 and r.w <= 3 * self.body and set(d["items"]) <= {"l"}:
                self.bars.append(r)  # fraction bars, radical overbars
            else:
                graphics.append(r)
                if d["type"] == "s" and r.h <= 1.0:
                    rules.setdefault((round(r.x0), round(r.x1)), []).append(r)
        # Two or more horizontal rules of equal extent frame a table: the whole span is one figure.
        for group in rules.values():
            if len(group) >= 2:
                graphics.append(union_all(group))
        for im in self.page["images"]:
            r = Rect.of(im["bbox"])
            if min(r.w, r.h) < SMALL_IMAGE_PT:
                self.small_images.append((im, r))  # bullets, block shadows
            elif r.w >= 0.6 * self.W:
                self.panels.append({"bbox": r, "fill": None, "id": im["id"], "rounded": False})
            else:
                graphics.append(r)
        self.regions = cluster_rects(graphics, gap=3.0) if graphics else []

    def panel_of(self, r: Rect) -> int | None:
        """Innermost panel containing the rect's centre."""
        best = None
        for i, p in enumerate(self.panels):
            if p["bbox"].contains(r.cx, r.cy) and (best is None or p["bbox"].w * p["bbox"].h <
                                                    self.panels[best]["bbox"].w * self.panels[best]["bbox"].h):
                best = i
        return best

    # -- lines ----------------------------------------------------------------

    def spans(self) -> list[Span]:
        links = [(Rect.of(l["bbox"]), l["uri"]) for l in self.page["links"]]
        out = []
        for s in self.page["spans"]:
            r = Rect.of(s["bbox"])
            dx, dy = s["dir"]
            span = Span(s["id"], s["text"], s["font"].split("+", 1)[-1], s["size"], s["color"], r,
                        s["origin"][1], abs(dy) < 0.01 and dx > 0, font_info(s["font"]))
            span.link = next((uri for lr, uri in links if lr.contains(r.cx, r.cy)), None)
            out.append(span)
        return out

    def build_lines(self, spans: list[Span]) -> list[Line]:
        n = len(spans)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            a = spans[i]
            for j in range(i + 1, n):
                b = spans[j]
                big = max(a.size, b.size)
                same_row = abs(a.baseline - b.baseline) <= 0.5 * big and \
                    min(a.rect.y1, b.rect.y1) > max(a.rect.y0, b.rect.y0)
                gap = max(0.0, b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1)
                if same_row and gap <= 2.0 * big:
                    parent[find(i)] = find(j)
        groups: dict[int, list[Span]] = {}
        for i, s in enumerate(spans):
            groups.setdefault(find(i), []).append(s)
        return sorted((Line(g) for g in groups.values()), key=lambda l: (l.baseline, l.rect.x0))

    # -- line reasons -----------------------------------------------------------

    def detect_bullet(self, line: Line) -> None:
        spans = line.spans
        if len(spans) < 2:
            first = None
        else:
            first, nxt = spans[0], spans[1]
            gap = nxt.rect.x0 - first.rect.x1
            token = first.text.strip()
            if gap >= 0.25 * line.size and (token in BULLET_GLYPHS or ENUM_RE.match(token)):
                kind = "glyph" if token in BULLET_GLYPHS else "number"
                line.bullet = {"kind": kind, "text": token, "color": first.color, "bbox": first.rect.as_list()}
                line.bullet_spans = [first]
                return
        # Image bullets (ball themes): a small image just left of the text, level with its
        # x-height. Numbered balls draw the digit as a small text span on top of the image.
        for im, ir in self.small_images:
            on_image = [s for s in spans if ir.expand(0.5).contains(s.rect.cx, s.rect.cy)]
            rest = [s for s in spans if s not in on_image]
            if not rest:
                continue
            x0 = min(s.rect.x0 for s in rest)
            if ir.x1 <= x0 + 0.5 and x0 - ir.x1 <= 1.5 * line.size and \
                    line.baseline - 0.9 * line.size <= ir.cy <= line.baseline + 0.1 * line.size:
                token = "".join(s.text.strip() for s in on_image)
                line.bullet = {"kind": "image", "image": im["id"], "text": token, "bbox": ir.as_list()}
                line.bullet_spans = on_image
                return

    def is_math(self, line: Line) -> bool:
        spans = line.content
        if any(s.info.family == "math" for s in spans) or "�" in line.text:
            return True
        if any(s.size < 0.85 * line.size and abs(s.baseline - line.baseline) > 0.12 * line.size for s in spans):
            return True  # sub/superscripts
        if any(b.expand(1).intersects(line.rect) for b in self.bars):
            return True
        chars = "".join(s.text for s in spans).replace(" ", "")
        mathy = sum(len(s.text.strip()) for s in spans if s.info.italic and len(s.text.strip()) <= 2)
        mathy += sum(ch in MATH_OPERATORS for ch in chars)
        return len(chars) > 0 and mathy / len(chars) >= 0.4

    def assign_reasons(self, lines: list[Line]) -> None:
        for line in lines:
            if not all(s.horizontal for s in line.spans):
                line.reason = "rotated"
            elif any(reg.expand(1).contains(s.rect.cx, s.rect.cy) for s in line.spans for reg in self.regions):
                line.reason = "figure"
            elif line.size <= 0.7 * self.body and (line.rect.y1 <= 0.13 * self.H or line.rect.y0 >= 0.87 * self.H):
                line.reason = "theme"

        # Short labels next to figures (axis ticks, axis labels) belong to the figure.
        regions = list(self.regions)
        changed = True
        while changed:
            changed = False
            for line in lines:
                if line.reason is None and len(line.text.replace(" ", "")) <= 12 and \
                        any(reg.distance(line.rect) <= 0.8 * line.size for reg in regions):
                    line.reason = "figure"
                    regions.append(line.rect)
                    changed = True

        for line in lines:
            if line.reason is None:
                self.detect_bullet(line)
                if self.is_math(line):
                    line.reason = "math"

        # Pieces of display math: limits, equation numbers, small italic fragments next to math.
        changed = True
        while changed:
            changed = False
            maths = [l for l in lines if l.reason == "math"]
            for line in lines:
                if line.reason is not None or line.bullet:
                    continue
                txt = line.text.replace(" ", "")
                near = any(m.rect.expand(0.6 * line.size).intersects(line.rect) for m in maths)
                small = line.size < 0.9 * self.body or all(s.info.italic for s in line.content)
                eqno = EQ_NUMBER_RE.match(txt) and any(abs(m.baseline - line.baseline) <= 3 for m in maths)
                if (near and small and len(txt) <= 6) or eqno:
                    line.reason = "math"
                    changed = True

    # -- paragraphs -------------------------------------------------------------

    def continues(self, par: Paragraph, line: Line) -> str | None:
        """How `line` continues `par` ('left' | 'center' | 'right'), or None."""
        last = par.last
        if line.bullet or abs(line.size - par.size) > 0.5:
            return None
        if is_mono(line.content) or is_mono(last.content):
            return None  # code: every line is its own paragraph
        pitch = line.baseline - last.baseline
        if not 0 < pitch <= 1.35 * par.size:
            return None
        if self.panel_of(line.rect) != self.panel_of(par.rect):
            return None
        left = abs(line.x0 - par.x0) <= 1.5
        right = abs(line.x1 - last.x1) <= 1.5
        center = abs((line.x0 + line.x1) / 2 - (last.x0 + last.x1) / 2) <= 1.5
        if left:
            # TeX would have pulled the next word up if it fitted: then this is a new paragraph.
            col_right = max([l.x1 for l in par.lines] + [line.x1])
            first_word = line.content[0].rect.w
            if not right and last.x1 + 0.3 * par.size + first_word < col_right - 0.5:
                return None
            return "left"
        if center and par.align in ("left", "center") and len(par.lines) == 1 or par.align == "center" and center:
            return "center"
        if right:
            return "right"
        return None

    def single_line_align(self, line: Line, margin: float) -> str:
        if abs(line.rect.cx - self.W / 2) <= 2 and line.x0 > 0.12 * self.W:
            return "center"
        if abs(line.x1 - (self.W - margin)) <= 2 and line.x0 > self.W / 2:
            return "right"
        return "left"

    def build_paragraphs(self, lines: list[Line]) -> list[Paragraph]:
        paragraphs: list[Paragraph] = []
        for line in lines:
            if line.reason not in (None, "math"):
                continue
            best = None
            for par in reversed(paragraphs):
                how = self.continues(par, line)
                if how:
                    best = (par, how)
                    break
            if best:
                par, how = best
                par.lines.append(line)
                par.align = how
                if line.reason == "math":
                    par.reason = "math"
            else:
                paragraphs.append(Paragraph([line], reason=line.reason))
        if not paragraphs:
            return paragraphs

        body_lines = [p.first for p in paragraphs if abs(p.size - self.body) < 1]
        margin = min((l.x0 for l in body_lines), default=0.08 * self.W)
        for par in paragraphs:
            if len(par.lines) == 1:
                par.align = self.single_line_align(par.first, margin)
            if any(l.reason == "math" for l in par.lines):
                par.reason = "math"
            if par.size >= 1.15 * self.body and par.rect.y0 < 0.2 * self.H:
                par.role = "title"
        titles = [p for p in paragraphs if p.role == "title"]
        for par in paragraphs:
            if par.role == "body" and par.rect.y0 < 0.2 * self.H and par.size < self.body + 0.5 and \
                    any(0 < par.first.baseline - t.last.baseline <= 2 * t.size for t in titles):
                par.role = "subtitle"
        return paragraphs

    # -- boxes ------------------------------------------------------------------

    def joins_box(self, box: list[Paragraph], par: Paragraph, blockers: list[Paragraph]) -> bool:
        last = box[-1]
        head = box[0]
        if par.role != head.role or par.align != head.align:
            return False
        if self.panel_of(par.rect) != self.panel_of(head.rect):
            return False
        gap = par.first.baseline - last.last.baseline
        if not 0 < gap <= 2.6 * max(par.size, last.size):
            return False
        box_rect = union_all(p.rect for p in box)
        if is_mono(par.spans) and all(is_mono(p.spans) for p in box):
            # Code block: indentation varies freely, lines follow at normal pitch.
            return par.rect.x0 >= box_rect.x0 - 1.5 and gap <= 1.35 * par.size
        if par.align == "center":
            if abs(par.rect.cx - box_rect.cx) > 2:
                return False
        else:
            # A nested item's bullet starts after its parent's text start, but not much further.
            x = par.rect.x0
            if not (box_rect.x0 - 1.5 <= x <= max(p.x0 for p in box) + 2 * par.size):
                return False
            if not (par.rect.x0 < box_rect.x1 and box_rect.x0 < par.rect.x1):
                return False
        between = Rect(box_rect.x0, last.last.baseline + 0.1, box_rect.x1, par.first.baseline - par.size)
        return not any(b.rect.intersects(between) for b in blockers)

    def build_boxes(self, paragraphs: list[Paragraph]) -> list[list[Paragraph]]:
        native = [p for p in paragraphs if p.reason is None]
        blockers = [p for p in paragraphs if p.reason is not None]
        boxes: list[list[Paragraph]] = []
        for par in sorted(native, key=lambda p: (p.first.baseline, p.x0)):
            for box in reversed(boxes):
                if self.joins_box(box, par, blockers):
                    box.append(par)
                    break
            else:
                boxes.append([par])
        for box in boxes:
            xs = sorted({round(Rect.of(p.bullet["bbox"]).x0, 0) for p in box if p.bullet})
            levels: list[float] = []
            for x in xs:
                if not levels or x - levels[-1] > 2:
                    levels.append(x)
            for p in box:
                if p.bullet:
                    bx = Rect.of(p.bullet["bbox"]).x0
                    p.level = min(range(len(levels)), key=lambda i: abs(levels[i] - bx))
        return boxes

    # -- output -----------------------------------------------------------------

    @staticmethod
    def runs(par: Paragraph, indent: str = "") -> list[dict]:
        runs: list[dict] = []
        prev: Span | None = None
        for li, line in enumerate(par.lines):
            for si, span in enumerate(line.content):
                text = span.text
                if prev is not None:
                    if si == 0:
                        tail = runs[-1]["text"]
                        if len(tail) >= 2 and tail.endswith("-") and tail[-2].isalpha() and text[:1].islower():
                            runs[-1]["text"] = tail[:-1]  # TeX hyphenation at a line break
                            sep = ""
                        else:
                            sep = " "
                    else:
                        sep = " " if span.rect.x0 - prev.rect.x1 > 0.15 * line.size else ""
                    if sep and not runs[-1]["text"].endswith(" ") and not text.startswith(" "):
                        runs[-1]["text"] += sep
                style = {
                    "font": span.font, "family": span.info.family, "size": round(span.size, 2),
                    "bold": span.info.bold, "italic": span.info.italic, "smallcaps": span.info.smallcaps,
                    "color": span.color, "link": span.link,
                }
                if runs and all(runs[-1][k] == v for k, v in style.items()):
                    runs[-1]["text"] += text
                else:
                    runs.append({"text": text, **style})
                prev = span
        if runs:
            runs[0]["text"] = indent + runs[0]["text"].lstrip()
            runs[-1]["text"] = runs[-1]["text"].rstrip()
        return runs

    def classify(self) -> dict:
        self.analyse_graphics()
        lines = self.build_lines(self.spans())
        self.assign_reasons(lines)
        paragraphs = self.build_paragraphs(lines)
        boxes = self.build_boxes(paragraphs)

        n = self.page["index"]
        elements = []
        for bi, box in enumerate(boxes):
            rect = union_all(p.rect for p in box)
            code = all(is_mono(p.spans) for p in box)
            elements.append({
                "id": f"p{n}t{bi}", "kind": "text", "role": box[0].role, "bbox": rect.as_list(),
                "panel": self.panel_of(rect),
                "paragraphs": [{
                    "align": p.align, "level": p.level, "bullet": p.bullet, "size": round(p.size, 2),
                    "text_x0": round(p.x0, 2),
                    "lines": [{"baseline": round(l.baseline, 2), "x0": round(l.x0, 2), "x1": round(l.x1, 2)}
                              for l in p.lines],
                    "runs": self.runs(p, code_indent(p, rect.x0) if code else ""),
                } for p in box],
                "code": code,
                "spans": [s.id for p in box for s in p.spans],
            })

        used = {sid for e in elements for sid in e["spans"]}
        by_reason: dict[str, list[Span]] = {}
        for line in lines:
            for s in line.spans:
                if s.id not in used:
                    reason = line.reason or "unsure"
                    by_reason.setdefault(reason, []).append(s)
        left = [{"reason": r, "spans": [s.id for s in ss], "bboxes": [s.rect.as_list() for s in ss]}
                for r, ss in by_reason.items()]

        chars_total = sum(len(s["text"].strip()) for s in self.page["spans"])
        chars_native = sum(len(s["text"].strip()) for s in self.page["spans"] if s["id"] in used)
        return {
            "page": n, "frame": self.page["label"], "size": self.page["size"],
            "elements": elements, "left_in_background": left,
            "panels": [{"bbox": p["bbox"].as_list(), "fill": p["fill"], "rounded": p["rounded"]} for p in self.panels],
            "figure_regions": [r.as_list() for r in self.regions],
            "stats": {"chars": chars_total, "chars_native": chars_native},
        }


def classify(raw: dict) -> dict:
    body = body_size(raw)
    slides = [PageClassifier(page, body).classify() for page in raw["pages"]]
    chars = sum(s["stats"]["chars"] for s in slides)
    native = sum(s["stats"]["chars_native"] for s in slides)
    return {
        "version": 1, "source": raw["source"], "body_size": body,
        "stats": {"chars": chars, "chars_native": native, "native_share": round(native / chars, 3) if chars else 0},
        "slides": slides,
    }
