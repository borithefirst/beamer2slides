"""The page as classify sees it: rectangles, and the spans, lines and paragraphs of its text."""

import functools
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, Union

from . import bidi, ir
from .fonts import FontInfo
from .raw_types import PathItem, RawDrawing


# Math extension fonts: big operators, big delimiters, radical signs - glyphs that hang from their
# origin (Computer Modern's CMEX, Latin Modern's LMMathExtension, txfonts/pxfonts/newtx's).
EXTENSION_FONT_RE = re.compile(r"^(CMEX|EUEX|ESINT|(NEW)?(N?TX|PX)EX)|MATHEXTENSION")


@functools.lru_cache(maxsize=None)
def extension_font(font: str) -> bool:
    # (a deck has a few dozen font names, asked about 770,000 times on an 86-slide deck)
    return bool(EXTENSION_FONT_RE.search(re.sub(r"[^A-Z0-9]", "", font.split("+", 1)[-1].upper())))


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, values: Sequence[float]) -> "Rect":
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

    def contains_rect(self, o: "Rect", tol: float) -> bool:
        """`o` lies within this rectangle grown by `tol` (0.5: a stroke's half width, rounding)."""
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


def union_all(rects: Iterable[Rect]) -> Rect:
    items = list(rects)
    out = items[0]
    for r in items[1:]:
        out = out.union(r)
    return out


def cluster_rects(rects: list[Rect], gap: float) -> list[Rect]:
    """Merge rectangles that come within `gap` of each other, transitively."""
    parent = list(range(len(rects)))

    def find(i: int) -> int:
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


@dataclass(eq=False, kw_only=True)
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
    link: str | None
    underline: bool
    strike: bool          # \sout
    highlight: str | None  # background colour (\colorbox)
    drawn: bool           # a character the PDF draws as a rule, not a glyph (underscores)
    decor_to: float | None  # where its underline, strike or highlight ends, when before its end
    pad_left: bool         # the first / last word of a padded \colorbox highlight
    pad_right: bool
    visual: str | None     # the text as the page shows it, left to right (bidi: RTL lines)
    reading: "Reading | None"  # on a right-to-left line (read_lines)


def new_span(*, id: str, text: str, font: str, size: float, color: str, rect: Rect, baseline: float,
             horizontal: bool, info: FontInfo, link: str | None, drawn: bool, visual: str | None) -> Span:
    """A span as the page gives it, before the graphics around it are read: no underline, strike or
    highlight yet (`text_decorations`: `underline`, `strike`, `highlight`, `decor_to`, `pad_left`,
    `pad_right`), and not yet read in its line's order (`read_lines`: `reading`)."""
    return Span(id=id, text=text, font=font, size=size, color=color, rect=rect, baseline=baseline,
                horizontal=horizontal, info=info, link=link, underline=False, strike=False, highlight=None,
                drawn=drawn, decor_to=None, pad_left=False, pad_right=False, visual=visual, reading=None)


Reading = tuple[int, int, int, float]
"""Where a span stands in its line's reading order (`PageClassifier.read_lines`): the line's
index, the span's rank in it, the line's base direction (`bidi.RIGHT`...), and the width of the
space before it that reading order moved."""

Fraction = tuple[Rect, list[Span], list[Span]]
"""A small inline fraction Slides text carries: its bar, its numerator's and denominator's spans."""


class IconBullet(TypedDict):
    """A list item's mark drawn in a symbol font or as a small graphic (`assign_reasons`): no
    bullet of deck.json's, but a picture grouped with the item (`PageClassifier.classify`)."""
    kind: Literal["icon"]
    text: str
    bbox: list[float]
    spans: list[str]


LineBullet = Union[ir.GlyphBullet, ir.NumberBullet, ir.ImageBullet, ir.ShapeBullet, IconBullet]
"""What a line's bullet is while classifying: one of deck.json's, or an icon."""


def ir_bullet(b: LineBullet | None) -> ir.Bullet | None:
    """The bullet deck.json writes for a line's: an icon is no bullet (its picture stands in)."""
    if b is None or b["kind"] == "icon":
        return None
    return b


