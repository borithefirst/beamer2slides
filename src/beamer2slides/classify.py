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
    underline: bool = False
    highlight: str | None = None  # background colour (\colorbox)


@dataclass(eq=False)
class Line:
    spans: list[Span]
    bullet: dict | None = None
    bullet_spans: list[Span] = field(default_factory=list)
    reason: str | None = None
    inline_math: bool = False
    fractions: list = field(default_factory=list)  # (bar, numerator spans, denominator spans)

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


FRACTION_SLASH = "⁄"


def reading_order(line: "Line") -> list[tuple]:
    """The line's content spans left to right, except that each simple fraction becomes
    numerator (superscript), fraction slash, denominator (subscript)."""
    owner = {id(s): f for f in line.fractions for s in f[1] + f[2]}
    out, emitted = [], set()
    for s in line.content:
        f = owner.get(id(s))
        if f is None:
            out.append((s, None))
        elif id(f) not in emitted:
            emitted.add(id(f))
            out += [(x, "super") for x in f[1]] + [(FRACTION_SLASH, None)] + [(x, "sub") for x in f[2]]
    return out


def span_runs(spans: list[Span]) -> list[dict]:
    """Runs for a short piece of text given as spans in reading order (cells, node labels)."""
    runs: list[dict] = []
    for i, s in enumerate(spans):
        text = s.text
        if i and s.rect.x0 - spans[i - 1].rect.x1 > 0.15 * s.size and not text.startswith(" "):
            text = " " + text
        style = {"font": s.font, "family": s.info.family, "size": round(s.size, 2), "bold": s.info.bold,
                 "italic": s.info.italic, "smallcaps": s.info.smallcaps, "color": s.color,
                 "link": s.link, "script": None, "underline": s.underline, "highlight": s.highlight}
        if runs and all(runs[-1][k] == v for k, v in style.items()):
            runs[-1]["text"] += text
        else:
            runs.append({"text": text, **style})
    return runs


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

    def text_decorations(self, spans: list[Span]) -> None:
        """Underlines and \\colorbox highlights become text styles: a thin rule just below a
        stretch of words, or a filled box tightly around words and touching no other graphics.
        Sets the span attributes and remembers the drawings (they leave the background with
        the text, and are not graphics)."""
        self.decor_ids: set[str] = set()
        self.decor_rects: dict[str, list[Rect]] = {}  # span id -> drawings styling it
        flat = [s for s in spans if s.horizontal and s.text.strip()]
        drawings = [(d, Rect.of(d["bbox"])) for d in self.page["drawings"]]
        for d, r in drawings:
            if self.is_decoration(r) or r.w < 2 or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            ops = d["items"]
            if r.h <= 1.2 and ((d["type"] == "f" and ops == "re") or (d["type"] == "s" and ops == "l")):
                words = sorted((s for s in flat if r.x0 - 1 <= s.rect.x0 and s.rect.x1 <= r.x1 + 1
                                and 0 < r.cy - s.baseline <= 0.45 * s.size), key=lambda s: s.rect.x0)
                if not words:
                    continue
                size = max(s.size for s in words)
                gaps = [b.rect.x0 - a.rect.x1 for a, b in zip(words, words[1:])]
                covered = sum(s.rect.w for s in words)
                below = any(s.rect.x0 < r.x1 and r.x0 < s.rect.x1 and s.baseline > r.cy and s.rect.y0 < r.cy + 0.25 * size
                            for s in flat)
                if below or covered < 0.8 * r.w or any(g > 0.6 * size for g in gaps) or \
                        abs(words[0].rect.x0 - r.x0) > 0.3 * size or abs(words[-1].rect.x1 - r.x1) > 0.3 * size:
                    continue
                for s in words:
                    s.underline = True
            elif d["type"] == "f" and ops == "re" and d.get("fill") and d.get("fill_opacity", 1.0) >= 0.99:
                inside = [s for s in flat if r.contains_rect(s.rect, tol=0.5)]
                if not inside or any(s.rect.intersects(r) and s not in inside for s in flat):
                    continue
                size = max(s.size for s in inside)
                if not 0.9 * size <= r.h <= 2.2 * size or len({round(s.baseline) for s in inside}) != 1:
                    continue
                if sum(s.rect.w for s in inside) < 0.6 * r.w or \
                        any(o is not d and ro.expand(2).intersects(r) and not r.contains_rect(ro, tol=0)
                            for o, ro in drawings if ro.w * ro.h < 0.95 * self.W * self.H):
                    continue  # part of a figure (a filled TikZ node with lines attached)
                for s in inside:
                    s.highlight = d["fill"]
                words = inside
            else:
                continue
            self.decor_ids.add(d["id"])
            for s in words:
                self.decor_rects.setdefault(s.id, []).append(r)

    def analyse_graphics(self) -> None:
        graphics = []
        rules: dict[tuple[int, int], list[Rect]] = {}
        self.decorations: list[Rect] = []
        for d in self.page["drawings"]:
            if d["id"] in self.decor_ids:
                continue
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
        # External links keep their URL; internal ones become "#page=N" (PDF page index).
        links = [(Rect.of(l["bbox"]), l.get("uri") or f"#page={l['page']}") for l in self.page["links"]]
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
            if 0.25 * line.size <= g.w <= 1.3 * line.size and 0.25 * line.size <= g.h <= 1.6 * line.size \
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
            # Any word of the line above may start the text column (theorem labels can hang left).
            aligned = any(abs(s.rect.x0 - line.rect.x0) <= 1.5 for s in other.spans)
            if 0 < pitch <= 1.4 * line.size and aligned and len(other.text.replace(" ", "")) >= 20:
                return True
        return False

    def simple_fraction(self, line: Line, bar: Rect):
        """(bar, numerator spans, denominator spans) for a small inline fraction such as
        \\frac{1}{2}: short text directly above and below a short bar, no radical sign."""
        above = [s for s in line.content if s.rect.x0 >= bar.x0 - 1 and s.rect.x1 <= bar.x1 + 1
                 and s.rect.cy < bar.cy and s.size < 0.85 * line.size]
        below = [s for s in line.content if s.rect.x0 >= bar.x0 - 1 and s.rect.x1 <= bar.x1 + 1
                 and s.rect.cy > bar.cy and s.size < 0.85 * line.size]
        if not above or not below:
            return None
        if len("".join(s.text for s in above + below).replace(" ", "")) > 6:
            return None
        if any("√" in s.text for s in line.content if abs(s.rect.x1 - bar.x0) < 3):
            return None  # radical overbar
        by_x = lambda group: sorted(group, key=lambda s: s.rect.x0)
        return bar, by_x(above), by_x(below)

    def math_kind(self, line: Line) -> str | None:
        """None for plain text, 'inline' for math that Slides text can carry (symbols,
        single-level sub/superscripts), 'complex' for anything that must stay a picture."""
        spans = line.content
        line_bars = [b for b in self.bars if b.expand(1).intersects(line.rect)]  # fractions, radicals
        fractions = [f for f in (self.simple_fraction(line, b) for b in line_bars) if f]
        if len(fractions) == len(line_bars):
            line.fractions = fractions  # all bars are simple a/b fractions: text can carry them
            bars = False
        else:
            bars = True
        in_fraction = {id(s) for _, num, den in fractions for s in num + den}
        scripts = [s for s in spans if script_of(s, line) and id(s) not in in_fraction]
        math_font = any(s.info.family == "math" for s in spans)
        chars = "".join(s.text for s in spans).replace(" ", "")
        mathy = sum(len(s.text.strip()) for s in spans if s.info.italic and len(s.text.strip()) <= 2)
        mathy += sum(ch in MATH_OPERATORS for ch in chars)
        formula_like = len(chars) > 0 and mathy / len(chars) >= 0.4  # a display equation, not prose

        if not (math_font or scripts or bars or fractions or formula_like or "�" in line.text):
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

        # Bullets first: a short list item next to its icon bullet is not a figure label.
        for line in lines:
            if line.reason is None:
                self.detect_bullet(line)

        # Short labels next to figures (axis ticks, axis labels) belong to the figure.
        regions = list(self.regions)
        changed = True
        while changed:
            changed = False
            for line in lines:
                if line.reason is None and not line.bullet and len(line.text.replace(" ", "")) <= 12 and \
                        line.size <= 1.15 * self.body and \
                        any(reg.distance(line.rect) <= 0.8 * line.size for reg in regions):
                    line.reason = "figure"
                    regions.append(line.rect)
                    changed = True

        for line in lines:
            if line.reason is None:
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
            # A frame subtitle sits right under the title, aligned with it, and is no list item.
            if par.role == "body" and not par.bullet and par.rect.y0 < 0.2 * self.H and par.size < self.body + 0.5 and \
                    any(0 < par.first.baseline - t.last.baseline <= 2 * t.size and abs(par.x0 - t.x0) <= 2
                        for t in titles):
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
            for si, (span, forced) in enumerate(reading_order(line)):
                if span == FRACTION_SLASH:
                    main = line.main
                    runs.append({"text": FRACTION_SLASH, "font": main.font, "family": main.info.family,
                                 "size": round(line.size, 2), "bold": False, "italic": False, "smallcaps": False,
                                 "color": main.color, "link": main.link, "script": None,
                                 "underline": False, "highlight": None})
                    continue
                text = span.text
                if forced == "sub":  # denominator: follows the slash directly
                    pass
                elif prev is not None:
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
                script = forced or script_of(span, line)
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
                    "underline": span.underline, "highlight": span.highlight,
                }
                if runs and runs[-1]["text"].endswith(" ") and (runs[-1]["underline"], runs[-1]["highlight"]) != \
                        (style["underline"], style["highlight"]) and (runs[-1]["underline"] or runs[-1]["highlight"]):
                    runs[-1]["text"] = runs[-1]["text"][:-1]  # an underline or highlight ends at the word
                    text = " " + text
                if runs and all(runs[-1][k] == v for k, v in style.items()):
                    runs[-1]["text"] += text
                else:
                    runs.append({"text": text, **style})
                prev = span
        if runs:
            runs[0]["text"] = indent + runs[0]["text"].lstrip()
            runs[-1]["text"] = runs[-1]["text"].rstrip()
        return runs

    def math_pictures(self, lines: list[Line], paragraphs: list[Paragraph], elements: list[dict]) -> list[dict]:
        """Math that cannot be text (display equations, fractions, and paragraphs containing
        them) becomes movable pictures instead of staying baked into the background."""
        used = {sid for e in elements for sid in e["spans"]}
        para_reason = {id(l): p.reason for p in paragraphs for l in p.lines}
        spans = [s for l in lines for s in l.spans if s.id not in used
                 and (l.reason == "math" or (l.reason is None and para_reason.get(id(l)) == "math"))]
        if not spans:
            return []
        # Collisions are checked against the actual text lines, not text box outlines: a math
        # item inside a bullet list lies within the list's box but touches none of its lines.
        blocked = [Rect.of(e["bbox"]) for e in elements if e["kind"] != "text"]
        for e in elements:
            if e["kind"] == "text":
                for p in e["paragraphs"]:
                    blocked += [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                                for l in p["lines"]]
                    if p["bullet"]:  # glyph boxes include ascender space; use their visible core
                        b = Rect.of(p["bullet"]["bbox"])
                        blocked.append(Rect(b.x0, b.cy - 0.25 * b.h, b.x1, b.cy + 0.25 * b.h))
        out = []
        for c in cluster_rects([s.rect for s in spans] + list(self.bars), gap=0.6 * self.body):
            box = c.expand(1.5)
            members = [s for s in spans if box.contains_rect(s.rect)]
            if not members or any(b.intersects(box) for b in blocked):
                continue  # only a stray bar, or tangled with native content: leave it in the background
            out.append({"id": f"p{self.page['index']}m{len(out)}", "kind": "image", "role": "math",
                        "bbox": box.as_list(), "spans": [s.id for s in members]})
        return out

    def figures(self, lines: list[Line], text_elements: list[dict]) -> list[dict]:
        """Figure regions (graphics, images and their labels) that can become separate pictures.

        Skipped, so they stay in the background: specks (shadow corners, QED boxes),
        near-full-page artwork, and anything overlapping an editable text box."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        if not self.regions:
            return []
        text_rects = [Rect.of(e["bbox"]) for e in text_elements]
        out = []
        # Tables first, from their rules: clustering could merge a table with a picture beside it.
        for group in self.table_rules:
            frame = union_all(r["rect"] for r in group).expand(1)
            table = self.table_from(frame, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                taken = set(table["spans"])
                label_spans = [s for s in label_spans if s.id not in taken]
        table_frames = [Rect.of(t["bbox"]) for t in out]
        regions = [r for r in self.regions if not any(f.expand(0.5).contains_rect(r) for f in table_frames)]
        if not regions:
            return out
        # Cluster word by word: one "line" of labels can span two neighbouring figures.
        rects = regions + [s.rect for s in label_spans]
        for c in cluster_rects(rects, gap=0.8 * self.body):
            if max(c.w, c.h) < 25 or c.w * c.h > 0.8 * self.W * self.H:
                continue
            if (c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H:
                continue  # navigation dots and ornaments in the header/footer band
            if not any(r.intersects(c.expand(0.1)) for r in regions):
                continue  # only stray rotated text, no graphics
            if any(t.intersects(c) for t in text_rects):
                continue
            table = self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                continue
            diagram = self.diagram_from(c, label_spans, len(out))
            if diagram:
                out.append(diagram)
                continue
            spans = [s.id for s in label_spans if c.expand(0.5).contains_rect(s.rect)]
            out.append({"id": f"p{self.page['index']}f{len(out)}", "kind": "image", "role": "figure",
                        "bbox": c.expand(1.0).as_list(), "spans": spans})
        return out

    def diagram_from(self, c: Rect, label_spans: list[Span], index: int) -> dict | None:
        """A figure cluster made only of simple nodes (rectangles, rounded rectangles, ellipses)
        with their text inside, straight lines and arrow tips: rebuilt from native Slides
        shapes and lines. Anything else (curves, images, math, loose labels) keeps it a picture."""
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"])) for im in self.page["images"]):
            return None
        nodes, lines, tips = [], [], []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.contains_rect(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            path = d.get("path")
            if path is None:
                return None
            ops = "".join(op for op, _ in path)
            shape = {"re": "RECTANGLE", "lclclclc": "ROUND_RECTANGLE", "clclclcl": "ROUND_RECTANGLE",
                     "cccc": "ELLIPSE"}.get(ops)
            if shape and r.w > 3 and r.h > 3:
                nodes.append({"rect": r, "shape": shape, "spans": [],
                              "fill": d["fill"] if "f" in d["type"] else None,
                              "stroke": d["stroke"] if "s" in d["type"] else None, "width": d["width"]})
            elif d["type"] == "s" and ops == "l":
                (x1, y1), (x2, y2) = path[0][1]
                lines.append({"from": [x1, y1], "to": [x2, y2], "stroke": d["stroke"] or "#000000",
                              "width": d["width"] or 0.4, "arrow_from": None, "arrow_to": None})
            elif max(r.w, r.h) <= 6 and set(ops) <= {"c", "l"}:
                # Arrow heads are small separate paths: stroked (->), filled triangles (latex)
                # or filled concave quadrilaterals (stealth).
                if d["type"] == "s":
                    style = "OPEN_ARROW"
                else:
                    style = "STEALTH_ARROW" if ops == "llll" else "FILL_ARROW"
                points = [p for _, pts in path for p in pts]
                tips.append((r, style, points))
            else:
                return None
        if not nodes:
            return None
        for tip, style, points in tips:
            ends = [(ln, end) for ln in lines for end in ("from", "to") if tip.expand(1).contains(*ln[end])]
            if not ends:
                return None
            ln, end = ends[0]
            ln["arrow_" + end] = style
            if style != "OPEN_ARROW":
                # TikZ stops the line where a filled head begins; Slides draws the head at the
                # line's end, so extend the line to the tip.
                other = ln["to" if end == "from" else "from"]
                ux, uy = ln[end][0] - other[0], ln[end][1] - other[1]
                length = (ux * ux + uy * uy) ** 0.5 or 1.0
                ux, uy = ux / length, uy / length
                reach = max((px - ln[end][0]) * ux + (py - ln[end][1]) * uy for px, py in points)
                if reach > 0:
                    ln[end] = [round(ln[end][0] + reach * ux, 2), round(ln[end][1] + reach * uy, 2)]

        spans = [s for s in label_spans if box.contains_rect(s.rect)]
        free: list[Span] = []
        for s in spans:
            if s.info.family == "math" or not s.horizontal:
                return None
            owners = [n for n in nodes if n["rect"].contains(s.rect.cx, s.rect.cy)]
            if owners:
                min(owners, key=lambda n: n["rect"].w * n["rect"].h)["spans"].append(s)
            else:
                free.append(s)  # edge labels and captions: a text box in the group
        # Free labels on one baseline and close together are one label.
        for s in sorted(free, key=lambda s: (round(s.baseline), s.rect.x0)):
            last = nodes[-1] if nodes and nodes[-1]["shape"] is None else None
            if last and abs(last["spans"][-1].baseline - s.baseline) <= 0.3 * s.size and \
                    s.rect.x0 - last["spans"][-1].rect.x1 <= 0.5 * s.size:
                last["spans"].append(s)
                last["rect"] = last["rect"].union(s.rect)
            else:
                nodes.append({"rect": s.rect, "shape": None, "spans": [s], "fill": None, "stroke": None, "width": None})

        out_nodes = []
        for n in nodes:
            rows: list[list[Span]] = []
            for s in sorted(n["spans"], key=lambda s: (s.baseline, s.rect.x0)):
                if rows and abs(s.baseline - rows[-1][0].baseline) <= 0.5 * s.size:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            out_nodes.append({
                "bbox": n["rect"].as_list(), "shape": n["shape"], "fill": n["fill"], "stroke": n["stroke"],
                "width": n["width"], "paragraphs": [span_runs(sorted(row, key=lambda s: s.rect.x0)) for row in rows],
                "baselines": [round(row[0].baseline, 2) for row in rows],
            })
        return {"id": f"p{self.page['index']}dg{index}", "kind": "diagram", "role": "figure",
                "bbox": c.expand(1.0).as_list(), "nodes": out_nodes, "lines": lines,
                "spans": [s.id for s in spans]}

    def table_from(self, c: Rect, label_spans: list[Span], text_rects: list[Rect], index: int) -> dict | None:
        """A figure cluster that is really a plain table: text framed by horizontal rules of
        equal extent and nothing else. Returns a native table element, or None."""
        groups = [g for g in self.table_rules if c.expand(1).contains_rect(union_all(r["rect"] for r in g))]
        if len(groups) != 1:
            return None
        rules = sorted(groups[0], key=lambda r: r["rect"].y0)
        frame = union_all(r["rect"] for r in rules)
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"])) for im in self.page["images"]):
            return None
        # Every drawing must be a horizontal or vertical rule (\hline, \cline, |, booktabs);
        # cell shading and anything else keep the table a picture.
        horizontal, vertical = [], []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or not box.contains_rect(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            color = (d["fill"] if d["type"] == "f" else d["stroke"]) or "#000000"
            stroke = d["type"] == "s" and d["items"] == "l"
            fill = d["type"] == "f" and d["items"] == "re"
            if (stroke and r.h <= 1.0) or (fill and r.h <= 1.5 and r.w >= 3):
                horizontal.append({"rect": r, "color": color, "weight": r.h if fill else (d["width"] or 0.4)})
            elif (stroke and r.w <= 1.0) or (fill and r.w <= 1.5 and r.h >= 3):
                vertical.append({"rect": r, "color": color, "weight": r.w if fill else (d["width"] or 0.4)})
            else:
                return None
        if vertical:
            frame = union_all([frame] + [v["rect"] for v in vertical])
        spans = sorted((s for s in label_spans if box.contains_rect(s.rect)), key=lambda s: s.baseline)
        if not spans or any(s.info.family == "math" or not s.horizontal for s in spans):
            return None
        size = max(s.size for s in spans)

        # Rows by baseline. A row sitting halfway between its neighbours is a \multirow cell
        # spanning both of them.
        rows: list[list[Span]] = []
        for s in spans:
            if rows and abs(s.baseline - rows[-1][0].baseline) <= 0.5 * s.size:
                rows[-1].append(s)
            else:
                rows.append([s])
        base = [statistics.fmean(s.baseline for s in row) for row in rows]
        between = {i for i in range(1, len(rows) - 1)
                   if base[i] - base[i - 1] < 0.75 * size and base[i + 1] - base[i] < 0.75 * size
                   and not any(a.rect.x0 < b.rect.x1 and b.rect.x0 < a.rect.x1
                               for a in rows[i] for b in rows[i - 1] + rows[i + 1])}
        if any(i - 1 in between for i in between):
            return None
        grid_rows = [row for i, row in enumerate(rows) if i not in between]

        def chunks_of(row: list[Span]) -> list[list[Span]]:
            chunks: list[list[Span]] = []
            for s in sorted(row, key=lambda s: s.rect.x0):
                if chunks and s.rect.x0 - chunks[-1][-1].rect.x1 <= 0.5 * size and \
                        not any(chunks[-1][-1].rect.x1 < v["rect"].cx < s.rect.x0 for v in vertical):
                    chunks[-1].append(s)
                else:
                    chunks.append([s])
            return chunks

        # (row index in grid_rows, row span, chunk)
        items = []
        for i, row in enumerate(rows):
            r = sum(1 for j in range(i) if j not in between)
            for ch in chunks_of(row):
                items.append((r - 1, 2, ch) if i in between else (r, 1, ch))

        def extent(ch):
            return ch[0].rect.x0, ch[-1].rect.x1

        # A chunk overlapping two separate chunks of another row (\multicolumn), or crossing a
        # vertical rule, spans several columns; columns come from the other chunks.
        def spanning(item) -> bool:
            r, _, ch = item
            x0, x1 = extent(ch)
            if any(x0 + 1 < v["rect"].cx < x1 - 1 for v in vertical):
                return True
            for r2 in {it[0] for it in items if it[0] != r}:
                under = sorted(extent(it[2]) for it in items if it[0] == r2 and extent(it[2])[0] < x1 and x0 < extent(it[2])[1])
                if any(b[0] > a[1] for a, b in zip(under, under[1:])):
                    return True
            return False

        wide = [it for it in items if spanning(it)]
        intervals = sorted(extent(it[2]) for it in items if it not in wide)
        columns: list[list[float]] = []
        for x0, x1 in intervals:
            if columns and x0 < columns[-1][1] + 1 and not any(columns[-1][1] - 1 < v["rect"].cx < x0 + 1 for v in vertical):
                columns[-1][1] = max(columns[-1][1], x1)
            else:
                columns.append([x0, x1])
        if not columns:
            return None
        bounds = [frame.x0]
        for a, b in zip(columns, columns[1:]):
            rule = [v["rect"].cx for v in vertical if a[1] - 1 <= v["rect"].cx <= b[0] + 1]
            bounds.append(rule[0] if rule else (a[1] + b[0]) / 2)
        bounds.append(frame.x1)

        n_rows, n_cols = len(grid_rows), len(columns)
        cells = [[[] for _ in columns] for _ in grid_rows]
        placed: list[list[list[Span]]] = [[] for _ in columns]
        merges, covered = [], {}
        for it in items:
            r, rs, ch = it
            x0, x1 = extent(ch)
            cols = [i for i in range(n_cols) if bounds[i] < x1 - 0.5 and x0 + 0.5 < bounds[i + 1]]
            if not cols:
                return None
            c0, cs = cols[0], len(cols)
            for rr in range(r, r + rs):
                for cc in range(c0, c0 + cs):
                    if covered.get((rr, cc), it) is not it:
                        return None  # overlapping cells: not a grid we understand
                    covered[(rr, cc)] = it
            cells[r][c0].extend(ch)
            if rs > 1 or cs > 1:
                mid = (bounds[c0] + bounds[c0 + cs]) / 2
                align = "center" if abs((x0 + x1) / 2 - mid) <= 2 else "left" if x0 - bounds[c0] < bounds[c0 + cs] - x1 else "right"
                merges.append({"row": r, "col": c0, "rows": rs, "cols": cs, "align": align})
            else:
                placed[c0].append(ch)
        col_info = []
        for (x0, x1), chunks in zip(columns, placed):
            if all(abs(ch[0].rect.x0 - x0) <= 1 for ch in chunks):
                align = "left"
            elif all(abs(ch[-1].rect.x1 - x1) <= 1 for ch in chunks):
                align = "right"
            else:
                align = "center"
            col_info.append({"x0": round(x0, 2), "x1": round(x1, 2), "align": align})
        rows = grid_rows

        baselines = [statistics.fmean(s.baseline for s in row if s.size >= 0.9 * size) for row in rows]

        # Borders: rules across the whole table stay row rules; partial rules (\cline,
        # \cmidrule) and vertical rules become the borders of the cells they run along.
        def row_boundary(y: float) -> int:
            return sum(b < y for b in baselines)

        borders = []
        full = [h for h in horizontal if h["rect"].x0 <= frame.x0 + 1.5 and h["rect"].x1 >= frame.x1 - 1.5]
        rules = full
        for h in horizontal:
            if h in full:
                continue
            k = row_boundary(h["rect"].cy)
            for cc in range(n_cols):
                if h["rect"].x0 <= bounds[cc] + 2.5 and h["rect"].x1 >= bounds[cc + 1] - 2.5:
                    borders.append({"row": min(k, n_rows - 1), "col": cc, "position": "TOP" if k < n_rows else "BOTTOM",
                                    "color": h["color"], "weight": round(h["weight"], 2)})
        for v in vertical:
            k = min(range(len(bounds)), key=lambda i: abs(bounds[i] - v["rect"].cx))
            if abs(bounds[k] - v["rect"].cx) > 1.5:
                return None  # a rule inside a column
            for rr, b in enumerate(baselines):
                if v["rect"].y0 <= b - 0.5 * size and v["rect"].y1 >= b:
                    borders.append({"row": rr, "col": min(k, n_cols - 1), "position": "LEFT" if k < n_cols else "RIGHT",
                                    "color": v["color"], "weight": round(v["weight"], 2)})
        pitches = [b - a for a, b in zip(baselines, baselines[1:])] or [1.4 * size]
        # Slides rows are at least one line plus 7.2 pt padding above and below, even with the
        # tightest line spacing emit uses (docs/calibration.md); refuse if the taller table
        # would run into content below.
        scale = 720.0 / self.W
        z = size * scale / 1.02
        ratio = min(1.0, max(0.5, (min(pitches) * scale - 14.4) / (1.195 * z)))
        row_h = [max(p * scale, 1.195 * z * ratio + 14.4) / scale for p in pitches + [pitches[-1]]]
        top = baselines[0] - (6.48 + 0.968 * z - (1 - ratio) * 0.9 * z) / scale
        bottom = top + sum(row_h)
        grown = Rect(frame.x0, frame.y1, frame.x1, bottom)
        if bottom > self.H - 2 or any(t.intersects(grown) for t in text_rects) or \
                any(reg.intersects(grown) and not c.expand(0.5).contains_rect(reg) for reg in self.regions):
            return None

        return {
            "id": f"p{self.page['index']}tab{index}", "kind": "table", "role": "table",
            "bbox": c.expand(1.0).as_list(), "frame": frame.as_list(), "size": round(size, 2),
            "row_baselines": [round(b, 2) for b in baselines],
            "row_heights": [round(p, 2) for p in pitches + [pitches[-1]]],
            "columns": col_info,
            "bounds": [round(b, 2) for b in bounds],
            "cells": [[span_runs(cell) for cell in row] for row in cells],
            "merges": merges,
            "rules": [{"row": min(k, len(rows) - 1), "position": "TOP" if k < len(rows) else "BOTTOM",
                       "color": r["color"], "weight": round(r["weight"], 2)}
                      for r in rules for k in [row_boundary(r["rect"].cy)]],
            "borders": borders,
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
        figures = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram")]
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
            if any(Rect.of(e["bbox"]).expand(0.5).contains_rect(r) for e in elements if e["kind"] in ("image", "table", "diagram")):
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
        spans = self.spans()
        self.text_decorations(spans)
        self.analyse_graphics()
        lines = self.build_lines(spans)
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
                # Fraction bars now written as text, underlines and highlight boxes now text
                # styles: they leave the background with the glyphs.
                "strokes": [f[0].as_list() for p in box for l in p.lines for f in l.fractions] +
                           list({tuple(r.as_list()): r.as_list() for p in box for s in p.spans
                                 for r in self.decor_rects.get(s.id, [])}.values()),
            })

        text_spans = {sid for e in elements for sid in e["spans"]}
        elements = self.figures(lines, elements) + elements  # pictures first: they sit below text
        text_spans |= {sid for e in elements if e["kind"] == "table" for sid in e["spans"]}
        elements = self.math_pictures(lines, paragraphs, elements) + elements
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

        # Theme text as ready-made text elements: text that is the same on every slide moves to
        # the slide layout (see promote_theme_text), the rest stays in the background.
        theme_texts = []
        for line in lines:
            if line.reason == "theme" and all(s.id not in used for s in line.spans):
                par = Paragraph([line], align="left")
                theme_texts.append({
                    "kind": "text", "role": "layout", "bbox": line.rect.as_list(), "panel": None, "code": False,
                    # Colour is part of the identity: section navigation highlights the current
                    # section by colour, which must not be frozen onto the layout.
                    "key": [line.text, round(line.rect.x0), round(line.baseline), "".join(s.color for s in line.spans)],
                    "chars": sum(len(s.text.strip()) for s in line.spans),
                    "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": round(line.size, 2),
                                    "text_x0": round(line.x0, 2),
                                    "lines": [{"baseline": round(line.baseline, 2), "x0": round(line.x0, 2),
                                               "x1": round(line.x1, 2)}],
                                    "runs": self.runs(par)}],
                    "spans": [s.id for s in line.spans],
                })

        chars_total = sum(len(s["text"].strip()) for s in self.page["spans"])
        chars_native = sum(len(s["text"].strip()) for s in self.page["spans"] if s["id"] in text_spans)
        return {
            "page": n, "frame": self.page["label"], "size": self.page["size"], "notes": self.page.get("notes"),
            "elements": elements, "left_in_background": left, "theme_texts": theme_texts,
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
                slide["title_page"] = True
                return


def promote_theme_text(slides: list[dict]) -> list[dict]:
    """Theme text (header/footer lines) identical in content and position on every slide
    - author, short title, institute, date - becomes text on the slide layouts, edited once
    for the whole deck. Slide numbers and section navigation differ per slide and stay put."""
    if len(slides) < 2:
        for s in slides:
            s["on_layout"] = []
        return []
    keys = [{tuple(t["key"]): t for t in s["theme_texts"]} for s in slides]
    common = set(keys[0]).intersection(*keys[1:])
    for slide, by_key in zip(slides, keys):
        moved = {sid for k in common for sid in by_key[k]["spans"]}
        slide["on_layout"] = sorted(moved)
        for left in slide["left_in_background"]:
            keep = [i for i, sid in enumerate(left["spans"]) if sid not in moved]
            left["spans"] = [left["spans"][i] for i in keep]
            left["bboxes"] = [left["bboxes"][i] for i in keep]
        slide["left_in_background"] = [l for l in slide["left_in_background"] if l["spans"]]
        slide["stats"]["chars_native"] += sum(by_key[k]["chars"] for k in common)
    return [keys[0][k] for k in sorted(common, key=lambda k: (k[2], k[1]))]


def mark_big_headings(slides: list[dict], body: float) -> None:
    """Slides without a frame title (section pages, "Thank you!") use their single, clearly
    largest heading as the title, so it shows up in Slides' outline and navigation."""
    for slide in slides:
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        if not texts or any(e["role"] == "title" for e in texts):
            continue
        sizes = sorted((max(p["size"] for p in e["paragraphs"]), i) for i, e in enumerate(texts))
        size, i = sizes[-1]
        runner_up = sizes[-2][0] if len(sizes) > 1 else 0.0
        if size >= 1.3 * body and size >= 1.15 * runner_up and texts[i]["paragraphs"][0]["size"] == size:
            texts[i]["role"] = "title"


def classify(raw: dict) -> dict:
    body = body_size(raw)
    slides = [PageClassifier(page, body).classify() for page in raw["pages"]]
    mark_title_page(slides, raw["source"].get("title", ""))
    mark_big_headings(slides, body)
    layout_texts = promote_theme_text(slides)
    chars = sum(s["stats"]["chars"] for s in slides)
    native = sum(s["stats"]["chars_native"] for s in slides)
    return {
        "version": 1, "source": raw["source"], "body_size": body,
        "stats": {"chars": chars, "chars_native": native, "native_share": round(native / chars, 3) if chars else 0},
        "layout_texts": layout_texts,
        "slides": slides,
    }
