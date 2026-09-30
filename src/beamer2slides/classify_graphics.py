"""PageClassifier's graphics: theme decoration, panels and their frames, underlines,
strike-throughs and highlights, and the beamer blocks that become shapes.
"""

import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from .classify_model import (
    SMALL_IMAGE_PT, Line, Rect, Span, box_outline, cluster_rects, extension_font, new_span, overlap, union_all,
)
from .classify_state import BareFrame, Frame, Panel, PathKey, Rule
from .classify_tables import TablesMixin
from .fonts import font_info
from .ir import Element, ShapeElement, ShapeKind
from .raw_types import RawColor, RawDrawing, RawPage, RawSpan

FRAME_RULE_PT = 1.5  # a filled box this thin is a rule (\fcolorbox's \fboxrule, a tcolorbox's frame)
FRAME_REACH = 1.5    # how far a frame's rule may lie from the edge of the box it frames
DECOR_SHORT = 0.12 # em a decoration ends before its span's end for its trailing punctuation to be left out
PAGE_FRAME_INSET = 0.075  # of the page's width and height: how far in from each edge a border framing the page lies

Axis = Literal["h", "v"]
Piece = tuple[float, float, float]
"""A rule's piece along a box's edge: where it starts and ends along the edge, and where it lies across."""


@dataclass(frozen=True, kw_only=True)
class _Framing:
    """One colour's rules along a box's edges, as `frame_of` gathers them (its lists grow)."""
    edges: dict[str, list[Piece]]
    """'t', 'b', 'l', 'r' -> the pieces along that edge."""
    widths: list[float]
    ids: list[str]
    edge_of: list[str]
    """Each rule's edge, in the order of `ids`."""
    rects: list[Rect]


def page_frame(page: RawPage) -> set[str]:
    """The strokes of a border framing the whole page a little in from its edges, as a theme draws
    one in its background canvas (a parchment's double rule): an outline of its own box reaching
    to within `PAGE_FRAME_INSET` of every edge, or straight lines along all four edges, each
    running nearly the page's length there. They are background, as the page's own fill is: as
    a graphic an inset rectangle made one figure region of the whole page, every line in it a
    label of a figure too big to crop, and the slide one picture; drawn as lines, the top and
    bottom ones framed rows of text and were a table's rules around the page. (Only strokes: a
    filled box is a panel. Never a drawing whose mark says what it is.)"""
    w, h = page["size"]
    dx, dy = PAGE_FRAME_INSET * w, PAGE_FRAME_INSET * h
    frame: set[str] = set()
    sides: dict[str, list[str]] = {}
    for d in page["drawings"]:
        r = Rect.of(d["bbox"])
        if d["type"] != "s" or d.get("marks"):
            continue
        lines = set(d["items"]) == {"l"}
        if box_outline(d, r) and r.x0 <= dx and r.y0 <= dy and r.x1 >= w - dx and r.y1 >= h - dy:
            frame.add(d["id"])
        elif lines and r.h <= 0.5 * dy and r.x0 <= dx and r.x1 >= w - dx and (r.y1 <= dy or r.y0 >= h - dy):
            sides.setdefault("top" if r.y1 <= dy else "bottom", []).append(d["id"])
        elif lines and r.w <= 0.5 * dx and r.y0 <= dy and r.y1 >= h - dy and (r.x1 <= dx or r.x0 >= w - dx):
            sides.setdefault("left" if r.x1 <= dx else "right", []).append(d["id"])
    if len(sides) == 4:
        frame.update(i for ids in sides.values() for i in ids)
    return frame


def without_page_frame(page: RawPage) -> RawPage:
    """The page as the classifier reads it: without its `page_frame`, which stays in the
    background picture."""
    frame = page_frame(page)
    if not frame:
        return page
    out = page.copy()
    out["drawings"] = [d for d in page["drawings"] if d["id"] not in frame]
    return out


def drawings_of(e: Element) -> list[str]:
    """The raw drawings an element names as its own (an overlay's, a marked table's)."""
    if e["kind"] == "image" or e["kind"] == "table" or e["kind"] == "shape":
        return e.get("drawings", [])
    return []