@dataclass(eq=False, kw_only=True)
class Line:
    spans: list[Span]
    bullet: LineBullet | None
    bullet_spans: list[Span]
    reason: str | None
    inline_math: bool
    fractions: list[Fraction]
    tab: Span | None  # content after a line label ("4:") starts here, reached by a tab
    holes: list[list[Span]]  # complex inline formulas: pictures over gaps in the text
    hole_pads: list[Rect]  # graphics drawn around words of a hole (a circle, a badge)
    limits: list["Line"]  # lines of the limits of a big operator in a hole (∑ with n=1 and ∞)
    code_number: bool  # a listing's line number (split_line_numbers): a paragraph of its own
    def hole_rect(self, hole: list[Span]) -> "Rect":
        """A hole's extent: its glyphs and the graphics drawn around them."""
        rect = union_all(s.rect for s in hole)
        return union_all([rect] + [g for g in self.hole_pads if g.intersects(rect.expand(0.5))])

    def add_holes(self, groups: list[list[Span]]) -> None:
        """Merge new holes with the line's: overlapping holes become one, and a word lying over
        or under a hole (a wavy underline's glyphs below it) or kerned into it (the "TEX" of the
        LaTeX logo) joins it."""
        holes = [list(h) for h in self.holes + groups]

        def off_baseline(s: Span) -> bool:
            return abs(s.baseline - self.baseline) > 0.1 * self.size
        for h in holes:
            grown = True
            while grown:
                r = self.hole_rect(h)

                def overlap_x(s: Span) -> float:
                    return min(s.rect.x1, r.x1) - max(s.rect.x0, r.x0)
                # (a letter raised or lowered into its neighbour, not an italic overhang)
                more = [s for s in self.content if s not in h and s.text.strip() and s.rect.w > 0
                        and (overlap_x(s) >= 0.5 * s.rect.w or
                             (overlap_x(s) >= min(1.0, 0.5 * s.rect.w) and (off_baseline(s) or any(map(off_baseline, h)))))]
                h += more
                grown = bool(more)
        def side_by_side(a: list[Span], b: list[Span]) -> bool:
            """Two holes with no word between them (\\uwave{all benchmarks}: a picture per word,
            kerned together): Slides' text has one gap there, as wide as both, and each picture
            was measured into the same gap, one over the other. They are one hole."""
            ra, rb = sorted((self.hole_rect(a), self.hole_rect(b)), key=lambda r: r.x0)
            if rb.x0 - ra.x1 > 0.5 * self.size:
                return False
            return not any(s not in a and s not in b and s.text.strip() and ra.x1 - 0.5 < s.rect.cx < rb.x0 + 0.5
                           for s in self.content)

        merged = True
        while merged:
            merged = False
            for i, a in enumerate(holes):
                for b in holes[i + 1:]:
                    if set(map(id, a)) & set(map(id, b)) or self.hole_rect(a).intersects(self.hole_rect(b)) \
                            or side_by_side(a, b):
                        a += [s for s in b if s not in a]
                        holes.remove(b)
                        merged = True
                        break
                if merged:
                    break
        self.holes = [sorted(h, key=lambda s: s.rect.x0) for h in holes]

    def __post_init__(self) -> None:
        self.spans.sort(key=lambda s: s.rect.x0)
        # An accent reaching left of its letter follows the letter (it becomes a combining mark).
        for i in range(len(self.spans) - 1):
            a, b = self.spans[i], self.spans[i + 1]
            if a.text.strip() in ACCENTS and b.text.strip() not in ACCENTS and \
                    min(a.rect.x1, b.rect.x1) - max(a.rect.x0, b.rect.x0) > 0.5 * a.rect.w:
                self.spans[i], self.spans[i + 1] = b, a

    @property
    def rect(self) -> Rect:
        return union_all(s.rect for s in self.spans)

    @property
    def main(self) -> Span:
        top = max(s.size for s in self.spans)
        big = [s for s in self.spans if s.size >= 0.9 * top]
        # (not a big-operator or brace glyph: it sits off the baseline)
        main = max(big, key=lambda s: (not extension_font(s.font), len(s.text.strip())))
        # A span's baseline is its first glyph's: a listing's lowered '*' or raised '_' opening
        # the longest span ("*)NULL);", "_exit(127);") put the whole line 2 pt off, and
        # Slides set two code lines almost touching (r2_code_v3 s2). The line's baseline is the
        # one most of its letters stand on.
        def letters(ss: list[Span]) -> int:
            return sum(len(s.text.strip()) for s in ss)

        def on(b: float) -> list[Span]:
            return [s for s in big if abs(s.baseline - b) <= 0.05 * top]
        if 2 * letters(on(main.baseline)) >= letters(big):
            return main
        return max(big, key=lambda s: (not extension_font(s.font), letters(on(s.baseline)), len(s.text.strip())))

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
        return " ".join(s.text.strip() for s in bidi.logical_spans(self.spans))


