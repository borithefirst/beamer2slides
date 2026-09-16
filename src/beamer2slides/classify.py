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
import unicodedata
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


def overlap(a: Rect, b: Rect) -> float:
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


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
    inline_math: bool = False

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


def script_of(span: Span, line: "Line") -> str | None:
    """'super' / 'sub' for a smaller span raised / lowered from the line's baseline."""
    if span.size >= 0.85 * line.size:
        return None
    shift = span.baseline - line.baseline
    if shift < -0.12 * line.size:
        return "super"
    if shift > 0.12 * line.size:
        return "sub"
    return None


DOUBLE_STRUCK = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}


def math_text(font: str, text: str) -> tuple[str, bool]:
    """Unicode text and italic flag for a span set in a math font."""
    name = font.upper()
    if name.startswith("MSBM"):  # \mathbb
        return "".join(DOUBLE_STRUCK.get(c, chr(0x1D538 + ord(c) - 65) if "A" <= c <= "Z" else c)
                       for c in text), False
    out, italic = [], name.startswith("CMMI")
    for c in text:
        uname = unicodedata.name(c, "")
        if uname.startswith("MATHEMATICAL ITALIC "):  # OpenType math fonts: 𝑥 -> x, italic
            letter = uname.rsplit(" ", 1)[-1]
            out.append(letter.lower() if "SMALL" in uname else letter)
            italic = True
        else:
            out.append(c)
    return "".join(out), italic


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

    def is_decoration(self, r: Rect) -> bool:
        """Theme artwork: things anchored to a page edge spanning much of it (sidebars, header
        and footer bars), or hairlines running across most of the page."""
        edges = (r.x0 <= 1) + (r.y0 <= 1) + (r.x1 >= self.W - 1) + (r.y1 >= self.H - 1)
        if edges >= 2:
            return True  # corner pieces (logo boxes, header/sidebar junctions)
        if edges and (r.w >= 0.4 * self.W or r.h >= 0.4 * self.H):
            return True
        return (r.h <= 3 and r.w >= 0.5 * self.W) or (r.w <= 3 and r.h >= 0.5 * self.H)

    def analyse_graphics(self) -> None:
        graphics = []
        rules: dict[tuple[int, int], list[Rect]] = {}
        self.decorations: list[Rect] = []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if r.w * r.h >= 0.95 * self.W * self.H:
                continue  # page background
            fill_only = d["type"] == "f" and set(d["items"]) <= set("relcq")
            if self.is_decoration(r) and not (fill_only and r.h >= 3 and r.w >= 0.25 * self.W):
                self.decorations.append(r)
                continue
            if fill_only and r.w >= 0.25 * self.W and r.h >= 3:
                self.panels.append({"bbox": r, "fill": d["fill"], "id": d["id"],
                                    "rounded": "c" in d["items"], "corners": d.get("corners", {}),
                                    "opacity": d.get("fill_opacity", 1.0), "image": False})
            elif d["type"] == "s" and r.h <= 1.0 and r.w <= 3 * self.body and set(d["items"]) <= {"l"}:
                self.bars.append(r)  # fraction bars, radical overbars
            else:
                graphics.append(r)
                stroke_rule = d["type"] == "s" and r.h <= 1.0 and set(d["items"]) <= {"l"}
                fill_rule = fill_only and r.h <= 1.5 and r.w >= 20  # booktabs rules are thin filled boxes
                if stroke_rule or fill_rule:
                    rules.setdefault((round(r.x0), round(r.x1)), []).append({
                        "rect": r, "color": (d["fill"] if fill_rule else d["stroke"]) or "#000000",
                        "weight": r.h if fill_rule else (d["width"] or 0.4)})
        # Two or more horizontal rules of equal extent frame a table: the whole span is one
        # figure (or a native table, see table_from).
        self.table_rules = [g for g in rules.values() if len(g) >= 2]
        for group in self.table_rules:
            graphics.append(union_all(r["rect"] for r in group))
        # A short stroke touching other graphics is an arrow shaft or a tick, not a fraction bar.
        touching = [b for b in self.bars if any(b.expand(1.5).intersects(g) for g in graphics)]
        self.bars = [b for b in self.bars if b not in touching]
        graphics += touching
        for im in self.page["images"]:
            r = Rect.of(im["bbox"])
            if min(r.w, r.h) < SMALL_IMAGE_PT:
                self.small_images.append((im, r))  # bullets, block shadows
            elif self.is_decoration(r):
                self.decorations.append(r)  # sidebar/header shading
            elif r.w >= 0.6 * self.W:
                self.panels.append({"bbox": r, "fill": None, "id": im["id"], "rounded": False,
                                    "corners": {}, "opacity": 1.0, "image": True})
            else:
                graphics.append(r)
        self.graphics = graphics
        self.regions = cluster_rects(graphics, gap=3.0) if graphics else []

    def on_edge_artwork(self, r: Rect) -> bool:
        edge_panels = [p["bbox"] for p in self.panels
                       if p["bbox"].x0 <= 1 or p["bbox"].y0 <= 1 or p["bbox"].x1 >= self.W - 1 or p["bbox"].y1 >= self.H - 1]
        return any(d.contains(r.cx, r.cy) for d in self.decorations + edge_panels)

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

    def inside_figure_share(self, line: Line) -> float:
        """Share of the line's characters whose span centre lies in a figure region. A list
        whose number sits in a drawn box has one character inside, not the whole line."""
        total = sum(len(s.text.strip()) for s in line.spans) or 1
        inside = sum(len(s.text.strip()) for s in line.spans
                     if any(reg.expand(1).contains(s.rect.cx, s.rect.cy) for reg in self.regions))
        return inside / total

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
            # Numbers drawn on a small box or circle (e.g. Bergen's enumerate): the box is
            # patched out of the background and replaced by a native numbered bullet.
            if gap >= 0.25 * line.size and re.fullmatch(r"[0-9]{1,2}|[a-zA-Z]", token):
                for g in self.graphics:
                    if g.w <= 1.6 * line.size and g.h <= 1.6 * line.size and g.contains(first.rect.cx, first.rect.cy):
                        line.bullet = {"kind": "number", "text": token, "color": first.color,
                                       "bbox": g.as_list(), "patch": True}
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
        # Vector bullets (shaded balls, squares drawn as paths): a small, roughly square
        # graphic just left of the text at x-height.
        x0 = min(s.rect.x0 for s in spans)
        for g in self.graphics:
            if 0.25 * line.size <= g.w <= 1.1 * line.size and 0.25 * line.size <= g.h <= 1.1 * line.size \
                    and g.x1 <= x0 + 0.5 and x0 - g.x1 <= 1.5 * line.size \
                    and line.baseline - 0.9 * line.size <= g.cy <= line.baseline + 0.1 * line.size:
                line.bullet = {"kind": "shape", "text": "", "bbox": g.as_list(), "patch": True}
                return

    def continues_prose(self, line: Line) -> bool:
        """A short all-math line that is really the wrapped end of a text line above it
        (same left edge, one line pitch higher) - not a display equation."""
        for other in self.all_lines:
            if other is line or other.reason is not None:
                continue
            pitch = line.baseline - other.baseline
            if 0 < pitch <= 1.4 * line.size and abs(other.rect.x0 - line.rect.x0) <= 1.5 \
                    and len(other.text.replace(" ", "")) >= 20:
                return True
        return False

    def math_kind(self, line: Line) -> str | None:
        """None for plain text, 'inline' for math that Slides text can carry (symbols,
        single-level sub/superscripts), 'complex' for anything that must stay a picture."""
        spans = line.content
        scripts = [s for s in spans if script_of(s, line)]
        math_font = any(s.info.family == "math" for s in spans)
        bars = any(b.expand(1).intersects(line.rect) for b in self.bars)  # fractions, radicals
        chars = "".join(s.text for s in spans).replace(" ", "")
        mathy = sum(len(s.text.strip()) for s in spans if s.info.italic and len(s.text.strip()) <= 2)
        mathy += sum(ch in MATH_OPERATORS for ch in chars)
        formula_like = len(chars) > 0 and mathy / len(chars) >= 0.4  # a display equation, not prose

        if not (math_font or scripts or bars or formula_like or "�" in line.text):
            return None
        if bars or "�" in line.text:
            return "complex"
        if formula_like and not self.continues_prose(line):
            return "complex"
        if any(s.font.upper().startswith("CMEX") for s in spans):
            return "complex"  # big operators, large delimiters
        if any(s.size < 0.6 * line.size or abs(s.baseline - line.baseline) > 0.6 * line.size for s in scripts):
            return "complex"  # second-level scripts, limits
        for a in scripts:
            for b in scripts:
                if a is not b and script_of(a, line) != script_of(b, line) and \
                        a.rect.x0 < b.rect.x1 - 0.5 and b.rect.x0 < a.rect.x1 - 0.5:
                    return "complex"  # sub and superscript stacked (a_1^2)
        return "inline"

    def assign_reasons(self, lines: list[Line]) -> None:
        self.all_lines = lines
        for line in lines:
            if not all(s.horizontal for s in line.spans):
                line.reason = "rotated"
            elif self.inside_figure_share(line) >= 0.5:
                line.reason = "figure"
            elif line.size <= 0.7 * self.body and (line.rect.y1 <= 0.13 * self.H or line.rect.y0 >= 0.87 * self.H):
                line.reason = "theme"
            elif line.size < 0.78 * self.body and self.on_edge_artwork(line.rect):
                line.reason = "theme"  # sidebar navigation, header/footer info

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
                kind = self.math_kind(line)
                if kind == "complex":
                    line.reason = "math"
                line.inline_math = kind == "inline"

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
                # The left-hand side of a display equation ("L(θ) =") split off from its complex part.
                same_formula = line.inline_math and any(
                    abs(m.baseline - line.baseline) <= 0.3 * line.size and
                    max(0.0, m.rect.x0 - line.rect.x1, line.rect.x0 - m.rect.x1) <= 4 * line.size for m in maths)
                if (near and small and len(txt) <= 6) or eqno or same_formula:
                    line.reason = "math"
                    changed = True

    # -- paragraphs -------------------------------------------------------------

    def has_side_content(self, a: Line, b: Line) -> bool:
        """Is there text or graphics beside these two lines (columns, a picture next to text)?"""
        y0, y1 = min(a.rect.y0, b.rect.y0), max(a.rect.y1, b.rect.y1)
        x0, x1 = min(a.rect.x0, b.rect.x0), max(a.rect.x1, b.rect.x1)
        others = [l.rect for l in self.all_lines if l is not a and l is not b] + list(self.regions)
        return any(r.y0 < y1 and y0 < r.y1 and (r.x0 > x1 + 5 or r.x1 < x0 - 5) for r in others)

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
            if not self.has_side_content(last, line):
                # Full-width text: beamer's margins are symmetric, so the text block ends
                # where the left margin mirrors. Short paragraphs never reach col_right.
                col_right = max(col_right, self.W - self.text_margin)
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
        box_rect = union_all([p.rect for p in box] + [Rect.of(p.bullet["bbox"]) for p in box if p.bullet])
        if is_mono(par.spans) and all(is_mono(p.spans) for p in box):
            # Code block: indentation varies freely, lines follow at normal pitch.
            return par.rect.x0 >= box_rect.x0 - 1.5 and gap <= 1.35 * par.size
        if par.align == "center":
            if abs(par.rect.cx - box_rect.cx) > 2:
                return False
        else:
            # A nested item's bullet starts after its parent's text start, but not much further.
            x = min(par.rect.x0, par.bullet["bbox"][0]) if par.bullet else par.rect.x0
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
                        if runs[-1]["script"]:
                            text = sep + text  # keep the space out of the raised/lowered run
                        else:
                            runs[-1]["text"] += sep
                script = script_of(span, line)
                family, italic = span.info.family, span.info.italic
                if family == "math":
                    # Math fonts carry symbols and variables; show them in the text family.
                    base = line.main.info.family
                    family = base if base != "math" else "serif"
                    text, italic = math_text(span.font, text)
                style = {
                    "font": span.font, "family": family,
                    # Slides shrinks sub/superscripts itself: give them the line's size.
                    "size": round(line.size if script else span.size, 2),
                    "bold": span.info.bold, "italic": italic, "smallcaps": span.info.smallcaps,
                    "color": span.color, "link": span.link, "script": script,
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

    def figures(self, lines: list[Line], text_elements: list[dict]) -> list[dict]:
        """Figure regions (graphics, images and their labels) that can become separate pictures.

        Skipped, so they stay in the background: specks (shadow corners, QED boxes),
        near-full-page artwork, and anything overlapping an editable text box."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        # Cluster word by word: one "line" of labels can span two neighbouring figures.
        rects = list(self.regions) + [s.rect for s in label_spans]
        if not self.regions:
            return []
        text_rects = [Rect.of(e["bbox"]) for e in text_elements]
        out = []
        for c in cluster_rects(rects, gap=0.8 * self.body):
            if max(c.w, c.h) < 25 or c.w * c.h > 0.8 * self.W * self.H:
                continue
            if (c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H:
                continue  # navigation dots and ornaments in the header/footer band
            if not any(r.intersects(c.expand(0.1)) for r in self.regions):
                continue  # only stray rotated text, no graphics
            if any(t.intersects(c) for t in text_rects):
                continue
            table = self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                continue
            spans = [s.id for s in label_spans if c.expand(0.5).contains_rect(s.rect)]
            out.append({"id": f"p{self.page['index']}f{len(out)}", "kind": "image", "role": "figure",
                        "bbox": c.expand(1.0).as_list(), "spans": spans})
        return out

    def table_from(self, c: Rect, label_spans: list[Span], text_rects: list[Rect], index: int) -> dict | None:
        """A figure cluster that is really a plain table: text framed by horizontal rules of
        equal extent and nothing else. Returns a native table element, or None."""
        groups = [g for g in self.table_rules if c.expand(1).contains_rect(union_all(r["rect"] for r in g))]
        if len(groups) != 1:
            return None
        rules = sorted(groups[0], key=lambda r: r["rect"].y0)
        frame = union_all(r["rect"] for r in rules)
        rule_rects = [r["rect"] for r in rules] + [frame]
        for g in self.graphics:
            if c.expand(0.5).contains_rect(g) and not any(abs(g.x0 - rr.x0) < 0.6 and abs(g.x1 - rr.x1) < 0.6
                                                          and abs(g.y0 - rr.y0) < 0.6 and abs(g.y1 - rr.y1) < 0.6
                                                          for rr in rule_rects):
                return None  # vertical rules, cell shading, pictures: keep it a picture
        spans = sorted((s for s in label_spans if c.expand(0.5).contains_rect(s.rect)), key=lambda s: s.baseline)
        if not spans or any(s.info.family == "math" or not s.horizontal for s in spans):
            return None

        # Rows by baseline, cells by horizontal gaps, columns by the union of cell extents.
        rows: list[list[Span]] = []
        for s in spans:
            if rows and abs(s.baseline - rows[-1][0].baseline) <= 0.5 * s.size:
                rows[-1].append(s)
            else:
                rows.append([s])
        size = max(s.size for s in spans)
        row_chunks = []
        for row in rows:
            chunks: list[list[Span]] = []
            for s in sorted(row, key=lambda s: s.rect.x0):
                if chunks and s.rect.x0 - chunks[-1][-1].rect.x1 <= 0.5 * size:
                    chunks[-1].append(s)
                else:
                    chunks.append([s])
            row_chunks.append(chunks)
        intervals = sorted((ch[0].rect.x0, ch[-1].rect.x1) for chunks in row_chunks for ch in chunks)
        columns: list[list[float]] = []
        for x0, x1 in intervals:
            if columns and x0 < columns[-1][1] + 1:
                columns[-1][1] = max(columns[-1][1], x1)
            else:
                columns.append([x0, x1])
        cells = [[[] for _ in columns] for _ in rows]
        placed: list[list[list[Span]]] = [[] for _ in columns]
        for r, chunks in enumerate(row_chunks):
            for ch in chunks:
                col = next(i for i, (x0, x1) in enumerate(columns) if ch[0].rect.x0 < x1 + 0.5 and x0 - 0.5 < ch[-1].rect.x1)
                cells[r][col].extend(ch)
                placed[col].append(ch)
        col_info = []
        for (x0, x1), chunks in zip(columns, placed):
            if all(abs(ch[0].rect.x0 - x0) <= 1 for ch in chunks):
                align = "left"
            elif all(abs(ch[-1].rect.x1 - x1) <= 1 for ch in chunks):
                align = "right"
            else:
                align = "center"
            col_info.append({"x0": round(x0, 2), "x1": round(x1, 2), "align": align})

        baselines = [statistics.fmean(s.baseline for s in row if s.size >= 0.9 * size) for row in rows]
        pitches = [b - a for a, b in zip(baselines, baselines[1:])] or [1.4 * size]
        # Slides rows are at least one line plus 7.2 pt padding above and below (see
        # docs/calibration.md); refuse if the taller table would run into content below.
        scale = 720.0 / self.W
        z = size * scale / 1.02
        row_h = [max(p * scale, 1.195 * z + 14.4) / scale for p in pitches + [pitches[-1]]]
        top = baselines[0] - (6.48 + 0.968 * z) / scale
        bottom = top + sum(row_h)
        grown = Rect(frame.x0, frame.y1, frame.x1, bottom)
        if bottom > self.H - 2 or any(t.intersects(grown) for t in text_rects) or \
                any(reg.intersects(grown) and not c.expand(0.5).contains_rect(reg) for reg in self.regions):
            return None

        def cell_runs(cell: list[Span]) -> list[dict]:
            runs: list[dict] = []
            for i, s in enumerate(cell):
                text = s.text
                if i and s.rect.x0 - cell[i - 1].rect.x1 > 0.15 * s.size and not text.startswith(" "):
                    text = " " + text
                style = {"font": s.font, "family": s.info.family, "size": round(s.size, 2), "bold": s.info.bold,
                         "italic": s.info.italic, "smallcaps": s.info.smallcaps, "color": s.color,
                         "link": s.link, "script": None}
                if runs and all(runs[-1][k] == v for k, v in style.items()):
                    runs[-1]["text"] += text
                else:
                    runs.append({"text": text, **style})
            return runs

        return {
            "id": f"p{self.page['index']}tab{index}", "kind": "table", "role": "table",
            "bbox": c.expand(1.0).as_list(), "frame": frame.as_list(), "size": round(size, 2),
            "row_baselines": [round(b, 2) for b in baselines],
            "row_heights": [round(p, 2) for p in pitches + [pitches[-1]]],
            "columns": col_info,
            "cells": [[cell_runs(cell) for cell in row] for row in cells],
            "rules": [{"row": min(k, len(rows) - 1), "position": "TOP" if k < len(rows) else "BOTTOM",
                       "color": r["color"], "weight": round(r["weight"], 2)}
                      for r in rules for k in [sum(b < r["rect"].cy for b in baselines)]],
            "spans": [s.id for s in spans],
        }

    def shapes(self, lines: list[Line], elements: list[dict]) -> list[dict]:
        """Filled panels (beamer blocks and the like) that can become native shapes.

        Only panels that are pure content containers qualify: not touching the page edge
        (those are theme bars, better left in the background like a layout), opaque, and
        with nothing on top that stays in the background (it would be hidden under the
        shape). The render stage additionally checks the panel really shows its fill colour."""
        used = {sid for e in elements for sid in e["spans"]}
        leftovers = [s.rect for l in lines for s in l.spans if s.id not in used]
        figures = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table")]
        figures += [Rect.of(p["bullet"]["bbox"]) for e in elements if e["kind"] == "text"
                    for p in e["paragraphs"] if p["bullet"] and p["bullet"].get("patch")]
        loose = [g for g in self.graphics if not any(f.expand(0.5).contains_rect(g) for f in figures)]
        bullet_images = {p["bullet"]["image"] for e in elements if e["kind"] == "text"
                         for p in e["paragraphs"] if p["bullet"] and p["bullet"]["kind"] == "image"}
        out = []
        for p in sorted(self.panels, key=lambda p: -p["bbox"].w * p["bbox"].h):
            r = p["bbox"]
            if p["image"] or not p["fill"] or p["opacity"] < 0.99:
                continue
            if r.x0 <= 1 or r.y0 <= 1 or r.x1 >= self.W - 1 or r.y1 >= self.H - 1:
                continue
            if any(Rect.of(e["bbox"]).expand(0.5).contains_rect(r) for e in elements if e["kind"] in ("image", "table")):
                continue  # already part of a picture
            inner = r.expand(-0.5)
            if any(inner.intersects(x) for x in leftovers):
                continue
            if any(overlap(inner, g) > 0.9 * max(g.w * g.h, 1e-6) for g in loose):
                continue  # graphics mostly on the panel (edge decorations like shadows are fine)
            if any(inner.contains_rect(ir, tol=0) and im["id"] not in bullet_images for im, ir in self.small_images):
                continue
            corners = set(p["corners"])
            if not corners:
                kind, flip = "RECTANGLE", False
            elif corners == {"tl", "tr"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", False
            elif corners == {"bl", "br"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", True  # same shape rotated 180°
            else:
                kind, flip = "ROUND_RECTANGLE", False
            out.append({"id": f"p{self.page['index']}s{len(out)}", "kind": "shape", "role": "panel",
                        "bbox": r.as_list(), "fill": p["fill"], "shape": kind, "flip": flip,
                        "radius": max(p["corners"].values(), default=0.0), "drawing": p["id"], "spans": []})
        return out

    def classify(self) -> dict:
        self.analyse_graphics()
        lines = self.build_lines(self.spans())
        self.assign_reasons(lines)
        body_lines = [l for l in lines if l.reason is None and abs(l.size - self.body) < 1]
        self.text_margin = min((l.rect.x0 for l in body_lines), default=0.08 * self.W)
        paragraphs = self.build_paragraphs(lines)
        boxes = self.build_boxes(paragraphs)

        n = self.page["index"]
        elements = []
        for bi, box in enumerate(boxes):
            rect = union_all([p.rect for p in box] + [Rect.of(p.bullet["bbox"]) for p in box if p.bullet])
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

        text_spans = {sid for e in elements for sid in e["spans"]}
        elements = self.figures(lines, elements) + elements  # pictures first: they sit below text
        text_spans |= {sid for e in elements if e["kind"] == "table" for sid in e["spans"]}
        elements = self.shapes(lines, elements) + elements   # shapes below pictures

        used = {sid for e in elements for sid in e["spans"]}
        paragraph_reason = {id(l): p.reason for p in paragraphs for l in p.lines}
        by_reason: dict[str, list[Span]] = {}
        for line in lines:
            for s in line.spans:
                if s.id not in used:
                    reason = line.reason or paragraph_reason.get(id(line)) or "unsure"
                    by_reason.setdefault(reason, []).append(s)
        left = [{"reason": r, "spans": [s.id for s in ss], "bboxes": [s.rect.as_list() for s in ss]}
                for r, ss in by_reason.items()]

        chars_total = sum(len(s["text"].strip()) for s in self.page["spans"])
        chars_native = sum(len(s["text"].strip()) for s in self.page["spans"] if s["id"] in text_spans)
        return {
            "page": n, "frame": self.page["label"], "size": self.page["size"],
            "elements": elements, "left_in_background": left,
            "panels": [{"bbox": p["bbox"].as_list(), "fill": p["fill"], "rounded": p["rounded"]} for p in self.panels],
            "figure_regions": [r.as_list() for r in self.regions],
            "stats": {"chars": chars_total, "chars_native": chars_native},
        }


def mark_title_page(slides: list[dict], doc_title: str) -> None:
    """On the title page, the box showing the document title (from the PDF metadata, which
    beamer fills as "Title - Subtitle") becomes the slide's title."""
    norm = lambda s: " ".join(s.casefold().split())
    wanted = norm(doc_title)
    if len(wanted) < 3:
        return
    for slide in slides[:2]:
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        if any(e["role"] == "title" for e in texts):
            continue
        for e in texts:
            first = norm("".join(r["text"] for r in e["paragraphs"][0]["runs"]))
            if len(first) >= 3 and wanted.startswith(first):
                e["role"] = "title"
                return


def classify(raw: dict) -> dict:
    body = body_size(raw)
    slides = [PageClassifier(page, body).classify() for page in raw["pages"]]
    mark_title_page(slides, raw["source"].get("title", ""))
    chars = sum(s["stats"]["chars"] for s in slides)
    native = sum(s["stats"]["chars_native"] for s in slides)
    return {
        "version": 1, "source": raw["source"], "body_size": body,
        "stats": {"chars": chars, "chars_native": native, "native_share": round(native / chars, 3) if chars else 0},
        "slides": slides,
    }
