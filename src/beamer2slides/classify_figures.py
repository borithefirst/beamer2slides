"""PageClassifier's figures: picture regions, formula pictures, overlays and icons, and the
drawings that become native diagrams.
"""

import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from . import curves
from .classify_model import (
    HOLE_PAD, Line, Paragraph, Rect, Span, cluster_rects, extension_font, miter_reach, new_line, new_paragraph,
    overlap, polygon_shape, union_all, upright_ellipse,
)
from .classify_paragraphs import ParagraphsMixin
from .classify_shapes import preset_shape, split_rectangle, turned_rounded, turned_shape
from .classify_state import DiagramRefusal, Refusal, StandingFigure
from .classify_text import EQ_NUMBER_RE, card_text, family_of, math_text, span_runs
from .classify_turned import SpanFrame, holds, moved, reframe, same_turn, span_frame, turn, unturned_spans
from .ir import (
    Arrow, BeforeWord, Dash, DiagramElement, DiagramLine, Element, ImageElement, Mark, Node, ShapeElement,
    TemplateKind, TextElement,
)
from .raw_types import DrawingType, PathItem, RawDrawing, RawImage, RawSpan

MAX_PLAIN_RECTANGLES = 32  # more rectangles in one cluster (a QR code, a pixel grid) are a picture

End = Literal["from", "to"]
ENDS: tuple[End, End] = ("from", "to")


@dataclass(frozen=True, kw_only=True)
class DraftNode:
    """A diagram node while `diagram_from` gathers it: its box, the words inside it, how it is
    drawn (`shape` None: a free label), and its corner radius when the drawing says one."""
    rect: Rect
    shape: TemplateKind | None
    spans: list[Span]
    fill: str | None
    stroke: str | None
    width: float | None
    radius: float | None
    dash: Dash | None
    adjust: float | None  # a preset's adjustment (classify_shapes.Preset)
    rotation: float | None  # a turned node's or a sloped label's turn, `rect` the box before it (ir.Node)


@dataclass(frozen=True, kw_only=True)
class Tip:
    """An arrow head `diagram_from` found: its box, the Slides head, its path's points, its
    outline's width (0 when only filled), whether its path closes (an open triangle, whose point
    the line must reach like a filled head's), and the line end it is on when the drawing order
    says so (a Circle tip), else None: the line end inside its box."""
    rect: Rect
    head: Arrow
    points: list[list[float]]
    outline: float
    closed: bool
    on: tuple[DiagramLine, End] | None


TIP_SIZE = 6.0  # pt: an arrow head is a separate path no larger than this
CENTRED_HEADS: frozenset[Arrow] = frozenset(
    {"FILL_CIRCLE", "OPEN_CIRCLE", "FILL_SQUARE", "OPEN_SQUARE", "FILL_DIAMOND", "OPEN_DIAMOND"})
"""Heads Slides centres on the line's end, where TikZ stops the line at the head's back."""
DOT_MAX = 2.0  # line widths: a dash no longer than this is a dot
LONG_DASH_FROM = math.sqrt(4.0 * 8.0)  # line widths: halfway, by ratio, from Slides' dash to its long dash


def dash_style(dash: Sequence[float], width: float) -> Dash | None:
    """The Slides dash style nearest a PDF dash array (page pt, raw `dash`) on a line `width` pt
    wide; None for a solid line. Slides' styles are ECMA-376's preset dashes, lengths in line
    widths (dot 1 on 3 off, dash 4-3, lgDash 8-3, dashDot 4-3-1-3, lgDashDot 8-3-1-3), all with
    the same gaps: what tells them apart is the dashes. So the PDF's dashes are measured in
    widths: none longer than `DOT_MAX` is a DOT, dashes and dots mixed a DASH_DOT, dashes alone a
    DASH, either LONG when its longest dash is nearer 8 widths than 4. TikZ's dashed (3 on, 3 off)
    is a DASH on a thick line and a LONG_DASH on a thin one, dotted (a width on, 2 off) a DOT, dash
    dot (and dash dot dot) a DASH_DOT or a LONG_DASH_DOT. Slides cannot say a phase or a gap."""
    if not any(v > 0 for v in dash):
        return None
    w = max(width, 0.25)  # (a 0-width hairline is drawn a device pixel wide)
    ons = (list(dash) * (2 if len(dash) % 2 else 1))[0::2]  # ([3] is 3 on, 3 off)
    longest, shortest = max(ons) / w, min(ons) / w
    if longest <= DOT_MAX:
        return "DOT"
    if shortest <= DOT_MAX:
        return "LONG_DASH_DOT" if longest > LONG_DASH_FROM else "DASH_DOT"
    return "LONG_DASH" if longest > LONG_DASH_FROM else "DASH"


def tip_corners(path: Sequence[PathItem]) -> list[tuple[float, float]]:
    """The corners of a path of straight segments: its points with repeats and points on a
    straight run left out (TikZ's Triangle tip passes through the middle of its back)."""
    points: list[tuple[float, float]] = []
    for _, pts in path:
        for x, y in pts:
            if not points or math.dist(points[-1], (x, y)) > 0.05:
                points.append((x, y))
    if len(points) > 1 and math.dist(points[0], points[-1]) <= 0.05:
        points.pop()
    corners = list(points)
    for p in points:
        k = corners.index(p)
        a, b = corners[k - 1], corners[(k + 1) % len(corners)]
        ux, uy, vx, vy = p[0] - a[0], p[1] - a[1], b[0] - p[0], b[1] - p[1]
        if len(corners) > 3 and abs(ux * vy - uy * vx) <= 0.02 * math.hypot(ux, uy) * math.hypot(vx, vy):
            corners.remove(p)
    return corners


def closed_path(path: Sequence[PathItem]) -> bool:
    """A path ending where it starts."""
    return bool(path) and math.dist(path[0][1][0], path[-1][1][-1]) <= 0.05


def tip_head(kind: DrawingType, fill: str | None, stroke: str | None, path: Sequence[PathItem], round_: bool) -> Arrow:
    """The Slides head nearest a small arrow-tip path. Filled (a fill in another colour than its
    outline, white in a black outline, reads hollow): a circle (`round_`) FILL_CIRCLE, a convex
    four-corner path a FILL_SQUARE when its corners are square, else FILL_DIAMOND, a concave one
    (stealth) STEALTH_ARROW, anything else (latex, triangle) FILL_ARROW. Hollow the same with
    OPEN_; a hollow triangle or stealth has no Slides head of its own and is OPEN_ARROW, the
    head Slides draws as an outline (FILL_ARROW would paint its inside in)."""
    filled = kind == "f" or (kind == "fs" and fill == stroke)
    if round_:
        return "FILL_CIRCLE" if filled else "OPEN_CIRCLE"
    polygon = {op for op, _ in path} == {"l"} and (filled or closed_path(path))
    corners: list[tuple[float, float]] = tip_corners(path) if polygon else []
    if len(corners) == 4:
        edges = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(corners, corners[1:] + corners[:1])]
        turns = [u[0] * v[1] - u[1] * v[0] for u, v in zip(edges, edges[1:] + edges[:1])]
        if all(t > 0 for t in turns) or all(t < 0 for t in turns):
            square = all(abs(u[0] * v[0] + u[1] * v[1]) <= 0.1 * math.hypot(*u) * math.hypot(*v)
                         for u, v in zip(edges, edges[1:] + edges[:1]))
            if square:
                return "FILL_SQUARE" if filled else "OPEN_SQUARE"
            return "FILL_DIAMOND" if filled else "OPEN_DIAMOND"
        return "STEALTH_ARROW" if filled else "OPEN_ARROW"
    return "FILL_ARROW" if filled else "OPEN_ARROW"


Segment = tuple[tuple[float, ...], tuple[float, ...]]


def straight_runs(segments: Sequence[Segment]) -> list[Segment]:
    """A polyline's segments without those of no length, and a run going on in one direction
    joined into one segment: a TikZ |-| edge to a child straight below its parent is three
    segments, the middle one a point, and Slides refuses a line of no size (the whole diagram
    became a picture)."""
    out: list[Segment] = []
    for a, b in segments:
        if math.dist(a, b) < 0.05:
            continue
        if out:
            p, q = out[-1]
            u, v = (q[0] - p[0], q[1] - p[1]), (b[0] - a[0], b[1] - a[1])
            if math.dist(q, a) < 0.05 and u[0] * v[0] + u[1] * v[1] > 0 and \
                    abs(u[0] * v[1] - u[1] * v[0]) < 0.01 * math.hypot(*u) * math.hypot(*v):
                out[-1] = (p, b)
                continue
        out.append((a, b))
    return out