def new_line(spans: list[Span]) -> Line:
    """A line of `spans` before anything is found about it: the passes that follow give it its
    bullet, reason, inline math, fractions, tab, holes and limits, and mark a listing's number."""
    return Line(spans=spans, bullet=None, bullet_spans=[], reason=None, inline_math=False, fractions=[], tab=None,
                holes=[], hole_pads=[], limits=[], code_number=False)


def reads_rtl(line: Line) -> bool:
    """The line was read right to left (`PageClassifier.read_lines`: its base direction), so it
    starts at its right end - where a Hebrew item's bullet hangs."""
    bases = {s.reading[2] for s in line.spans if s.reading}
    return bases == {bidi.RIGHT}


@dataclass(eq=False, kw_only=True)
class Paragraph:
    lines: list[Line]
    align: ir.Align
    reason: str | None
    role: ir.TextRole
    level: int
    indent: float  # a first line set in by \parindent: how far right of the others it starts
    justified: bool

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
    def bullet(self) -> LineBullet | None:
        return self.first.bullet

    @property
    def direction(self) -> Literal["rtl"] | None:
        """`rtl` where the paragraph reads right to left, else None - Unicode's P2 over the
        words as they are now read (`bidi`). Slides has to be told: in a paragraph it takes
        for left-to-right, a Hebrew sentence's full stop lands at the wrong end, a bullet
        hangs on the wrong side and the cursor walks the wrong way."""
        bases = {s.reading[2] for s in self.lines[0].spans if s.reading} if self.lines else set()
        if len(bases) == 1:  # the way its first line was read (PageClassifier.read_lines)
            return "rtl" if bases.pop() == bidi.RIGHT else None
        return "rtl" if bidi.reads_rtl(" ".join(l.text for l in self.lines)) else None

    @property
    def x0(self) -> float:
        return self.first.x0

    @property
    def rect(self) -> Rect:
        return union_all(l.rect for l in self.lines)

    @property
    def spans(self) -> list[Span]:
        return [s for l in self.lines for s in l.spans]


def new_paragraph(lines: list[Line], *, align: ir.Align, reason: str | None) -> Paragraph:
    """A paragraph of `lines` before its place and measures are found: a body paragraph at list
    level 0, no `\\parindent` first line, not justified (the passes that follow decide `role`,
    `level`, `indent` and `justified`)."""
    return Paragraph(lines=lines, align=align, reason=reason, role="body", level=0, indent=0.0, justified=False)


# Accents TeX sets as glyphs of their own over a letter (\bar{X}, and every accent in the OT1
# encoding, pdflatex's default: Schr¨odinger): combining marks in Slides.
ACCENTS = {"¯": "̄", "ˆ": "̂", "˜": "̃", "˙": "̇", "¨": "̈", "´": "́",
           "`": "̀", "ˇ": "̌", "˘": "̆", "˚": "̊", "˝": "̋", "¸": "̧", "˛": "̨"}


def polygon_shape(points: Sequence[Sequence[float]], r: "Rect") -> Literal["DIAMOND", "TRIANGLE"] | None:
    """Slides shape for a closed polygon path: a diamond touching the middle of each side of its
    bounding box, or a triangle with its apex centred on the top or bottom side."""
    tol = 0.08 * max(r.w, r.h)
    def near(p: Sequence[float], x: float, y: float) -> bool:
        return abs(p[0] - x) <= tol and abs(p[1] - y) <= tol
    corners = {(round(p[0], 1), round(p[1], 1)) for p in points}
    mids = [(r.cx, r.y0), (r.x1, r.cy), (r.cx, r.y1), (r.x0, r.cy)]
    if len(corners) == 4 and all(any(near(p, *m) for p in corners) for m in mids):
        return "DIAMOND"
    if len(corners) == 3:
        if any(near(p, r.cx, r.y0) for p in corners) and any(near(p, r.x0, r.y1) for p in corners) \
                and any(near(p, r.x1, r.y1) for p in corners):
            return "TRIANGLE"
    return None