class GraphicsMixin(TablesMixin):
    """PageClassifier's graphics (after its tables, whose hairlines `analyse_graphics` asks for)."""

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
        """Underlines, strike-throughs and \\colorbox highlights become text styles: a thin rule
        just below or through a stretch of words, or a filled box tightly around words and
        touching no other graphics.
        Sets the span attributes and remembers the drawings (they leave the background with
        the text, and are not graphics)."""
        self.decor_ids = set()
        self.decor_rects = {}
        flat = [s for s in spans if s.horizontal and s.text.strip()]
        drawings = [(d, Rect.of(d["bbox"])) for d in self.raw["drawings"]]

        def is_rule(d: RawDrawing, r: Rect) -> bool:
            return r.h <= 1.2 and ((d["type"] == "f" and d["items"] == "re") or (d["type"] == "s" and d["items"] == "l"))

        # ulem draws a rule per word and per space, soul a rule or box per word piece: pieces
        # touching or overlapping end to end are one rule or box.
        def is_box(d: RawDrawing, r: Rect) -> bool:
            return d["type"] == "f" and d["items"] == "re" and bool(d.get("fill")) and not is_rule(d, r)
        candidates: list[tuple[list[RawDrawing], Rect]] = [([d], r) for d, r in drawings if not is_rule(d, r) and not is_box(d, r)]
        pieces = sorted(((d, r) for d, r in drawings if is_rule(d, r) or is_box(d, r)), key=lambda x: (round(x[1].cy), x[1].x0))
        for d, r in pieces:
            last = next((c for c in reversed(candidates) if is_rule(c[0][0], c[1]) == is_rule(d, r)), None)
            if last and last[0][0]["type"] == d["type"] and abs(last[1].cy - r.cy) <= 0.2 and -0.6 <= r.x0 - last[1].x1 <= 1 \
                    and (is_rule(d, r) or (abs(last[1].h - r.h) <= 0.2 and last[0][0]["fill"] == d["fill"] and r.x0 < last[1].x1)):
                candidates[candidates.index(last)] = (last[0] + [d], last[1].union(r))
            else:
                candidates.append(([d], r))

        def run_of(s: Span) -> Rect:
            """The words joined to s on its row."""
            row = sorted((o for o in flat if abs(o.baseline - s.baseline) <= 0.1 * s.size), key=lambda o: o.rect.x0)
            j0 = j1 = row.index(s)
            while j0 > 0 and row[j0].rect.x0 - row[j0 - 1].rect.x1 <= 0.6 * s.size:
                j0 -= 1
            while j1 < len(row) - 1 and row[j1 + 1].rect.x0 - row[j1].rect.x1 <= 0.6 * s.size:
                j1 += 1
            return Rect(row[j0].rect.x0, s.rect.y0, row[j1].rect.x1, s.rect.y1)

        for group, r in candidates:
            d = group[0]
            # (ulem's pieces under a long underlined line join into a rule wider than half the
            # page, which is_decoration takes for a theme hairline: the line then read as a
            # formula over two dozen fraction bars. A theme's hairline is one piece.)
            chain = len(group) >= 2 and is_rule(d, r)
            if (self.is_decoration(r) and not chain) or r.w < 2 or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            ops = d["items"]
            if is_rule(d, r):
                def within(s: Span) -> bool:
                    return r.x0 - 1 <= s.rect.x0 and s.rect.x1 <= r.x1 + max(1.0, 0.3 * s.size)  # "out," past the rule
                words = sorted((s for s in flat if within(s) and 0 < r.cy - s.baseline <= 0.45 * s.size), key=lambda s: s.rect.x0)
                # \sout: the rule runs through the middle of the lower-case letters.
                struck = sorted((s for s in flat if within(s) and 0.12 * s.size <= s.baseline - r.cy <= 0.4 * s.size),
                                key=lambda s: s.rect.x0)
                strike = not words
                words = words or struck
                if not words:
                    continue
                size = max(s.size for s in words)
                gaps = [b.rect.x0 - a.rect.x1 for a, b in zip(words, words[1:])]
                covered = sum(s.rect.w for s in words)

                # (a rule over words below it: an overline, a fraction bar; not the next line of
                # text under a wrapped underline, whose words run on past the rule)
                # (nor the short last line of a wrapped underlined paragraph, flush with the rule's
                # start and ending far before its end: a fraction's part is centred on its bar)
                def flush_line(o: Span) -> bool:
                    return abs(run_of(o).x0 - r.x0) <= 1 and r.x1 - run_of(o).x1 > size and o.baseline - r.cy >= 0.9 * size
                below = not strike and any(s.rect.x0 < r.x1 and r.x0 < s.rect.x1 and s.baseline > r.cy and s.rect.y0 < r.cy + 0.25 * size
                                           and (s.baseline - r.cy < 0.75 * size or r.expand(size).contains(run_of(s).x0, r.cy)
                                                and r.expand(size).contains(run_of(s).x1, r.cy) and not flush_line(s))
                                           for s in flat)
                if below or covered < 0.8 * r.w or any(g > 0.6 * size for g in gaps) or \
                        (strike and max(s.baseline for s in words) - min(s.baseline for s in words) > 0.1 * size) or \
                        abs(words[0].rect.x0 - r.x0) > 0.3 * size or abs(words[-1].rect.x1 - r.x1) > 0.3 * size:
                    continue
                for s in words:
                    if strike:
                        s.strike = True
                    else:
                        s.underline = True
            elif d["type"] == "f" and ops == "re" and d.get("fill") and d.get("fill_opacity", 1.0) >= 0.99:
                # (soul's \hl ends before punctuation that follows in the same span: "words,")
                def punct(s: Span) -> float:
                    return 0.3 * s.size if s.text.rstrip()[-1:] in ",.;:!?)" and s.text.rstrip()[-2:-1].isalnum() else 0.0
                inside = [s for s in flat if r.contains_rect(Rect(s.rect.x0, s.rect.y0, max(min(s.rect.x1, r.x1), s.rect.x1 - punct(s)),
                                                                   s.rect.y1), tol=0.5)]
                if not inside or any(s.rect.intersects(r) and s not in inside for s in flat):
                    continue
                size = max(s.size for s in inside)
                if not 0.9 * size <= r.h <= 2.2 * size or len({round(s.baseline) for s in inside}) != 1:
                    continue
                if sum(s.rect.w for s in inside) < 0.6 * r.w or \
                        any(o not in group and ro.expand(1).intersects(r) and not r.contains_rect(ro, tol=0)
                            for o, ro in drawings if ro.w * ro.h < 0.95 * self.W * self.H):
                    continue  # part of a figure (a filled TikZ node with lines attached)
                if any(o not in group and not is_rule(o, ro) and r.contains_rect(ro, tol=0) and not ro.contains_rect(r, tol=0.5)
                       for o, ro in drawings):
                    continue  # a box holding marks besides its words: a legend's swatches
                row = sorted(inside, key=lambda s: s.rect.x0)
                # \colorbox pads its words by \fboxsep (3 pt), soul's \hl by a quarter point: a
                # padded box alone on its line is a panel under its words (as a highlight it
                # shrank to the glyphs), one within a line keeps its padding as highlighted
                # no-break spaces (runs).
                padded = min(row[0].rect.x0 - r.x0, r.x1 - row[-1].rect.x1) >= max(1.5, 0.15 * size)
                if padded and r.w >= 0.25 * self.W and not any(
                        abs(o.baseline - row[0].baseline) <= 0.1 * size and o not in inside for o in flat):
                    continue
                for s in inside:
                    s.highlight = d["fill"]
                if padded:
                    row[0].pad_left = row[-1].pad_right = True
                words = inside
            else:
                continue
            for s in words:
                # (\uline{matches}, and \hl{...}. end before the punctuation their span goes on
                # with: runs cut it off undecorated, or two phrases joined into one across ", ")
                if r.x1 < s.rect.x1 - DECOR_SHORT * s.size:
                    s.decor_to = r.x1
            self.decor_ids |= {g["id"] for g in group}
            for s in words:
                self.decor_rects.setdefault(s.id, []).append(r)

    def underscores(self, spans: list[Span]) -> None:
        """OT1, beamer's default encoding, has no underscore glyph: `\\_` is a rule TeX draws
        0.3 em long on the baseline (`\\kern.06em\\vbox{\\hrule width.3em}`), so "x86\\_64"
        reaches the PDF as two words with a line between them, which read as a word on a small
        graphic (a hole, `graphic_holes`) or as a formula. A rule that short, level with the
        baseline and right beside a glyph of that line, is an underscore: a span "_" joins the
        words (`drawn`: it has no page object), and the rule leaves the background with the
        text as an underline does (`decor_rects`)."""
        # (never beside a radical sign, which hangs from its origin like a CMEX glyph: its overbar
        # is level with that origin, and has its radicand right under it where an underscore
        # has nothing)
        flat = [s for s in spans if s.horizontal and s.text.strip()]
        signs = [s for s in flat if s.font.startswith("CMEX") or "√" in s.text]
        flat = [s for s in flat if s not in signs]
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or r.h > 0.8 or \
                    not ((d["type"] == "s" and d["items"] == "l") or (d["type"] == "f" and d["items"] == "re")):
                continue
            beside = [s for s in flat if 0.2 * s.size <= r.w <= 0.7 * s.size and abs(r.cy - s.baseline) <= 0.12 * s.size
                      and (-0.5 <= r.x0 - s.rect.x1 <= 0.25 * s.size or -0.5 <= s.rect.x0 - r.x1 <= 0.25 * s.size)]
            if not beside or any(s.rect.x0 < r.x1 and r.x0 < s.rect.x1 and r.cy < s.rect.cy < r.cy + beside[0].size
                                 for s in flat) or \
                    any(abs(s.rect.x1 - r.x0) <= 1 or abs(r.x1 - s.rect.x0) <= 1 for s in signs):
                continue  # an overbar or a fraction bar: something is set under it
            like = beside[0]
            span = new_span(id=f"{d['id']}u", text="_", font=like.font, size=like.size, color=like.color,
                            rect=Rect(r.x0, like.rect.y0, r.x1, like.rect.y1), baseline=like.baseline,
                            horizontal=True, info=like.info, link=like.link, drawn=True, visual=None)
            spans.append(span)
            self.decor_ids.add(d["id"])
            self.decor_rects[span.id] = [r]

    def typeset_fraction(self, r: Rect) -> bool:
        """A stroke is a fraction's bar, however long, when words sit right on it and right
        under it, all within its ends, one side running from end to end (TeX makes the bar as
        long as the wider part) and the other centred on it. As a graphic it became a figure
        region with the words at it, and the numerator or denominator went with that figure
        or into the background while the rest of the formula stayed text."""
        # the glyphs of each part, scripts further off the bar too: (box, size)
        above: list[tuple[list[float], float]] = []
        below: list[tuple[list[float], float]] = []
        touching = [False, False]
        for s in self.raw["spans"]:
            x0, y0, x1, y1 = s["bbox"]
            if x1 <= r.x0 or x0 >= r.x1 or not s["text"].strip():
                continue
            if r.y0 - 0.8 * self.body <= y1 <= r.y0 + 0.5:
                above.append((s["bbox"], s["size"]))
                on = y1 >= r.y0 - 0.4 * s["size"]
                touching[0] |= on
            elif r.y1 - 1 <= y0 <= r.y1 + 0.8 * self.body:
                below.append((s["bbox"], s["size"]))
                on = y0 <= r.y1 + 0.4 * s["size"]
                touching[1] |= on
            else:
                continue
            if on and (x0 < r.x0 - 1 or x1 > r.x1 + 1):
                return False  # a word across its end: an underline or a rule under a heading
        if not all(touching):
            return False
        for side in (above, below):  # one run of glyphs each, not a table's cells
            side.sort(key=lambda b: b[0][0])
            size = max(b[1] for b in side)
            if any(b[0][0] - max(a[0][2] for a in side[:i + 1]) > 0.7 * size for i, b in enumerate(side[1:])):
                return False
        sides = [(min(b[0] for b in boxes), max(b[2] for b in boxes))
                 for boxes in ([b for b, _ in above], [b for b, _ in below])]
        full = [abs(a - r.x0) <= 1 and abs(b - r.x1) <= 1 for a, b in sides]
        centred = [abs((a + b) / 2 - r.cx) <= 0.05 * r.w + 1 for a, b in sides]
        return any(full) and all(centred)

    @staticmethod
    def thin_rule(d: RawDrawing) -> tuple[Axis, float, RawColor] | None:
        """A drawing that is a straight rule: ('h' or 'v', its thickness, its colour) - a stroked
        line, or a filled box thinner than FRAME_RULE_PT."""
        r = Rect.of(d["bbox"])
        stroke, fill = d.get("stroke"), d.get("fill")
        axis: Axis = "h" if r.w > r.h else "v"
        if d["type"] == "s" and d["items"] == "l" and stroke and min(r.w, r.h) <= 0.1:
            return axis, (d.get("width") or 0.4), stroke
        if d["type"] == "f" and d["items"] == "re" and fill and d.get("fill_opacity", 1.0) >= 0.99 \
                and min(r.w, r.h) <= FRAME_RULE_PT and max(r.w, r.h) >= 2:
            return axis, min(r.w, r.h), fill
        return None

    def frame_of(self, box: Rect, fill: RawColor | None, drawings: list[RawDrawing], partial: bool) -> Frame | None:
        """The frame drawn around a filled box: rules along all four of its edges, in one colour
        other than the fill, covering each edge, with no other rule inside (then the box is a
        table's). \\fcolorbox draws its fill, then four rules on its outer edge; listings'
        frame=single a rule per line on each side and one above and below. Returns the outline
        (its colour, width, sides, and box: the rules' centre lines) and the ids of the drawings
        it takes (with rules in the fill's own colour over it: listings paints the side strips
        twice). `partial`: some of the sides are enough (listings' frame=lines, leftline)."""
        by_color: dict[RawColor, _Framing] = {}
        same: list[str] = []
        for d in drawings:
            rule = self.thin_rule(d)
            if rule is None:
                continue
            axis, width, c = rule
            r = Rect.of(d["bbox"])
            if not box.expand(FRAME_REACH).contains_rect(r, tol=0):
                continue  # (a footnote rule reaching out under the box)
            if c == fill:
                same.append(d["id"])
                continue
            mid = r.cy if axis == "h" else r.cx
            near = [e for e, at in (("t", box.y0), ("b", box.y1)) if axis == "h" and abs(mid - at) <= FRAME_REACH] + \
                   [e for e, at in (("l", box.x0), ("r", box.x1)) if axis == "v" and abs(mid - at) <= FRAME_REACH]
            if not near:
                return None  # a rule across the box: a table's, or a figure's
            got = by_color.get(c)
            if got is None:
                got = by_color[c] = _Framing(edges={"t": [], "b": [], "l": [], "r": []}, widths=[], ids=[], edge_of=[], rects=[])
            got.edges[near[0]].append((r.x0, r.x1, mid) if axis == "h" else (r.y0, r.y1, mid))
            got.widths.append(width)
            got.ids.append(d["id"])
            got.edge_of.append(near[0])
            got.rects.append(r if min(r.w, r.h) >= width - 0.05 else
                             (Rect(r.x0, mid - width / 2, r.x1, mid + width / 2) if axis == "h"
                              else Rect(mid - width / 2, r.y0, mid + width / 2, r.y1)))

        words = [Rect.of(s["bbox"]) for s in self.raw["spans"] if s["text"].strip()]

        def labelled(e: str, a: float, b: float, at: float) -> bool:
            """A word set in the frame's rule (fancyvrb's label, a titled frame) opens it."""
            return any(w.y0 < at < w.y1 and a <= w.cx <= b if e in "tb" else w.x0 < at < w.x1 and a <= w.cy <= b
                       for w in words)

        def sides(edges: dict[str, list[Piece]]) -> str:
            out = ""
            for e, pieces in edges.items():
                covered, end = 0.0, None
                for a, b, m in sorted(pieces):
                    if end is not None and a > end and labelled(e, end, a, m):
                        covered += a - end
                    a = a if end is None else max(a, end)
                    covered += max(0.0, b - a)
                    end = b if end is None else max(end, b)
                if pieces and covered >= 0.9 * (box.w if e in "tb" else box.h):
                    out += e
            return out
        framing = [(c, got, sides(got.edges)) for c, got in by_color.items()]
        framing = [f for f in framing if f[2] and (partial or len(f[2]) == 4)]
        if not framing:
            return None
        color, got, drawn = max(framing, key=lambda f: (len(f[2]), len(f[1].ids)))
        # (each side drawn has pieces: `sides` counts no other)
        mid_of = {e: sorted(m for _, _, m in pieces)[len(pieces) // 2] for e, pieces in got.edges.items() if pieces}
        at = {"l": mid_of["l"] if "l" in drawn else box.x0, "t": mid_of["t"] if "t" in drawn else box.y0,
              "r": mid_of["r"] if "r" in drawn else box.x1, "b": mid_of["b"] if "b" in drawn else box.y1}
        ids = [i for i, e in zip(got.ids, got.edge_of) if e in drawn]
        rules: list[tuple[str, Rect]] = []  # each side's pieces joined where they touch (a label's gap stays open)
        for side in drawn:
            for r in sorted((r for r, e in zip(got.rects, got.edge_of) if e == side), key=lambda r: (r.x0, r.y0)):
                if rules and rules[-1][0] == side and rules[-1][1].expand(0.6).intersects(r):
                    rules[-1] = (side, rules[-1][1].union(r))
                else:
                    rules.append((side, r))
        return Frame(color=color, width=round(sorted(got.widths)[len(got.widths) // 2], 2), sides=drawn,
                     box=[round(at["l"], 2), round(at["t"], 2), round(at["r"], 2), round(at["b"], 2)],
                     rules=[[round(v, 2) for v in r.as_list()] for _, r in rules], ids=ids + same)

    def panel_frames(self) -> tuple[list[RawDrawing], dict[str, list[str]]]:
        """Filled boxes whose frame is drawn as rules around them (\\fcolorbox, listings'
        frame=single) and fills of one colour tiling one box (listings paints its background a
        line at a time, with strips for the frame's separation; a black terminal listing is a
        stack of bands): the page's drawings with each such stack as one fill, and the frames
        found in `self.frames` (fill drawing id -> outline) and `self.frame_ids`. Without this a
        listing was a stack of panels whose shared edges show as seams, its frame and its line
        numbers were thin pictures under them, and an \\fcolorbox lost the rules the panel
        removal took from the background. Also returns each stack's tiles: its first tile's
        id -> the ids of all of them."""
        self.frames = {}
        self.frame_ids = set()
        drawings = [d for d in self.raw["drawings"] if d["id"] not in self.decor_ids]

        def tile(d: RawDrawing) -> bool:
            return d["type"] == "f" and d["items"] == "re" and bool(d.get("fill")) and d.get("fill_opacity", 1.0) >= 0.99
        tiles = [(d, Rect.of(d["bbox"])) for d in drawings if tile(d)
                 and Rect.of(d["bbox"]).w * Rect.of(d["bbox"]).h < 0.95 * self.W * self.H]
        parent = list(range(len(tiles)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def join(beside: Callable[[Rect, Rect], bool]) -> None:
            boxes: dict[int, Rect] = {}
            for i, (_, r) in enumerate(tiles):
                boxes[find(i)] = boxes[find(i)].union(r) if find(i) in boxes else r
            keys = list(boxes)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    ra, rb = boxes[a], boxes[b]
                    if tiles[a][0]["fill"] == tiles[b][0]["fill"] and beside(ra, rb) and find(a) != find(b):
                        parent[find(a)] = find(b)
        # a line's pieces side by side first, then the lines one under the other
        join(lambda ra, rb: abs(ra.y0 - rb.y0) <= 0.3 and abs(ra.y1 - rb.y1) <= 0.3 and
             (-0.3 <= rb.x0 - ra.x1 <= 0.3 or -0.3 <= ra.x0 - rb.x1 <= 0.3))
        join(lambda ra, rb: abs(ra.x0 - rb.x0) <= 0.6 and abs(ra.x1 - rb.x1) <= 0.6 and
             (-0.3 <= rb.y0 - ra.y1 <= 0.3 or -0.3 <= ra.y0 - rb.y1 <= 0.3))
        groups: dict[int, list[int]] = {}
        for i in range(len(tiles)):
            groups.setdefault(find(i), []).append(i)
        replaced: dict[str, RawDrawing] = {}  # first tile's id -> the stack as one fill
        tiles_of: dict[str, list[str]] = {}
        gone: set[str] = set()
        spans = [(Rect.of(s["bbox"]), s) for s in self.raw["spans"] if s["text"].strip()]
        for members in groups.values():
            if len(members) < 2:
                continue
            box = union_all(tiles[i][1] for i in members)
            if sum(tiles[i][1].w * tiles[i][1].h for i in members) < 0.97 * box.w * box.h or box.w < 0.25 * self.W:
                continue  # not one box (an L, a staircase of bars)
            inside = [s for r, s in spans if box.contains(r.cx, r.cy)]
            code = bool(inside) and all(font_info(s["font"]).family == "mono" for s in inside)
            first = tiles[min(members)][0]
            frame = self.frame_of(box, first["fill"], drawings, partial=code)
            # (a stack is one panel when it is framed, or when what it holds is code: the rows of
            # a table with its fills stay cells)
            if frame is None and not code:
                continue
            # (its path the rectangle's two corners, as extract writes an 're': it was the four
            # numbers flat, which only an op-reading `box_outline` survived)
            stack: RawDrawing = {**first, "bbox": box.as_list(), "path": [("re", [[box.x0, box.y0], [box.x1, box.y1]])],
                                 "corners": {}}
            replaced[first["id"]] = stack
            tiles_of[first["id"]] = [tiles[i][0]["id"] for i in members]
            gone |= {tiles[i][0]["id"] for i in members}
            if frame:
                self.frames[first["id"]] = frame
                self.frame_ids |= set(frame.ids)
        out: list[RawDrawing] = []
        for d in drawings:
            if d["id"] in replaced:
                out.append(replaced[d["id"]])
            elif d["id"] not in gone:
                out.append(d)
        for d in out:
            r = Rect.of(d["bbox"])
            if d["id"] in self.frames or d["id"] in self.frame_ids or not (
                    d["type"] == "f" and set(d["items"]) <= set("relcq") and r.w >= 0.25 * self.W and r.h >= 3 and box_outline(d, r)):
                continue
            frame = self.frame_of(r, d["fill"], [o for o in out if o is not d and o["id"] not in self.frame_ids], partial=False)
            if frame:
                self.frames[d["id"]] = frame
                self.frame_ids |= set(frame.ids)
        self.bare_frames = self.rule_frames([d for d in out if d["id"] not in self.frame_ids], spans)
        return [d for d in out if d["id"] not in self.frame_ids], tiles_of

    def rule_frames(self, drawings: list[RawDrawing], spans: list[tuple[Rect, RawSpan]]) -> list[BareFrame]:
        """Rules closing a rectangle around code with nothing filled under them (listings'
        frame=single without a background: a rule piece per line on each side): the frames,
        their drawings taken into `self.frame_ids`. The sides become rule shapes and the frame
        a listing's panel; as figures, the side strips were pictures whose ends did not meet
        the top and bottom rules, which stayed in the background as page-wide hairlines."""
        found = [(d, self.thin_rule(d)) for d in drawings]
        rules = [(d, rule[2]) for d, rule in found if rule]
        if len(rules) < 4 or len(rules) > 400:
            return []  # (a chart's hundreds of ticks frame no code)
        parent = list(range(len(rules)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        boxes = [Rect.of(d["bbox"]).expand(0.6) for d, _ in rules]
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                if rules[i][1] == rules[j][1] and boxes[i].intersects(boxes[j]):
                    parent[find(i)] = find(j)
        groups: dict[int, list[RawDrawing]] = {}
        for i, (d, _) in enumerate(rules):
            groups.setdefault(find(i), []).append(d)

        def letters(ss: Iterable[RawSpan]) -> int:
            return sum(len(s["text"].strip()) for s in ss)
        out: list[BareFrame] = []
        for group in groups.values():
            box = union_all(Rect.of(d["bbox"]) for d in group)
            if len(group) < 4 or box.w < 0.25 * self.W or box.h < 6:
                continue
            inside = [s for r, s in spans if box.contains(r.cx, r.cy)]
            if not inside or letters(s for s in inside if font_info(s["font"]).family == "mono") < 0.9 * letters(inside):
                continue  # (an \fbox around prose is a table's cell, see table_from)
            frame = self.frame_of(box, None, group, partial=False)
            if frame is None:
                continue
            out.append(BareFrame(bbox=box, frame=frame, id=group[0]["id"]))
            self.frame_ids |= set(frame.ids)
        return out

    def analyse_graphics(self) -> None:
        graphics: list[Rect] = []
        rules: dict[tuple[int, int], list[Rule]] = {}
        self.analysed = True
        self.decorations = []
        self.graphic_drawings = {}
        bar_ids: list[tuple[str, Rect]] = []
        self.graphic_paths = {}
        table_rules = self.table_hairlines()
        drawings, tiles_of = self.panel_frames()
        for d in drawings:
            r = Rect.of(d["bbox"])
            if r.w * r.h >= 0.95 * self.W * self.H:
                continue  # page background
            fill_only = d["type"] == "f" and set(d["items"]) <= set("relcq")
            panel = fill_only and r.w >= 0.25 * self.W and r.h >= 3
            if self.is_decoration(r) and not (panel and box_outline(d, r)) and d["id"] not in table_rules:
                self.decorations.append(r)
                continue
            if panel and box_outline(d, r):
                self.panels.append(Panel(bbox=r, fill=d["fill"], id=d["id"], rounded="c" in d["items"],
                                         corners=d.get("corners", {}), opacity=d.get("fill_opacity", 1.0), image=False,
                                         frame=self.frames.get(d["id"]), tiles=tiles_of.get(d["id"], [])))
            elif d["type"] == "s" and r.h <= 1.0 and set(d["items"]) <= {"l"} and (r.w <= 3 * self.body or any(
                    abs(s["bbox"][2] - r.x0) <= 1 and s["bbox"][1] - 1 <= r.y0 <= s["bbox"][3]
                    and (extension_font(s["font"]) or "√" in s["text"])
                    for s in self.raw["spans"]) or self.typeset_fraction(r)):
                self.bars.append(r)  # fraction bars, radical overbars (long ones start at their radical sign)
                bar_ids.append((d["id"], r))
            else:
                graphics.append(r)
                self.graphic_drawings[d["id"]] = r
                self.graphic_paths.setdefault(tuple(r.as_list()), d)
                stroke_rule = d["type"] == "s" and r.h <= 1.0 and set(d["items"]) <= {"l"}
                fill_rule = fill_only and r.h <= 1.5 and r.w >= 20  # booktabs rules are thin filled boxes
                if stroke_rule or fill_rule:
                    rules.setdefault((round(r.x0), round(r.x1)), []).append(Rule(
                        rect=r, color=(d["fill"] if fill_rule else d["stroke"]) or "#000000",
                        weight=r.h if fill_rule else (d["width"] or 0.4)))
        for f in self.bare_frames:  # a listing's frame of rules: a panel with nothing filled
            self.panels.append(Panel(bbox=Rect.of(f.frame.box), fill=None, id=f.id, rounded=False,
                                     corners={}, opacity=1.0, image=False, frame=f.frame, tiles=[]))
        for p in [p for p in self.panels if self.legend_box(p.bbox, graphics)]:
            # A chart's legend box is part of the chart: as a panel, its swatches became bullets
            # of labels set on a native box, one per line, and the others were lost.
            self.panels.remove(p)
            graphics.append(p.bbox)
            self.graphic_drawings[p.id] = p.bbox
            self.graphic_paths.setdefault(tuple(p.bbox.as_list()), next(d for d in self.raw["drawings"] if d["id"] == p.id))
        # Strips thinner than a line of text, with no words on them, starting at one x, as thick
        # as each other and three or more (or two abutting), touching a graphic, are the bars of
        # a chart (xbar: one \addplot per series, a rectangle each): as panels they were cut off
        # the chart's picture and set on top of it. (Not a listing frame's top and bottom strip.)
        words = [Rect.of(s["bbox"]) for s in self.raw["spans"] if s["text"].strip()]
        thin = [p for p in self.panels if not p.image and p.bbox.h < 0.6 * self.body
                and not any(p.bbox.contains(w.cx, w.cy) for w in words)]
        series_bars: list[Panel] = []
        for p in thin:
            r = p.bbox
            mates = [q.bbox for q in thin if q is not p and abs(q.bbox.x0 - r.x0) <= 0.5 and abs(q.bbox.h - r.h) <= 0.3]
            series = len(mates) >= 2 or any(min(abs(q.y0 - r.y1), abs(r.y0 - q.y1)) <= r.h for q in mates)
            if series and any(r.expand(1.0).intersects(g) for g in graphics):
                series_bars.append(p)
        for p in series_bars:
            self.panels.remove(p)
            graphics.append(p.bbox)
            self.graphic_drawings[p.id] = p.bbox
            self.graphic_paths.setdefault(tuple(p.bbox.as_list()), next(d for d in self.raw["drawings"] if d["id"] == p.id))
        # A box of a footline or headline made of boxes side by side (author | title | date |
        # page): on the page's edge, as tall as the band and abutting a piece of it. Too narrow
        # to be theme artwork alone, it was a figure, and the date in it a label baked into the
        # background while the words of the boxes beside it were theme text.
        grown = True
        while grown:
            grown = False
            for i, r in [(i, r) for i, r in self.graphic_drawings.items() if any(r is g for g in graphics)]:
                edge = r.y0 <= 1 or r.y1 >= self.H - 1
                if edge and r.h <= 0.1 * self.H and any(
                        abs(d.y0 - r.y0) <= 1 and abs(d.y1 - r.y1) <= 1 and (abs(d.x0 - r.x1) <= 1 or abs(r.x0 - d.x1) <= 1)
                        for d in self.decorations):
                    graphics.remove(r)
                    del self.graphic_drawings[i]
                    self.decorations.append(r)
                    grown = True
        # Two or more horizontal rules of equal extent frame a table: the whole span is one
        # figure (or a native table, see table_from).
        self.table_rules = [g for g in rules.values() if len(g) >= 2]
        for group in self.table_rules:
            graphics.append(union_all(r.rect for r in group))
        # A short stroke touching other graphics is an arrow shaft or a tick, not a fraction bar.
        touching = [b for b in self.bars if any(b.expand(1.5).intersects(g) for g in graphics)]
        self.bars = [b for b in self.bars if b not in touching]
        graphics += touching
        self.graphic_drawings.update((i, r) for i, r in bar_ids if any(r is t for t in touching))
        for im in self.raw["images"]:
            r = Rect.of(im["bbox"])
            if min(r.w, r.h) < SMALL_IMAGE_PT:
                self.small_images.append((im, r))  # bullets, block shadows
            elif self.is_decoration(r):
                self.decorations.append(r)  # sidebar/header shading
            elif r.w >= 0.6 * self.W:
                self.panels.append(Panel(bbox=r, fill=None, id=im["id"], rounded=False,
                                         corners={}, opacity=1.0, image=True, frame=None, tiles=[]))
            else:
                graphics.append(r)
        # Glyphs of symbol fonts (Creative Commons badges, FontAwesome) are artwork.
        graphics += [Rect.of(s["bbox"]) for s in self.raw["spans"] if font_info(s["font"]).family == "icon"]
        self.graphics = graphics
        self._artwork = None  # see artwork_of
        self.regions = cluster_rects(graphics, gap=3.0) if graphics else []
        self.title_bridges = []  # see axis_titles
        self.column_bridges = []  # see axis_label_column

    def legend_box(self, box: Rect, graphics: list[Rect]) -> bool:
        """A box holding a row of key marks, each right before its label ("[■] EMEA  [■] Americas"):
        a legend. (A list's bullets stand one above the other, and the first items of two
        columns side by side are a column apart.)"""
        spans = [Rect.of(s["bbox"]) for s in self.raw["spans"] if s["text"].strip()]
        marks = [g for g in graphics if box.contains_rect(g, tol=0.5) and max(g.w, g.h) <= 15 and
                 any(0 <= s.x0 - g.x1 <= 12 and s.y0 < g.cy < s.y1 for s in spans)]
        return any(a is not b and abs(a.cy - b.cy) <= 1 and 0 < b.x0 - a.x1 <= 100 for a in marks for b in marks)

    def band_ornament(self, r: Rect) -> bool:
        """A small graphic in the header or footer band (navigation symbols, a title's accent
        bar): theme furniture that stays in the background and joins nothing into a figure."""
        return (r.y1 <= 0.15 * self.H or r.y0 >= 0.88 * self.H) and max(r.w, r.h) < 25

    def artwork_of(self, r: Rect) -> PathKey | None:
        """The smallest filled piece of theme artwork (a decoration: corner square, sidebar,
        band; or a small box in the header or footer band, a \\logo's) the rect's centre is on,
        as its box. (Not strokes: a zoomed plot's lines reaching off the page are decorations
        too, and cross the title.) A logo of words in the corner beside the last line of a
        references frame was a hole at that line's end, and the box wider than the entries."""
        if not self.analysed:
            return None  # (lines built before the graphics were looked at)
        artwork = self._artwork
        if artwork is None:
            decor = {tuple(d.as_list()) for d in self.decorations}

            def ornament(d: RawDrawing) -> bool:
                return d["id"] in self.graphic_drawings and self.band_ornament(self.graphic_drawings[d["id"]])
            # (a logo's box holds its words; navigation symbols drawn over a footnote do not)
            artwork = [(Rect.of(d["bbox"]), tuple(Rect.of(d["bbox"]).as_list()) not in decor)
                       for d in self.raw["drawings"] if "f" in d["type"] and d.get("fill")
                       and (tuple(Rect.of(d["bbox"]).as_list()) in decor or ornament(d))]
            self._artwork = artwork
        on = [d for d, whole in artwork if (d.contains_rect(r, tol=0.5) if whole else d.contains(r.cx, r.cy))]
        return tuple(min(on, key=lambda d: d.w * d.h).as_list()) if on else None

    def on_edge_artwork(self, r: Rect) -> bool:
        edge_panels = [p.bbox for p in self.panels
                       if p.bbox.x0 <= 1 or p.bbox.y0 <= 1 or p.bbox.x1 >= self.W - 1 or p.bbox.y1 >= self.H - 1]
        return any(d.contains(r.cx, r.cy) for d in self.decorations + edge_panels)

    def panel_of(self, r: Rect) -> int | None:
        """Innermost panel containing the rect's centre. A translucent fill is a highlight laid
        over text, not a container."""
        best: int | None = None
        for i, p in enumerate(self.panels):
            if p.bbox.contains(r.cx, r.cy) and p.opacity >= 0.99 and (best is None or p.bbox.w * p.bbox.h <
                                                                       self.panels[best].bbox.w * self.panels[best].bbox.h):
                best = i
        return best

    def shapes(self, lines: list[Line], elements: list[Element]) -> list[ShapeElement]:
        """Filled panels (beamer blocks and the like) that can become native shapes.

        Only panels that are pure content containers qualify: not touching the page edge
        (those are theme bars, better left in the background like a layout), opaque, and
        with nothing on top that stays in the background (it would be hidden under the
        shape). The render stage additionally checks the panel really shows its fill colour."""
        used = {sid for e in elements for sid in e["spans"]}
        leftovers = [s.rect for l in lines for s in l.spans if s.id not in used]
        figures = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram") and not e.get("overlay")]
        figures += [self.graphic_drawings[i] for e in elements for i in drawings_of(e)]
        figures += [Rect.of(b["bbox"]) for e in elements if e["kind"] == "text"
                    for p in e["paragraphs"] for b in [p["bullet"]] if b and b.get("patch")]
        loose = [g for g in self.graphics if not any(f.expand(0.5).contains_rect(g, tol=0.5) for f in figures)]
        # Strokes drawn after a panel that reach onto it (a matrix's quadrant dividers on the
        # panels' shared edges, an axis along their bottom): they stay in the background, and a
        # native panel over it hid them, halfway or whole.
        # (Hairlines that long count as theme decoration: any stroke not in a picture counts, but
        # not one that leaves the background with the text - an underline, a fraction bar.)
        order = {d["id"]: i for i, d in enumerate(self.raw["drawings"])}
        with_text = {tuple(s) for e in elements if e["kind"] == "text" for s in e.get("strokes", [])}
        strokes = [(i, Rect.of(d["bbox"]).expand((d["width"] or 0.4) / 2)) for i, d in enumerate(self.raw["drawings"])
                   if d["type"] == "s" and d["id"] not in self.decor_ids and d["id"] not in self.frame_ids
                   and tuple(Rect.of(d["bbox"]).as_list()) not in with_text
                   and not any(f.expand(0.5).contains_rect(Rect.of(d["bbox"]), tol=0.5) for f in figures)]
        bullet_images = {b["image"] for e in elements if e["kind"] == "text"
                         for p in e["paragraphs"] for b in [p["bullet"]] if b is not None and b["kind"] == "image"}
        out: list[ShapeElement] = []
        for p in sorted(self.panels, key=lambda p: -p.bbox.w * p.bbox.h):
            r = p.bbox
            frame = p.frame
            # A translucent fill over text (a highlight behind list items) is a translucent
            # shape under the text it covers, grouped with it.
            covered = Counter(self.line_owner[id(l)][0] for l in lines if id(l) in self.line_owner and l.rect.intersects(r))
            if not p.image and not p.fill and frame is not None and not any(
                    Rect.of(e["bbox"]).expand(0.5).contains_rect(r, tol=0.5) for e in elements if e["kind"] in ("image", "table", "diagram")):
                k = len(out)
                bare: list[ShapeElement] = [
                    {"id": f"p{self.raw['index']}s{k}r{i}", "kind": "shape", "role": "rule", "bbox": rule,
                     "fill": frame.color, "shape": "RECTANGLE", "flip": False, "radius": 0.0,
                     "drawing": p.id, "spans": []} for i, rule in enumerate(frame.rules)]
                out += bare
                continue
            if p.image or not p.fill or (p.opacity < 0.99 and not covered):
                continue
            if r.x0 <= 1 or r.y0 <= 1 or r.x1 >= self.W - 1 or r.y1 >= self.H - 1:
                continue
            if any(Rect.of(e["bbox"]).expand(0.5).contains_rect(r, tol=0.5) for e in elements
                   if e["kind"] in ("image", "table", "diagram") and not e.get("overlay")):
                continue  # already part of a picture
            inner = r.expand(-0.5)
            if any(inner.intersects(x) for x in leftovers):
                continue
            if any(overlap(inner, g) > 0.9 * max(g.w * g.h, 1e-6) for g in loose):
                continue  # graphics mostly on the panel (edge decorations like shadows are fine)
            if p.id in order and any(k > order[p.id] and overlap(r, band) > 0.01 for k, band in strokes):
                continue
            if any(inner.contains_rect(ir, tol=0) and im["id"] not in bullet_images for im, ir in self.small_images):
                continue
            corners = set(p.corners)
            kind: ShapeKind
            if not corners:
                kind, flip = "RECTANGLE", False
            elif corners == {"tl", "tr"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", False
            elif corners == {"bl", "br"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", True  # same shape rotated 180°
            else:
                kind, flip = "ROUND_RECTANGLE", False
            if any(o["bbox"] == r.as_list() and o["fill"] == p.fill for o in out):
                continue  # the same panel painted twice (a tcolorbox's title tab)
            panel: ShapeElement = {"id": f"p{self.raw['index']}s{len(out)}", "kind": "shape", "role": "panel",
                                   "bbox": r.as_list(), "fill": p.fill, "shape": kind, "flip": flip,
                                   "radius": max(p.corners.values(), default=0.0), "drawing": p.id, "spans": []}
            out.append(panel)
            if len(p.tiles) > 1:  # (a listing's per-line bands, painted as this one fill)
                panel["tiles"] = p.tiles
            if frame and len(frame.sides) == 4:
                panel["bbox"] = frame.box
                panel["outline"] = {"color": frame.color, "width": frame.width}
                panel["frame_drawings"] = frame.ids
            elif frame:  # listings' frame=lines, leftline...: the sides drawn are rules over the panel
                panel["frame_drawings"] = frame.ids
                sides: list[ShapeElement] = [
                    {"id": f"{panel['id']}r{k}", "kind": "shape", "role": "rule", "bbox": rule,
                     "fill": frame.color, "shape": "RECTANGLE", "flip": False, "radius": 0.0,
                     "drawing": p.id, "spans": []} for k, rule in enumerate(frame.rules)]
                out += sides
            if p.opacity < 0.99:
                last = out[-1]
                last["role"] = "highlight"
                last["opacity"] = round(p.opacity, 3)
                last["anchor"] = covered.most_common(1)[0][0]
        return self.framed_panels(out)

    @staticmethod
    def framed_panels(shapes: list[ShapeElement]) -> list[ShapeElement]:
        """A panel painted over a slightly larger one of another colour, inset by the same few
        tenths of a point on every side, is framed by it: tcolorbox (and themes like it) paints
        the frame colour's box and the interior over it. The inner panel becomes one shape with
        that outline, on the frame's centre line; the outer one, which shows only as that ring,
        is no shape of its own (as one it failed render's fill check and the frame was lost)."""
        out = list(shapes)
        for inner in shapes:
            if inner.get("outline") or inner.get("opacity") or inner not in out:
                continue
            ix0, iy0, ix1, iy1 = inner["bbox"]
            for outer in out:
                if outer is inner or outer.get("outline") or outer.get("opacity") or outer["fill"] == inner["fill"]:
                    continue
                ox0, oy0, ox1, oy1 = outer["bbox"]
                insets = (ix0 - ox0, iy0 - oy0, ox1 - ix1, oy1 - iy1)
                if not (0.1 <= min(insets) and max(insets) <= FRAME_RULE_PT and max(insets) - min(insets) <= 0.3):
                    continue
                w = sum(insets) / 4
                color = outer["fill"]
                if color is None:
                    continue  # (never: every panel here is filled)
                inner["bbox"] = [round(ox0 + w / 2, 2), round(oy0 + w / 2, 2), round(ox1 - w / 2, 2), round(oy1 - w / 2, 2)]
                inner["outline"] = {"color": color, "width": round(w, 2)}
                inner["radius"] = round(max(outer["radius"] - w / 2, inner["radius"]) if outer["radius"] else inner["radius"], 2)
                inner["shape"] = outer["shape"] if outer["shape"] != "RECTANGLE" else inner["shape"]
                inner["frame_drawings"] = [d for d in [outer.get("drawing")] if d is not None]
                out.remove(outer)
                break
        return out

    def blocks(self, shapes: list[ShapeElement]) -> None:
        """Beamer blocks: a title bar panel directly above a body panel of the same width get a
        common `block` number; the body gets the title bar's box (`title_bar`), and emit lays
        it under the whole block so that no gap can open between the two when the block is
        resized. Theme pictures that belong to the block are listed for the render stage to
        paint out of the background: the gradient strip between title bar and body (`strips`)
        and the soft shadow pieces right of and below the block (`shadow`, which emit turns
        into a native drop shadow)."""
        k = 0
        for head in sorted(shapes, key=lambda s: s["bbox"][1]):
            for body in shapes:
                if body is head or "block" in body or "block" in head or "opacity" in body or "opacity" in head \
                        or "rule" in (body.get("role"), head.get("role")):
                    continue
                hx0, hy0, hx1, hy1 = head["bbox"]
                bx0, by0, bx1, by1 = body["bbox"]
                # Title page boxes overlap the two parts by a few points.
                if abs(hx0 - bx0) <= 1.5 and abs(hx1 - bx1) <= 1.5 and by0 - hy1 <= 3.5 and by0 >= max(hy0 + 1, hy1 - 4):
                    head["block"] = k
                    body["block"] = k
                    body["title_bar"] = head["bbox"]
                    body["strips"] = [im["bbox"] for im in self.raw["images"]
                                      if im["bbox"][3] - im["bbox"][1] <= 6 and hy1 - 3 <= (im["bbox"][1] + im["bbox"][3]) / 2 <= by0 + 3
                                      and im["bbox"][2] - im["bbox"][0] >= 0.9 * (bx1 - bx0)
                                      and im["bbox"][0] >= bx0 - 2 and im["bbox"][2] <= bx1 + 2]
                    k += 1
                    break
        for el in shapes:
            if el.get("block") is not None and "title_bar" not in el:
                continue  # title bars: the body carries the block's shadow
            x0, y0, x1, y1 = el["bbox"]
            title_bar = el.get("title_bar")
            if title_bar:
                y0 = title_bar[1]
            outer = Rect(x0 - 1.5, y0 - 1.5, x1 + 8, y1 + 8)
            inner = Rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1)
            pieces = [Rect.of(im["bbox"]) for im in self.raw["images"]
                      if outer.contains_rect(Rect.of(im["bbox"]), tol=0) and not inner.contains_rect(Rect.of(im["bbox"]), tol=0)
                      and im["bbox"] not in el.get("strips", [])]
            right = [r.x1 - x1 for r in pieces if r.x1 > x1 + 2]
            below = [r.y1 - y1 for r in pieces if r.y1 > y1 + 2]
            under = Rect(x0 + 2, y1 + 0.5, x1 + 2, y1 + 3)  # a shadow never falls onto another panel
            if right and below and all(r.x0 >= x0 - 1.5 and r.y0 >= y0 - 1.5 for r in pieces) \
                    and not any(o is not el and Rect.of(o["bbox"]).intersects(under)
                                and not outer.contains_rect(Rect.of(o["bbox"]), tol=0.5)  # the shadow's own black geometry
                                for o in shapes):
                # Image boxes are rounded outwards to whole points: 4.07 ... 4.51 for a 4 pt shadow.
                el["shadow"] = {"size": float(max(1, math.floor(min(max(right), max(below)) + 0.25))),
                                "pieces": [r.as_list() for r in pieces]}