def on_rim(rect: Rect, end: Sequence[float], other: Sequence[float]) -> bool:
    """A line end on a small circle's rim with the circle behind it, away from the line: where
    TikZ stops a line at a Circle tip's back (a dot a line runs into has the end at its centre)."""
    cx, cy, radius = rect.cx, rect.cy, (rect.w + rect.h) / 4
    behind = (cx - end[0]) * (other[0] - end[0]) + (cy - end[1]) * (other[1] - end[1]) < 0
    return behind and abs(math.dist((cx, cy), end) - radius) <= 0.6


def path_points(d: RawDrawing) -> list[Sequence[float]]:
    """A drawing's line ends, corners and curve ends: where it may meet words."""
    out: list[Sequence[float]] = []
    for op, pts in d.get("path") or []:
        if op == "re":
            (x0, y0), (x1, y1) = pts
            out += [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        else:
            out += [pts[0], pts[-1]] if op == "c" else pts
    return out


def near_words(s: Span, x: float, y: float) -> bool:
    """A drawing's point at a span: over it, or a third of an em off it."""
    return s.rect.x0 - 0.1 * s.size <= x <= s.rect.x1 + 0.1 * s.size and \
        s.rect.y0 - 0.35 * s.size <= y <= s.rect.y1 + 0.35 * s.size


def rim_only(d: RawDrawing) -> bool:
    """A closed curve: it has no ends, it meets the words it circles, not those its rim passes
    (a Venn circle under the title of its set)."""
    path = d.get("path") or []
    return len(path) >= 2 and all(op == "c" for op, _ in path) and math.dist(path[0][1][0], path[-1][1][-1]) <= 0.5


STANDING_STROKES = 3  # drawings ending on a caption's words from one side: a figure standing on it


def caption_above(met: Sequence[Span], labels: Sequence[Span]) -> list[Span]:
    """The caption's first lines when the figure's box holds them as its labels: label lines
    starting where a met line starts, stacked straight up from it (a tree's leaves end on the
    first line of a three-line caption, only its last line left as words)."""
    out: list[Span] = []
    for s in met:
        top = s
        while True:
            up = [o for o in labels if o not in out and abs(o.rect.x0 - s.rect.x0) <= 0.1 * s.size
                  and o.rect.y0 < top.rect.y0 and o.rect.y1 >= top.rect.y0 - 0.5 * s.size]
            if not up:
                break
            top = max(up, key=lambda o: o.rect.y0)
            out.append(top)
    return out


def stands_on(c: Rect, met: Sequence[tuple[Span, Line]], ends: Sequence[tuple[Sequence[float], RawDrawing]],
              near: Callable[[Span, float, float], bool], caption: Sequence[Span]) -> bool:
    """A figure standing on its caption, not a graphic drawn for its words: the words it meets
    lie beyond its box (their middle below its foot or above its top), and several of its
    strokes end on them - a tree's leaves on the line under it. An arrow or a brace meets its
    words with one or two strokes. As an overlay, the figure was stretched after the caption
    Slides set while what it did not hold stayed in the background (a tree's nodes slid off
    their outlines). `caption` are its first lines the box holds as labels (`caption_above`):
    strokes ending on them, or half an em above them, count too."""
    if not all(s.rect.cy >= c.y1 or s.rect.cy <= c.y0 for s, _ in met):
        return False

    def on_caption(s: Span, x: float, y: float) -> bool:
        return s.rect.x0 - 0.1 * s.size <= x <= s.rect.x1 + 0.1 * s.size and \
            s.rect.y0 - 0.5 * s.size <= y <= s.rect.y1
    meeting = {d["id"] for (x, y), d in ends if any(near(s, x, y) for s, _ in met)
               or any(on_caption(s, x, y) for s in caption)}
    return len(meeting) >= STANDING_STROKES


class FiguresMixin(ParagraphsMixin):
    """Methods of classify.PageClassifier; the state they share is `classify_state.PageState`."""

    def math_pictures(self, lines: list[Line], paragraphs: list[Paragraph], elements: list[Element]) -> list[Element]:
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
        blocked = [Rect.of(e["bbox"]) for e in elements if e["kind"] != "text" and not e.get("overlay")]
        for e in elements:
            if e["kind"] == "text":
                for p in e["paragraphs"]:
                    blocked += [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                                for l in p["lines"]]
                    bullet = p["bullet"]
                    if bullet:  # glyph boxes include ascender space; use their visible core
                        b = Rect.of(bullet["bbox"])
                        blocked.append(Rect(b.x0, b.cy - 0.25 * b.h, b.x1, b.cy + 0.25 * b.h))
        out: list[Element] = []
        taken: set[str] = set()
        clusters = cluster_rects([s.rect for s in spans] + list(self.bars), gap=0.6 * self.body)
        # (clusters whose boxes overlap are one formula: a display \sum between the words of its
        # row is a cluster of its own inside theirs, and became a picture of its glyph's font
        # box, a chevron, under the picture of the rest)
        while len(merged := cluster_rects(clusters, gap=3.0)) < len(clusters):
            clusters = merged
        # (columns of one display on the same rows - a cases' conditions, an aligned
        # right-hand side - a little further apart than glyphs; not an equation number)
        def eqno(c: Rect) -> bool:
            return all(EQ_NUMBER_RE.match(s.text.strip()) for s in spans if c.expand(1.5).contains_rect(s.rect, tol=0.5))
        merged = True
        while merged:
            merged = False
            for a in clusters:
                for b in clusters:
                    if a is not b and not eqno(a) and not eqno(b) and \
                            min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * min(a.h, b.h) and \
                            max(a.x0, b.x0) - min(a.x1, b.x1) <= 1.5 * self.body:
                        clusters = [c for c in clusters if c is not a and c is not b] + [union_all([a, b])]
                        merged = True
                        break
                if merged:
                    break
        for c in clusters:
            box = c.expand(1.5)
            members = [s for s in spans if box.contains_rect(s.rect, tol=0.5) and s.id not in taken]
            taken |= {s.id for s in members}
            # (the glyphs themselves, not the picture's anti-aliasing margin: a display's big
            # operator box starts a hair under the descenders of the paragraph above)
            if not members or any(b.intersects(c.expand(0.3)) for b in blocked):
                continue  # only a stray bar, or tangled with native content: leave it in the background
            if len(members) == 1 and EQ_NUMBER_RE.match(members[0].text.strip()) and members[0].info.family != "math":
                # An equation number beside its equation is plain text.
                par = new_paragraph([new_line(members)], align="right", reason=None)
                out.append(self.text_element([par], f"p{self.raw['index']}eq{len(out)}"))
                continue
            picture: ImageElement = {"id": f"p{self.raw['index']}m{len(out)}", "kind": "image", "role": "math",
                                     "bbox": box.as_list(), "spans": [s.id for s in members]}
            out.append(picture)
        return out

    def figures(self, lines: list[Line], text_elements: list[TextElement]) -> list[Element]:
        """Figure regions (graphics, images and their labels) that can become separate pictures.

        Skipped, so they stay in the background: specks (shadow corners, QED boxes),
        near-full-page artwork, and anything overlapping an editable text box."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        if not self.regions:
            return []
        def ink_rect(e: TextElement) -> Rect:
            # Big text without descenders (a statistic: "92%") ends at its baseline, not a
            # quarter em below it where a card under it may start.
            r = Rect.of(e["bbox"])
            last = e["paragraphs"][-1] if e["paragraphs"] else None
            if last and last["lines"]:
                text = "".join(run["text"] for run in last["runs"])
                depth = 0.25 if any(c in "gjpqyQ,;()[]{}|/@$_" for c in text) else 0.05
                r = Rect(r.x0, r.y0, r.x1, min(r.y1, last["lines"][-1]["baseline"] + depth * last["size"]))
            return r

        text_rects = [ink_rect(e) for e in text_elements]

        def grazed(e: TextElement, t: Rect, c: Rect) -> bool:
            """A figure reaching less than an em into a text box's outline, clear of its lines
            and bullets (a pie's pin label in the margin of the list beside it, between two
            bullets): no text under it. (Deeper in, a figure is over the text.)"""
            if min(t.x1, c.x1) - max(t.x0, c.x0) >= self.body and min(t.y1, c.y1) - max(t.y0, c.y0) >= self.body:
                return False
            ink = [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                   for p in e["paragraphs"] for l in p["lines"]]
            ink += [Rect.of(b["bbox"]) for p in e["paragraphs"] for b in [p["bullet"]] if b and b["bbox"]]
            # (the figure's own pieces there, not its outline: the label reaches under the
            # bullets' column between two of them)
            pieces = [r for r in rects if c.expand(0.1).contains_rect(r, tol=0.5) and r.intersects(t)]
            return bool(ink) and not any(r.intersects(q) for r in ink for q in pieces)

        self.bullet_boxes = [Rect.of(b["bbox"]).expand(1) for e in text_elements for p in e["paragraphs"]
                             for b in [p["bullet"]] if b and b["bbox"]]
        out: list[Element] = []
        # Tables first, from their rules: clustering could merge a table with a picture beside it.
        for group in self.table_rules:
            frame = union_all(r.rect for r in group).expand(1)
            table = self.table_from(frame, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                taken = set(table["spans"])
                label_spans = [s for s in label_spans if s.id not in taken]
        table_frames = [Rect.of(t["bbox"]) for t in out]
        regions = [r for r in self.regions if not any(f.expand(0.5).contains_rect(r, tol=0.5) for f in table_frames)]
        if not regions:
            return out
        # Cluster word by word: one "line" of labels can span two neighbouring figures.
        # Navigation symbols and ornaments in the header/footer band bridge nothing (they would
        # join a \logo above them into one picture).
        regions = [r for r in regions if not self.band_ornament(r)]
        if not regions:
            return out
        rects = regions + [s.rect for s in label_spans] + self.title_bridges + self.column_bridges
        for c in cluster_rects(rects, gap=0.8 * self.body):
            if max(c.w, c.h) < 25:
                continue
            if c.w * c.h > 0.8 * self.W * self.H:
                self.diagram_refusals.append(DiagramRefusal(box=c, reason="too_large", detail=f"{c.w:.0f}x{c.h:.0f} pt"))
                continue
            if (c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H:
                continue  # navigation dots and ornaments in the header/footer band
            if not any(r.intersects(c.expand(0.1)) for r in regions):
                continue  # only stray rotated text, no graphics
            if any(h.expand(0.5).contains_rect(c, tol=0.5) for h in self.hole_boxes):
                continue  # the graphic of a hole (a frame around words): in that picture already
            over_text = any(t.intersects(c) and not grazed(e, t, c) for e, t in zip(text_elements, text_rects))
            table = None if over_text or not self.fill_grid(c) else self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)  # shaded cells edge to edge: a table, not a diagram of boxes
                continue
            drafted = DiagramRefusal(box=c, reason="over_text", detail="") if over_text \
                else self.diagram_from(c, label_spans, len(out))
            if not isinstance(drafted, DiagramRefusal) and self.splits_cells(drafted, label_spans):
                # a ruled grid table_from could not read: a picture, not merged rows
                drafted = DiagramRefusal(box=c, reason="splits_cells", detail="")
            diagram = None if isinstance(drafted, DiagramRefusal) else drafted
            if isinstance(drafted, DiagramRefusal):
                self.diagram_refusals.append(drafted)
            if diagram and any(n["text"] for n in diagram["nodes"]):
                out.append(diagram)  # frames around their own text (a framed paragraph), not marks on prose
                continue
            overlay = self.overlay(c, label_spans, lines, len(out), over_text)
            captions = {sid for f in self.standing if c.expand(0.5).contains_rect(f.drawing, tol=0.5) for sid in f.caption}
            if overlay == "standing" and over_text and all(
                    t.cy >= c.y1 or t.cy <= c.y0 or not captions.isdisjoint(e["spans"])
                    for e, t in zip(text_elements, text_rects) if t.intersects(c) and not grazed(e, t, c)):
                # (its strokes reach into the caption's box - its plot down beside a caption it
                # gave back its first lines - no text under it all the same)
                over_text = False
                drafted = self.diagram_from(c, label_spans, len(out))
                diagram = None if isinstance(drafted, DiagramRefusal) else drafted
                self.diagram_refusals = [r for r in self.diagram_refusals if r.box is not c] + \
                    ([drafted] if isinstance(drafted, DiagramRefusal) else [])
            if isinstance(overlay, dict):
                out.append(overlay)
                continue
            if over_text:
                continue
            table = self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                continue
            if diagram:
                out.append(diagram)
                continue
            bars = self.plain_rectangles(c, label_spans, len(out))
            if bars:
                out += bars
                continue
            spans = [s.id for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
            under_words = any(t.x0 < c.x1 and c.x0 < t.x1 and t.y0 < c.cy <= t.y1 + 0.3 * self.body for t in text_rects)
            if not spans and c.h <= 1.5 and under_words and \
                    not any(c.expand(0.5).intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
                # Only a hairline (the pieces of an underline under words left in the background):
                # a picture adds nothing, and its box took the words above it off the background
                # while the crop showed only the rule. (A rule with no words on it - a footnote's,
                # above its text - stays a picture: in the background it is ink by native words.)
                continue
            el: ImageElement = {"id": f"p{self.raw['index']}f{len(out)}", "kind": "image", "role": "figure",
                                "bbox": self.clip_to_bands(c, c.expand(1.0)).as_list(), "spans": spans}
            bare = None if spans else self.bare_image(c)
            if bare is not None:
                # One `\includegraphics` and nothing else: the picture is the image itself, on
                # its own box (the margin around a drawing is for strokes reaching out of it,
                # which an image has none of). render.image_file may then write the author's
                # file instead of a render of the page.
                el["bbox"], el["image"] = list(bare["bbox"]), bare["id"]
            out.append(el)
        return out

    def bare_image(self, c: Rect) -> RawImage | None:
        """The raster image a figure region consists of, if it consists of nothing else: one
        graphic, one image covering it, no text drawn in the region."""
        members = [g for g in self.graphics if c.expand(0.5).contains_rect(g, tol=0.5)]
        images = [im for im in self.raw["images"] if Rect.of(im["bbox"]).intersects(c.expand(0.5))]
        if len(members) != 1 or len(images) != 1:
            return None
        r = Rect.of(images[0]["bbox"])
        if not (r.contains_rect(members[0], tol=0.5) and members[0].contains_rect(r, tol=0.5)):
            return None
        if any(Rect.of(s["bbox"]).intersects(c.expand(0.5)) for s in self.raw["spans"]):
            return None
        return images[0]

    def overlay(self, c: Rect, label_spans: list[Span], lines: list[Line], index: int, over_text: bool
                ) -> ImageElement | Literal["standing"] | None:
        """A figure cluster drawn over native text or right at its words (a tikzmark arrow, a
        brace under a phrase, an emphasis ellipse, a callout): a picture of only its own
        drawings and labels on a transparent ground (`drawings`), grouped with the text it
        meets (`anchor`). `marks` are the word edges it meets, with what precedes them on their
        line, so that emit moves and stretches it with the words Slides sets at other widths."""
        box = c.expand(1)
        # (not the frames of holes, nor bullets drawn on the page: those are pictures of their own)
        taken = [h.expand(0.5) for h in self.hole_boxes] + self.bullet_boxes
        drawings = [d for d in self.raw["drawings"] if d["id"] in self.graphic_drawings
                    and box.contains_rect(self.graphic_drawings[d["id"]], tol=0.5)
                    and not any(t.contains_rect(self.graphic_drawings[d["id"]], tol=0.5) for t in taken)]
        if not drawings or any(box.intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
            return None
        def on_node(s: Span, l: Line) -> bool:
            # Words well inside a filled shape of the cluster are that node's label (a "?" on
            # a disc), not words the drawing is made for: stretched after them as Slides sets
            # them, the disc came out an ellipse. (A highlight box is tight around its words.)
            for d in drawings:
                g = self.graphic_drawings[d["id"]]
                if "f" in d["type"] and d.get("fill") and g.contains_rect(s.rect, tol=0):
                    label = union_all([o.rect for o in l.content if o.text.strip() and g.contains_rect(o.rect, tol=0)])
                    if g.w >= 2 * label.w and g.h >= 1.5 * label.h:
                        return True
            return False

        words = [(s, l) for l in lines if id(l) in self.line_owner for s in l.content
                 if s.text.strip() and s.info.family != "icon" and not any(s in h for h in l.holes) and not on_node(s, l)]
        if not over_text and any(box.contains_rect(s.rect, tol=0.5) and s not in label_spans for l in lines
                                 if id(l) not in self.line_owner for s in l.spans):
            return None  # math set inside a figure: one picture of everything in its box
        if over_text and any(box.contains_rect(s.rect, tol=0.5) and s not in label_spans
                             for l in lines if l.reason == "math" for s in l.spans):
            # A display formula's own drawing (an \underbrace under a \left( ... \right) row,
            # its \text label native below it): the formula stays where it is, and so does its
            # brace. As an overlay it took the paren pieces its labels' bands reached off the
            # background and was stretched to the label's words (real_linear-attention-a s42).
            return None

        ends = [(p, d) for d in drawings for p in path_points(d)]
        met = self.met_words(ends, words)
        held = [s for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
        if met and stands_on(c, met, ends, near_words, caption_above([s for s, _ in met], held)):
            return "standing"  # a figure on its caption: no overlay, whatever its strokes reach into
        if any(c.expand(0.5).contains_rect(f.drawing, tol=0.5) for f in self.standing):
            return "standing"  # (its caption's first lines given back: release_held_captions)
        if not met and over_text:
            met = [(s, l) for s, l in words if any(s.rect.intersects(self.graphic_drawings[d["id"]]) for d in drawings)]
        if not over_text:
            # Beside text, it points at words of prose; a line of widely spaced words next to a
            # figure is its axis labels.
            def spaced(l: Line) -> bool:
                return any(b.rect.x0 - a.rect.x1 >= l.size for a, b in zip(l.content, l.content[1:]))

            met = [(s, l) for s, l in met if not spaced(l)]
            if not met:
                return None
        labels = [s for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
        el: ImageElement = {"id": f"p{self.raw['index']}f{index}", "kind": "image", "role": "figure", "overlay": True,
              "bbox": union_all([self.graphic_drawings[d["id"]] for d in drawings] + [s.rect for s in labels]).expand(1.0).as_list(),
              "spans": [s.id for s in labels], "drawings": [d["id"] for d in drawings]}
        if met:
            anchor = Counter(self.line_owner[id(l)][0] for _, l in met).most_common(1)[0][0]
            el["anchor"] = anchor
            el["marks"] = [self.mark(l, x) for s, l in dict.fromkeys(met) if self.line_owner[id(l)] == (anchor, "left")
                           for x in (s.rect.x0, s.rect.x1)]
        return el

    def trim_overlays(self, elements: list[Element]) -> None:
        """Drawings of an overlay that lie within a formula picture (a highlight behind a line
        that became one picture) belong to that picture: the overlay gives them up."""
        boxes = [Rect.of(e["bbox"]) for e in elements if e["kind"] == "image" and e["role"] == "math"]
        by_id = self.spans_by_id
        for el in elements:
            if el["kind"] != "image" or not el.get("overlay"):
                continue
            drawings = [i for i in el.get("drawings", []) if not any(b.contains_rect(self.graphic_drawings[i], tol=0.5) for b in boxes)]
            el["drawings"] = drawings
            if drawings:
                el["bbox"] = union_all([self.graphic_drawings[i] for i in drawings] +
                                       [by_id[i].rect for i in el["spans"]]).expand(1.0).as_list()

    @staticmethod
    def mark(line: Line, x: float) -> Mark:
        """Where a graphic meets a line of text at `x`: the line's style and the words before x,
        as a formula hole run carries them (emit.formula_shifts predicts where x lands in Slides)."""
        main = line.main
        words = [s for s in line.content if s.rect.x1 <= x + 0.5 and s.info.family != "icon"
                 and not any(s in h for h in line.holes)]
        # Holes and em-space gaps before x keep their width in Slides: close them up, or
        # formula_shifts would read each as one huge word space.
        cuts: list[tuple[float, float]] = []
        holes = [line.hole_rect(h) for h in line.holes]
        for r in holes:
            if r.x1 <= x + 0.5:
                cuts.append((r.x0, min([s.rect.x0 for s in words if s.rect.x0 >= r.x1 - 0.5] + [x]) - r.x0))
        stops = sorted([(s.rect.x0, s.rect.x1) for s in words] + [(x, x)])
        for (_, a), (b, _) in zip(stops, stops[1:]):
            if b - a >= line.size and not any(a - 0.5 <= r.x0 < b for r in holes):
                cuts.append((a, max(1, round((b - a - 0.33 * line.size) / line.size)) * line.size))  # (as runs() writes it)
        def closed(v: float) -> float:
            return v - sum(w for c, w in cuts if c < v - 0.5)
        before: list[BeforeWord] = [
            (round(s.rect.w, 2), s.font, family_of(s), s.info.bold, s.info.italic,
             math_text(s.font, s.text)[0] if s.info.family == "math" else s.text, round(closed(s.rect.x0), 2))
            for s in words]
        # (a hole's gap in Slides is HOLE_PAD wider on each side)
        pads = 2 * HOLE_PAD * sum(r.x1 <= x + 0.5 for r in holes)
        return {"x": round(x, 2), "hole_x0": round(closed(x), 2), "pads": pads, "font": main.font, "family": family_of(main), "size": round(line.size, 2),
                "bold": main.info.bold, "italic": main.info.italic, "before": before}

    def icons(self, text_elements: list[TextElement]) -> list[ImageElement]:
        """Small raster images next to text that are not bullets (bibliography icons, inline
        logos): movable pictures. Overlapping parts of one icon are cropped together."""
        bullets = {b.get("image") for e in text_elements for p in e["paragraphs"] for b in [p["bullet"]] if b}
        starts = [Rect(l["x0"], l["baseline"] - p["size"], l["x1"], l["baseline"])
                  for e in text_elements for p in e["paragraphs"] for l in p["lines"]]

        def beside_text(r: Rect) -> bool:
            # right of the icon, or starting on it (a label drawn on a ball)
            return any(-r.w <= s.x0 - r.x1 <= 15 and s.y0 < r.y1 and r.y0 < s.y1 for s in starts)

        rects = [ir for im, ir in self.small_images
                 if im["id"] not in bullets and max(ir.w, ir.h) <= 20 and min(ir.w, ir.h) >= 3
                 and not any(b.contains_rect(ir, tol=0.5) for b in self.icon_bullets)
                 and 0.15 * self.H < ir.cy < 0.88 * self.H and not self.on_edge_artwork(ir)
                 and not any(p.bbox.expand(1).intersects(ir) for p in self.panels)]  # block shadow pieces
        rects = [c for c in cluster_rects(rects, gap=0.0) if beside_text(c)]
        out: list[ImageElement] = []
        for c in rects:
            out.append({"id": f"p{self.raw['index']}ic{len(out)}", "kind": "image", "role": "icon",
                        "bbox": c.expand(0.5).as_list(), "spans": []})
            # A picture as an item label (\item[\includegraphics...]) moves with its item.
            item = next((e["id"] for e in text_elements for p in e["paragraphs"] for l in p["lines"][:1]
                         if 0 <= l["x0"] - c.x1 <= 1.5 * p["size"] and l["baseline"] - p["size"] < c.cy < l["baseline"]), None)
            if item:
                out[-1]["anchor"] = item
        return out

    def specks_on_panels(self, spans: list[Span], elements: list[Element]) -> list[ImageElement]:
        """Small graphics on a block panel that no other element took (a proof's QED box, a
        TikZ mark): the panel becomes a native shape over the background, so they become
        pictures above it (grouped with the block) instead of staying hidden in the background."""
        taken = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram")]
        taken += [Rect.of(b["bbox"]) for e in elements if e["kind"] == "text"
                  for p in e["paragraphs"] for b in [p["bullet"]] if b and b["bbox"]]
        panels = [p.bbox for p in self.panels if p.fill and not p.image
                  and p.bbox.x0 > 1 and p.bbox.y0 > 1 and p.bbox.x1 < self.W - 1 and p.bbox.y1 < self.H - 1]
        out: list[ImageElement] = []
        for c in (cluster_rects(self.graphics, gap=0.5) if self.graphics and panels else []):
            if max(c.w, c.h) >= 25 or any(t.expand(0.5).intersects(c) for t in taken) or \
                    not any(p.expand(-0.5).contains_rect(c, tol=0.5) for p in panels) or \
                    any(s.rect.intersects(c) for s in spans if s.text.strip()):
                continue
            out.append({"id": f"p{self.raw['index']}k{len(out)}", "kind": "image", "role": "icon",
                        "bbox": c.expand(1.0).as_list(), "spans": []})
        return out

    def met_words(self, ends: Sequence[tuple[Sequence[float], RawDrawing]],
                  words: Sequence[tuple[Span, Line]]) -> list[tuple[Span, Line]]:
        """The words a figure's line ends, corners and tips touch (`near_words`); a closed curve
        only those it circles (`rim_only`)."""
        return [(s, l) for s, l in words if any(
            near_words(s, x, y) and (not rim_only(d) or self.graphic_drawings[d["id"]].contains(s.rect.cx, s.rect.cy))
            for (x, y), d in ends)]

    def release_held_captions(self, lines: list[Line]) -> None:
        """A figure standing on a caption (`stands_on`) whose first lines it holds as labels
        (`caption_above`: a tree's plot reaching down beside them, real_third-year-talk p10
        panel 6) gives those lines back to the caption, before paragraphs are built: the
        caption was cut in two, its first lines in the picture in the PDF's face and the rest
        native in Slides' face and measure. Its picture is then of the drawing and its true
        labels, under the whole caption (`standing`: `figures` makes no overlay of it)."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        if not label_spans:
            return
        regions = [r for r in self.regions if not self.band_ornament(r)]
        words = [(s, l) for l in lines if l.reason is None for s in l.content
                 if s.text.strip() and s.info.family != "icon"]
        rects = regions + [s.rect for s in label_spans] + self.title_bridges + self.column_bridges
        for c in cluster_rects(rects, gap=0.8 * self.body):
            inside = [r for r in regions if r.intersects(c.expand(0.1))]
            if max(c.w, c.h) < 25 or not inside:
                continue
            box = c.expand(1)
            drawings = [d for d in self.raw["drawings"] if d["id"] in self.graphic_drawings
                        and box.contains_rect(self.graphic_drawings[d["id"]], tol=0.5)]
            if not drawings or any(box.intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
                continue
            ends = [(p, d) for d in drawings for p in path_points(d)]
            met = self.met_words(ends, words)
            held = [s for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
            caption = caption_above([s for s, _ in met], held)
            if not met or not caption or not stands_on(c, met, ends, near_words, caption):
                continue
            given = [l for l in lines if l.reason == "figure" and any(s in caption for s in l.spans)]
            for l in given:
                l.reason = None
            self.standing.append(StandingFigure(
                drawing=union_all(inside),
                caption=frozenset(s.id for l in given + [l for _, l in met] for s in l.spans)))

    def release_stranded_labels(self, lines: list[Line], joined: list[Line]) -> None:
        """Labels joined to graphics that `figures` will make no picture of - a cluster under
        25 pt (a timeline's tick mark and its year), or one in the header/footer band - stay
        text: as a figure's they were left in the background, where no one can edit them."""
        if not joined:
            return
        labels = [s.rect for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        regions = [r for r in self.regions if not self.band_ornament(r)]
        for c in cluster_rects(regions + labels + self.column_bridges, gap=0.8 * self.body):
            if max(c.w, c.h) < 25 or ((c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H):
                for l in joined:
                    if c.expand(0.1).contains_rect(l.rect, tol=0.5):
                        l.reason = None

    @staticmethod
    def wrapped_end(line: Line, lines: list[Line]) -> bool:
        """The last words of a paragraph wrapped onto a line of their own ("(no defiers).",
        "cycles"): one pitch below a line of prose in the same size, starting where one of its
        words does. Near a list's drawn label they were taken for a figure's label and left in
        the background, and the native paragraph ended a line early."""
        return any(o is not line and o.reason is None and len(o.text) >= 20 and abs(o.size - line.size) <= 0.05 * line.size
                   and 0.9 * line.size <= line.baseline - o.baseline <= 1.6 * line.size
                   and any(abs(s.rect.x0 - line.rect.x0) <= 1.0 for s in o.content) for o in lines)

    def clip_to_bands(self, c: Rect, box: Rect) -> Rect:
        """A figure's box ends where a headline or footline band drawn after it begins: the band
        hides what of the figure reaches under it (tick labels of a chart set just above the
        footline), and a picture reaching into it showed the band's colours over the layout's
        footline texts, cut in half. (A band drawn first is under the figure on the page too.)"""
        order = {d["id"]: i for i, d in enumerate(self.raw["drawings"])}
        members = [order[i] for i, r in self.graphic_drawings.items() if c.expand(0.5).contains_rect(r, tol=0.5)]
        if not members:
            return box
        last = max(members)
        # (a band of boxes side by side is panels on the edge, see on_edge_artwork)
        decor = {tuple(r.as_list()) for r in self.decorations}
        panels = {p.id for p in self.panels if not p.image}
        x0, y0, x1, y1 = box.as_list()
        for i, d in enumerate(self.raw["drawings"]):
            r = Rect.of(d["bbox"])
            if i < last or r.h > 0.15 * self.H or r.x1 <= box.x0 or r.x0 >= box.x1 or not (
                    d["id"] in self.decor_ids or d["id"] in panels or tuple(r.as_list()) in decor):
                continue
            if r.y1 >= self.H - 1 and box.y0 + 0.5 * box.h < r.y0 < y1:
                y1 = r.y0
            elif r.y0 <= 1 and y0 < r.y1 < box.y1 - 0.5 * box.h:
                y0 = r.y1
        return Rect(x0, y0, x1, y1)

    def holds_other_text(self, c: Rect, label_spans: list[Span]) -> bool:
        """Text drawn inside a figure cluster that is none of its labels stays in the
        background (a section number in a filled node, read with its heading as one formula):
        native shapes rebuilt from the cluster would be drawn over it and hide it."""
        labels = {s.id for s in label_spans}
        box = c.expand(0.5)

        def centre(r: Rect) -> tuple[float, float]:
            return r.cx, r.cy
        return any(s["id"] not in labels and s["text"].strip() and box.contains(*centre(Rect.of(s["bbox"])))
                   for s in self.raw["spans"])

    def plain_rectangles(self, c: Rect, label_spans: list[Span], index: int) -> list[ShapeElement]:
        """A figure cluster that is only opaque filled rectangles without text (progress bars,
        colour swatches, \\rule): native rectangle shapes."""
        box = c.expand(0.5)
        if any(box.contains_rect(s.rect, tol=0.5) for s in label_spans) or self.holds_other_text(c, label_spans) or \
                any(box.intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
            return []
        out: list[ShapeElement] = []
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.intersects(r) or d["id"] in self.decor_ids or self.is_decoration(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            points = [p for _, pts in (d.get("path") or []) for p in pts]
            axis_aligned = d["items"] == "re" or (set(d["items"]) == {"l"} and points and all(
                min(abs(x - r.x0), abs(x - r.x1)) < 0.05 and min(abs(y - r.y0), abs(y - r.y1)) < 0.05 for x, y in points))
            if not box.contains_rect(r, tol=0.5) or d["type"] != "f" or not axis_aligned or not d["fill"] \
                    or d.get("fill_opacity", 1.0) < 0.99:
                return []
            out.append({"id": f"p{self.raw['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                        "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                        "radius": 0.0, "drawing": d["id"], "spans": []})
        if len(out) > MAX_PLAIN_RECTANGLES:
            # A QR code or a pixel grid (426 modules): a picture, not hundreds of shapes nobody
            # would edit one by one, each its own request.
            return []
        # The track of a progress bar runs across the page like a decoration hairline: it goes
        # along (below the bar) when it has exactly the bar's height and contains it.
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if d["type"] == "f" and d["fill"] and d.get("fill_opacity", 1.0) >= 0.99 and d["id"] not in self.decor_ids \
                    and r.x0 > 1 and r.x1 < self.W - 1 and all(o.get("drawing") != d["id"] for o in out) \
                    and any(abs(r.y0 - o["bbox"][1]) < 0.1 and abs(r.y1 - o["bbox"][3]) < 0.1
                            and r.x0 <= o["bbox"][0] + 0.1 and r.x1 >= o["bbox"][2] - 0.1 for o in out):
                out.insert(0, {"id": f"p{self.raw['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                               "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                               "radius": 0.0, "drawing": d["id"], "spans": []})
        return out

    @staticmethod
    def closed_frames(nodes: list[DraftNode], lines: list[DiagramLine]) -> None:
        """Four stroked lines closing a rectangle (\\fbox and \\fcolorbox draw their frame side by
        side) become one rectangle node; a filled rectangle right inside the frame (the
        \\fcolorbox background) takes it as its outline."""
        def extent(ln: DiagramLine, k: int) -> list[float]:
            return sorted((ln["from"][k], ln["to"][k]))
        straight = [l for l in lines if "via" not in l and "sweep" not in l]  # (an arc's ends level is no side)
        horizontal = [l for l in straight if abs(l["from"][1] - l["to"][1]) < 0.05]
        vertical = [l for l in straight if abs(l["from"][0] - l["to"][0]) < 0.05]

        def near(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
            return all(abs(p - q) <= tol for p, q in zip(a, b))
        for top in horizontal:
            for bottom in horizontal:
                y0, y1, tol = top["from"][1], bottom["from"][1], top["width"] + 0.5
                if top not in lines or bottom not in lines or y1 - y0 <= 3 or bottom["stroke"] != top["stroke"] \
                        or not near(extent(bottom, 0), extent(top, 0), tol):
                    continue
                x0, x1 = extent(top, 0)
                sides = [v for v in vertical if v in lines and v["stroke"] == top["stroke"] and near(extent(v, 1), (y0, y1), tol)]
                left = next((v for v in sides if abs(v["from"][0] - x0) <= tol), None)
                right = next((v for v in sides if abs(v["from"][0] - x1) <= tol), None)
                if left is None or right is None or left is right:
                    continue
                rect = Rect(left["from"][0], y0, right["from"][0], y1)
                for ln in (top, bottom, left, right):
                    lines.remove(ln)
                inner = next((i for i, n in enumerate(nodes) if n.shape == "RECTANGLE" and n.stroke is None and
                              rect.expand(tol).contains_rect(n.rect, tol=0.5) and n.rect.w >= rect.w - 2 * tol
                              and n.rect.h >= rect.h - 2 * tol), None)
                if inner is not None:
                    nodes[inner] = replace(nodes[inner], rect=rect, stroke=top["stroke"], width=top["width"],
                                           dash=top.get("dash"))
                else:
                    nodes.append(DraftNode(rect=rect, shape="RECTANGLE", spans=[], fill=None,
                                           stroke=top["stroke"], width=top["width"], radius=None, dash=top.get("dash"),
                                           adjust=None, rotation=None))

    def diagram_from(self, c: Rect, label_spans: list[Span], index: int) -> DiagramElement | DiagramRefusal:
        """A figure cluster made only of simple nodes (rectangles, rounded rectangles, ellipses)
        with their text inside, straight lines and arrow tips: rebuilt from native Slides
        shapes and lines. Anything else (curves, images, math, loose labels) keeps it a picture,
        and the refusal says which."""
        box = c.expand(0.5)

        def refused(reason: Refusal, detail: str) -> DiagramRefusal:
            return DiagramRefusal(box=c, reason=reason, detail=detail)
        inside = [im for im in self.raw["images"] if box.contains_rect(Rect.of(im["bbox"]), tol=0.5)]
        if inside:
            return refused("image_inside", f"{len(inside)} image(s), {inside[0]['id']}")
        if self.holds_other_text(c, label_spans):
            return refused("other_text", "")
        nodes: list[DraftNode] = []
        lines: list[DiagramLine] = []
        tips: list[Tip] = []
        # Small circles, a Circle tip or a dot: (where in nodes, the node, its drawing's index)
        rims: list[tuple[int, DraftNode, int]] = []
        drawn: dict[int, int] = {}  # id of a line -> the index of the drawing it came from
        for k, d in enumerate(self.raw["drawings"]):
            r = Rect.of(d["bbox"])
            if not box.contains_rect(r, tol=0.5) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            path = d.get("path")
            if path is None:
                return refused("unreadable_path", d["id"])
            if d.get("soft_mask") or d.get("fill_opacity", 1.0) < 0.99 or d.get("stroke_opacity", 1.0) < 0.99:
                # A see-through node or line (opacity=0.3 on the steps still to come, a
                # multiplied fill): native shapes came out opaque, the dimmed step drawn in full.
                return refused("see_through", d["id"])
            ops = "".join(op for op, _ in path)
            shapes: dict[str, TemplateKind | None] = {
                "re": "RECTANGLE", "lclclclc": "ROUND_RECTANGLE", "clclclcl": "ROUND_RECTANGLE",
                "cccc": "ELLIPSE" if upright_ellipse(path, r) else None}
            shape = shapes.get(ops)
            points = [p for _, pts in path for p in pts]
            if shape is None and max(r.w, r.h) > 6 and ops in ("llll", "lll") and "f" in d["type"] + "f":
                shape = polygon_shape(points, r)  # decision diamonds, triangles
            preset = preset_shape(path, r, "f" in d["type"]) if shape is None and max(r.w, r.h) > 6 else None
            parts = split_rectangle(path, r) if shape is None and preset is None and r.w > 3 and r.h > 3 else None
            outline_dash = dash_style(d.get("dash", []), d["width"] or 0.4) if "s" in d["type"] else None
            if preset is not None and r.w > 3 and r.h > 3:
                # cylinders, clouds, polygons, pills, tapes: a Slides preset at the path's proportions
                nodes.append(DraftNode(rect=r, shape=preset.shape, spans=[],
                                       fill=d["fill"] if "f" in d["type"] else None,
                                       stroke=d["stroke"] if "s" in d["type"] else None, width=d["width"],
                                       radius=preset.radius, dash=outline_dash, adjust=preset.adjust, rotation=None))
            elif parts is not None:
                # a rectangle split into parts (a UML class): one rectangle per part, stacked
                nodes.extend(DraftNode(rect=part, shape="RECTANGLE", spans=[],
                                       fill=d["fill"] if "f" in d["type"] else None,
                                       stroke=d["stroke"] if "s" in d["type"] else None, width=d["width"],
                                       radius=None, dash=outline_dash, adjust=None, rotation=None) for part in parts)
            elif shape and r.w > 3 and r.h > 3:
                corners = d.get("corners")
                # (a rounded rectangle turned: its box before the turn, its corners' radius)
                rounded = turned_rounded(path, r) if shape == "ROUND_RECTANGLE" else None
                node = DraftNode(rect=rounded[0].rect if rounded else r, shape=shape, spans=[],
                                 fill=d["fill"] if "f" in d["type"] else None,
                                 stroke=d["stroke"] if "s" in d["type"] else None, width=d["width"],
                                 # rounded corners=3pt: without it the node got Slides' default rounding
                                 radius=round(rounded[1], 2) if rounded else
                                 max(corners.values()) if shape == "ROUND_RECTANGLE" and corners else None,
                                 dash=dash_style(d.get("dash", []), d["width"] or 0.4) if "s" in d["type"] else None,
                                 adjust=None, rotation=rounded[0].rotation if rounded else None)
                if shape == "ELLIPSE" and max(r.w, r.h) <= TIP_SIZE:
                    rims.append((len(nodes), node, k))
                else:
                    nodes.append(node)
            elif d["type"] == "s" and set(ops) == {"l"} and max(r.w, r.h) > 6:
                segments = straight_runs([(tuple(a), tuple(b)) for _, (a, b) in path])
                stroke, width = d["stroke"] or "#000000", d["width"] or 0.4
                first = len(lines)
                (p0, p1), (p1b, p2) = segments[0], segments[-1]
                if len(segments) == 2 and math.dist(p1, p1b) < 0.05 and (abs(p0[0] - p1[0]) < 0.05) != (abs(p0[1] - p1[1]) < 0.05) \
                        and (abs(p1[0] - p2[0]) < 0.05) != (abs(p1[1] - p2[1]) < 0.05) \
                        and (abs(p0[0] - p1[0]) < 0.05) != (abs(p1[0] - p2[0]) < 0.05):
                    # An orthogonal connector (|- or -|): one elbow line, vertical first. A -| one
                    # is written from its other end: Slides drew bentConnector3 at adj 1 (turn at
                    # the end) with its turn halfway, three segments; adj 0 draws |- right.
                    if abs(p0[0] - p1[0]) >= 0.05:
                        p0, p2 = p2, p0
                    lines.append({"from": list(p0), "via": list(p1), "to": list(p2), "bend": "vh",
                                  "stroke": stroke, "width": width, "arrow_from": None, "arrow_to": None})
                else:  # straight lines, and other polylines one segment at a time
                    for (x1, y1), (x2, y2) in segments:
                        lines.append({"from": [x1, y1], "to": [x2, y2],
                                      "stroke": stroke, "width": width, "arrow_from": None, "arrow_to": None})
                dash = dash_style(d.get("dash", []), width)
                for ln in lines[first:]:
                    drawn[id(ln)] = k
                    if dash is not None:
                        ln["dash"] = dash
            elif max(r.w, r.h) <= TIP_SIZE and set(ops) <= {"c", "l"}:
                # Arrow heads are small separate paths: stroked (->), filled triangles (latex),
                # filled concave quadrilaterals (stealth), circles, squares, diamonds (tip_head).
                points = [p for _, pts in path for p in pts]
                tips.append(Tip(rect=r, head=tip_head(d["type"], d["fill"], d["stroke"], path,
                                                      ops == "cccc" and upright_ellipse(path, r)),
                                points=points, outline=d["width"] if "s" in d["type"] and d["width"] else 0.0,
                                closed=closed_path(path), on=None))
            else:
                turned = turned_shape(path, r) if r.w > 3 and r.h > 3 else None
                if turned is not None:
                    # an ellipse or a rectangle turned (rotate=30): its box before the turn, turned
                    nodes.append(DraftNode(rect=turned.rect, shape=turned.shape, spans=[],
                                           fill=d["fill"] if "f" in d["type"] else None,
                                           stroke=d["stroke"] if "s" in d["type"] else None, width=d["width"],
                                           radius=None, dash=outline_dash, adjust=None, rotation=turned.rotation))
                    continue
                curve =d["type"] == "s" and "c" in ops and set(ops) <= {"c", "l"}
                # A stroked curve (bend left, out/in, a loop, a brace) as circular arcs and
                # straight pieces; one no chain of `MAX_PIECES` follows (a coil) stays a picture.
                pieces = curves.path_pieces(path, curves.ARC_TOLERANCE) if curve else None
                if pieces is None:
                    return refused("curve" if curve else "unknown_shape",
                                   f"{d['id']} {d['type']} {ops[:24]} {r.w:.0f}x{r.h:.0f} pt")
                width = d["width"] or 0.4
                first = len(lines)
                lines += curve_lines(pieces, d["stroke"] or "#000000", width)
                dash = dash_style(d.get("dash", []), width)
                for ln in lines[first:]:
                    drawn[id(ln)] = k
                    if dash is not None:
                        ln["dash"] = dash
        # A small circle drawn right after a line whose end is on its rim is that line's Circle
        # tip (TikZ draws a path, then its heads); any other is a dot node, kept in its place.
        for at, node, k in reversed(rims):
            owners = [(ln, end) for ln in lines for end in ENDS if 0 < k - drawn.get(id(ln), k) <= 2
                      and on_rim(node.rect, ln[end], ln.get("via") or ln["to" if end == "from" else "from"])]
            drawing = self.raw["drawings"][k]
            if len(owners) == 1:
                path = drawing.get("path") or []
                tips.append(Tip(rect=node.rect, head=tip_head(drawing["type"], drawing["fill"], drawing["stroke"], path, True),
                                points=[p for _, pts in path for p in pts],
                                outline=drawing["width"] if "s" in drawing["type"] and drawing["width"] else 0.0,
                                closed=True, on=owners[0]))
            else:
                nodes.insert(at, node)
        self.closed_frames(nodes, lines)
        if not nodes:
            return refused("no_nodes", f"{len(lines)} line(s)")
        # Shapes that cross each other (a Venn diagram): its words are placed by region - the
        # lens, one circle's own part - and a node's text is set centred in all of it.
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                ra, rb = a.rect, b.rect
                if overlap(ra, rb) > 0.05 * min(ra.w * ra.h, rb.w * rb.h) and \
                        not ra.contains_rect(rb, tol=0.5) and not rb.contains_rect(ra, tol=0.5):
                    return refused("nodes_cross", f"{a.shape} {ra.as_list()} and {b.shape} {rb.as_list()}")
        for t in tips:
            tip, head, points, outline = t.rect, t.head, t.points, t.outline
            ends = [t.on] if t.on else [(ln, end) for ln in lines for end in ENDS if tip.expand(1).contains(*ln[end])]
            if not ends:
                return refused("loose_arrow_tip", f"{head} at {tip.as_list()}")
            ln, end = ends[0]
            if end == "from":
                ln["arrow_from"] = head
            else:
                ln["arrow_to"] = head
            if (head != "OPEN_ARROW" or t.closed) and "sweep" in ln:
                extend_arc(ln, end, points, outline)
            elif head != "OPEN_ARROW" or t.closed:
                # TikZ stops the line where a filled (or closed hollow) head begins; Slides draws
                # an arrow's point at the line's end, and centres a circle, square or diamond on
                # it, so extend the line to the point or the centre.
                other = ln.get("via") or (ln["to"] if end == "from" else ln["from"])
                ux, uy = ln[end][0] - other[0], ln[end][1] - other[1]
                length = (ux * ux + uy * uy) ** 0.5 or 1.0
                ux, uy = ux / length, uy / length
                reach = max((px - ln[end][0]) * ux + (py - ln[end][1]) * uy for px, py in points)
                # A head filled and stroked (ultra thick ->) reaches past its path by its outline's
                # mitred point: the line stopped that short of the node, the black edge drawn
                # under it showing as a stub at the tip.
                reach += miter_reach(points, (ux, uy), outline)
                if head in CENTRED_HEADS:
                    reach = (tip.cx - ln[end][0]) * ux + (tip.cy - ln[end][1]) * uy
                if reach > 0:
                    ln[end] = [round(ln[end][0] + reach * ux, 2), round(ln[end][1] + reach * uy, 2)]

        spans = [s for s in label_spans if box.contains_rect(s.rect, tol=0.5)]
        # Simple math in a label is runs as in a table cell (`span_runs`: Greek, operators, x_t
        # set SUBSCRIPT); a big operator, a fraction or a radical's bar is no run of text.
        if any(box.contains_rect(b, tol=0.5) for b in self.bars):
            return refused("math_label", "a fraction or radical bar")
        free: list[Span] = []

        def area(n: DraftNode) -> float:
            return n.rect.w * n.rect.h
        seen: list[Span] = []
        sloped: list[Span] = []
        for s in spans:
            # (an icon font's glyph has no Unicode: as a node's label it read U+FFFD, a
            # diamond with a question mark, where the picture shows the icon)
            if s.info.family == "math" and extension_font(s.font):
                return refused("math_label", repr(s.text))
            if s.info.family == "icon" or "�" in s.text:
                return refused("icon_label", repr(s.text))
            if not s.horizontal:
                sloped.append(s)  # (set along its turn below: sloped_labels)
                continue
            # A node drawn again on a later overlay step (\node<2->[fill=yellow] at (a) {A})
            # paints its label a second time on the same spot: one label, and it goes to the
            # copy on top - the last drawn of the smallest nodes around it. Given to the first,
            # it came out doubled ("LexerLexer") under an empty filled box.
            if any(o.text == s.text and abs(o.rect.x0 - s.rect.x0) <= 0.1 and abs(o.baseline - s.baseline) <= 0.1
                   and abs(o.size - s.size) <= 0.1 for o in seen):
                continue
            seen.append(s)
            owners = [n for n in nodes if holds(n.rect, n.rotation, s.rect.cx, s.rect.cy)]
            if owners:
                smallest = min(map(area, owners))
                owner = [n for n in owners if area(n) <= 1.02 * smallest + 0.01][-1]
                if owner.rotation is not None:
                    # (`shape border rotate`: the outline turned, its words not - a label in a
                    # shape turns with it in Slides)
                    return refused("rotated_node", f"a level label {s.text!r} in a turned {owner.shape}")
                owner.spans.append(s)
            else:
                free.append(s)  # edge labels and captions: a text box in the group
        empty = sum(n.shape is not None and not n.spans for n in nodes)
        if empty > MAX_PLAIN_RECTANGLES:
            return refused("too_many_rectangles", f"{empty} empty nodes")  # a QR code, a pixel grid
        # Free labels close together on one line are one label, a script with its letter. (Close
        # on both sides: two edge labels joined across the node between them read "connect
        # SYN+ACK" over two arrows.)
        for row in text_rows(free):
            for s in row:
                last = nodes[-1] if nodes and nodes[-1].shape is None else None
                if last and continues(last.spans[-1], s):
                    last.spans.append(s)
                    nodes[-1] = replace(last, rect=last.rect.union(s.rect))
                else:
                    nodes.append(DraftNode(rect=s.rect, shape=None, spans=[s], fill=None, stroke=None, width=None,
                                           radius=None, dash=None, adjust=None, rotation=None))
        if sloped:
            raws = {r["id"]: r for r in self.raw["spans"]}
            unread = next((s for s in sloped if s.id not in raws), None)
            if unread is not None:
                return refused("rotated_label", f"{unread.text!r}: no span of the page")
            problem = sloped_labels(nodes, [(s, raws[s.id]) for s in sloped])
            if problem is not None:
                return refused(problem[0], problem[1])

        out_nodes: list[Node] = []
        for n in nodes:
            rows = text_rows(n.spans)
            if n.rotation is not None and n.shape and card_text(n.rect, rows) is not None:
                return refused("rotated_node", f"a card's text in a turned {n.shape}")
            out_nodes.append({
                "bbox": n.rect.as_list(), "shape": n.shape, "fill": n.fill, "stroke": n.stroke,
                "width": n.width, "paragraphs": [span_runs(row) for row in rows],
                "baselines": [round(max(row, key=lambda s: s.size).baseline, 2) for row in rows],
                "label_w": round(max((max(s.rect.x1 for s in row) - min(s.rect.x0 for s in row) for row in rows), default=0.0), 2),
                "text": card_text(n.rect, rows) if n.shape else None,
                **({"radius": n.radius} if n.radius is not None else {}),
                **({"dash": n.dash} if n.dash is not None else {}),
                **({"adjust": n.adjust} if n.adjust is not None else {}),
                **({"rotation": round(n.rotation, 2)} if n.rotation is not None else {}),
            })
        return {"id": f"p{self.raw['index']}dg{index}", "kind": "diagram", "role": "figure",
                "bbox": c.expand(1.0).as_list(), "nodes": out_nodes, "lines": lines,
                "spans": [s.id for s in spans]}


def scripted(a: Span, b: Span) -> bool:
    """One of the two is a script of the other (h_{t-1}, x^2): smaller by TeX's script or
    scriptscript size (0.7 and 0.5 of the text), off its letter's baseline by less than the
    letter's height, and beside it. (A caption under a card's big number is smaller still.)"""
    big, small = (a, b) if a.size >= b.size else (b, a)
    gap = max(small.rect.x0 - big.rect.x1, big.rect.x0 - small.rect.x1)
    return 0.45 * big.size <= small.size < 0.85 * big.size and abs(a.baseline - b.baseline) < 0.6 * big.size \
        and -0.3 * big.size <= gap <= 0.3 * big.size


def text_rows(spans: Sequence[Span]) -> list[list[Span]]:
    """Spans by line, top to bottom, each left to right: a span is on the line whose largest
    span it is level with, or on the line of the letter it is a script of - which read on its
    own baseline it had left as a line of its own."""
    rows: list[list[Span]] = []
    for s in sorted(spans, key=lambda s: (s.baseline, s.rect.x0)):
        if rows:
            row = rows[-1]
            anchor = max(row, key=lambda x: x.size)
            if abs(s.baseline - anchor.baseline) <= 0.5 * min(s.size, anchor.size) or any(scripted(a, s) for a in row):
                row.append(s)
                continue
        rows.append([s])
    return [sorted(row, key=lambda s: s.rect.x0) for row in rows]


def continues(a: Span, b: Span) -> bool:
    """b, after a on a line, is more of a's label: on its baseline and close, or a script of it
    or its letter."""
    gap = b.rect.x0 - a.rect.x1
    return (abs(a.baseline - b.baseline) <= 0.3 * b.size and -0.3 * b.size <= gap <= 0.5 * b.size) or scripted(a, b)


def sloped_labels(nodes: list[DraftNode], sloped: Sequence[tuple[Span, RawSpan]]) -> tuple[Refusal, str] | None:
    """Sloped spans set along their turn (classify_turned): a turned node's words go into it, read
    upright in its frame, the node described along their direction (`reframe`); the others are
    free labels, each a text box turned about its centre (`rotation`), joined as level ones are
    (`text_rows`, `continues`) once upright. What a turned Slides text box cannot say is refused
    (None: all placed): a span set vertically or upside down, words sloped in an upright node or
    across a turned one, words of two directions in one node."""
    framed: list[tuple[Span, SpanFrame]] = []
    for s, raw in sloped:
        f = span_frame(raw)
        if f is None:
            return "rotated_label", f"{s.text!r} set vertically or upside down"
        framed.append((s, f))
    owned: dict[int, list[tuple[Span, SpanFrame]]] = {}
    free: list[tuple[Span, SpanFrame]] = []
    for s, f in framed:
        cx, cy = f.centre()
        owners = [k for k, n in enumerate(nodes) if n.shape is not None and holds(n.rect, n.rotation, cx, cy)]
        if not owners:
            free.append((s, f))
            continue
        smallest = min(nodes[k].rect.w * nodes[k].rect.h for k in owners)
        owner = [k for k in owners if nodes[k].rect.w * nodes[k].rect.h <= 1.02 * smallest + 0.01][-1]
        owned.setdefault(owner, []).append((s, f))
    for k, words in owned.items():
        n = nodes[k]
        text = "".join(s.text for s, _ in words)
        if n.rotation is None:
            return "rotated_label", f"{text!r} sloped in an upright {n.shape}"
        if any(not same_turn(f.rotation, words[0][1].rotation) for _, f in words):
            return "rotated_label", f"{text!r}: words of two directions in one {n.shape}"
        framing = reframe(n.rect, n.rotation, words[0][1].rotation)
        if framing is None:
            return "rotated_label", f"{text!r} across its turned {n.shape}"
        rect, rotation = framing
        nodes[k] = replace(n, rect=rect, rotation=rotation,
                           spans=n.spans + unturned_spans(words, (rect.cx, rect.cy), rotation))
    groups: list[list[tuple[Span, SpanFrame]]] = []
    for s, f in sorted(free, key=lambda sf: sf[1].rotation):
        if groups and same_turn(groups[-1][0][1].rotation, f.rotation):
            groups[-1].append((s, f))
        else:
            groups.append([(s, f)])
    for group in groups:
        pivot, rotation = group[0][1].origin, round(group[0][1].rotation, 2)
        labels: list[list[Span]] = []
        for row in text_rows(unturned_spans(group, pivot, rotation)):
            for s in row:
                if labels and continues(labels[-1][-1], s):
                    labels[-1].append(s)
                else:
                    labels.append([s])
        for spans in labels:
            r = union_all([s.rect for s in spans])
            # the label upright about its own centre, where the turn about the pivot put it
            px, py = turn((r.cx, r.cy), pivot, rotation)
            dx, dy = px - r.cx, py - r.cy
            nodes.append(DraftNode(rect=Rect(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy), shape=None,
                                   spans=[moved(s, dx, dy) for s in spans], fill=None, stroke=None, width=None,
                                   radius=None, dash=None, adjust=None, rotation=rotation))
    return None


def curve_lines(pieces: Sequence[curves.Piece], stroke: str, width: float) -> list[DiagramLine]:
    """A stroked curve's pieces as diagram lines: an arc keeps its `sweep`, a straight piece is a line."""
    out: list[DiagramLine] = []
    for p in pieces:
        ln: DiagramLine = {"from": [round(p.start[0], 2), round(p.start[1], 2)], "to": [round(p.end[0], 2), round(p.end[1], 2)],
                           "stroke": stroke, "width": width, "arrow_from": None, "arrow_to": None}
        if p.sweep is not None and ln["from"] != ln["to"]:
            ln["sweep"] = p.sweep
        out.append(ln)
    return out


def extend_arc(ln: DiagramLine, end: End, points: Sequence[Sequence[float]], outline: float) -> None:
    """A filled arrow head on an arc: the arc grown along its circle to the head's tip, as a
    straight line is extended to it (TikZ stops the stroke where the head begins)."""
    sweep = ln.get("sweep")
    if sweep is None:
        return
    start, stop = (ln["from"][0], ln["from"][1]), (ln["to"][0], ln["to"][1])
    at = start if end == "from" else stop
    ux, uy = curves.end_direction(start, stop, sweep, end)
    reach = max((p[0] - at[0]) * ux + (p[1] - at[1]) * uy for p in points)
    reach += miter_reach(points, (ux, uy), outline)
    if reach > 0:
        a, b, grown = curves.extended(start, stop, sweep, end, reach)
        if abs(grown) < 360 - curves.ARC_STEP:
            ln["from"], ln["to"], ln["sweep"] = [a[0], a[1]], [b[0], b[1]], round(grown, 2)