MITER_LIMIT = 10.0  # PDF's default, and TikZ's


def miter_reach(points: Sequence[Sequence[float]], direction: tuple[float, float], width: float) -> float:
    """How far an arrow head's outline, stroked `width` wide with mitred joins, reaches past the
    point of its path furthest along `direction`: half the width over the sine of half the angle
    at that point (a bevel's half width beyond the miter limit)."""
    if width <= 0 or len(points) < 3:
        return 0.0
    ux, uy = direction
    def along(p: Sequence[float]) -> float:
        return p[0] * ux + p[1] * uy
    corners = [tuple(p) for k, p in enumerate(points) if k == 0 or math.dist(p, points[k - 1]) > 1e-6]
    if len(corners) > 2 and math.dist(corners[0], corners[-1]) <= 1e-6:
        corners.pop()
    if len(corners) < 3:
        return 0.0
    k = max(range(len(corners)), key=lambda i: along(corners[i]))
    apex, a, b = corners[k], corners[k - 1], corners[(k + 1) % len(corners)]
    va, vb = (a[0] - apex[0], a[1] - apex[1]), (b[0] - apex[0], b[1] - apex[1])
    na, nb = math.hypot(*va), math.hypot(*vb)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    cos = max(-1.0, min(1.0, (va[0] * vb[0] + va[1] * vb[1]) / (na * nb)))
    half = math.acos(cos) / 2
    if half < 1e-3 or 1 / math.sin(half) > MITER_LIMIT:
        return width / 2
    return width / 2 / math.sin(half)


def upright_ellipse(path: list[PathItem], r: Rect) -> bool:
    """Four curves closing an ellipse whose axes are the box's: they join end to start, and
    meet the box at the middle of each side. A sine wave is four curves too (TikZ's sin cos
    sin cos), and a rotated or sheared ellipse touches its box elsewhere: as an ELLIPSE of the
    box they came out a closed upright ring."""
    if [op for op, _ in path] != ["c"] * 4:
        return False
    tol = 0.05 * max(r.w, r.h) + 0.1
    ends = [pts[-1] for _, pts in path]
    if any(math.dist(pts[0], prev) > tol for (_, pts), prev in zip(path, ends[-1:] + ends[:-1])):
        return False  # open (a wave), or pieces of different outlines
    mids = [(r.cx, r.y0), (r.x1, r.cy), (r.cx, r.y1), (r.x0, r.cy)]
    return all(any(math.dist(e, m) <= tol for e in ends) for m in mids)


def box_outline(d: RawDrawing, r: Rect) -> bool:
    """A path that outlines its own bounding box: one rectangle, or one outline of axis-aligned
    edges with rounded corners (a beamer block). A panel is rebuilt as a shape of its box, so
    a funnel's trapezium, a band between two curves or a bar series (one path, a rectangle per
    bar) would turn into one big rectangle."""
    path = d.get("path")
    if not path:
        return False
    if [op for op, _ in path] == ["re"]:
        return True
    ops = {op for op, _ in path}
    if not ops <= {"l", "c"} or "l" not in ops:
        return False
    tol = max(0.1, 0.01 * max(r.w, r.h))
    def on_border(x: float, y: float) -> bool:
        return min(abs(x - r.x0), abs(x - r.x1)) <= tol or min(abs(y - r.y0), abs(y - r.y1)) <= tol
    end: list[float] | None = None
    for op, pts in path:
        if end is not None and math.dist(pts[0], end) > tol:
            return False  # a second outline (another bar)
        if not all(on_border(x, y) for x, y in pts):
            return False
        if op == "l" and abs(pts[0][0] - pts[-1][0]) > tol and abs(pts[0][1] - pts[-1][1]) > tol:
            return False  # a slanted edge
        end = pts[-1]
    return True


OUTLINE_MIN = 0.3  # pt: a stroke this wide around a filled bullet shows as an outline


SMALL_IMAGE_PT = 12

HOLE_PAD = 1.0  # pt of page around an inline formula picture (antialiasing, italic overhang)
