"""Turned nodes and sloped labels in a diagram (TikZ's `rotate=30`, an edge label `sloped`): the
geometry `diagram_from` needs to describe them as Slides draws them - a box before the turn, and
the turn, degrees clockwise on the page as Slides shows it (ir.Node `rotation`).

A sloped span is read back into the frame it was set in (`span_frame`): extract boxes a turned
glyph run by the corners of its turned font box, so its origin, direction and box give its ascent,
descent and length. That length is PDFium's loose advance, which runs long on a turned glyph (its
loose box is itself the upright box of the turned glyph), and is corrected by what the turn added.
Glyphs of one word abut once unturned (`unturned_spans`), so a label read one glyph per span (the
loose advance broke extract's runs) is still one word.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .classify_model import Rect, Span
from .raw_types import RawSpan

Point = tuple[float, float]

SAME_TURN = 1.0      # degrees: spans turned within this of each other are set along one direction
LEVEL_ENOUGH = 0.2   # a direction whose x part is below this (all but vertical, or reading leftwards)
                     # is no sloped label: a text box turned that far reads upside down or sideways
ABUT = 0.25          # em: a glyph's estimated end this close to the next glyph's origin is one word's
SAME_FRAME = 2.0     # degrees: a label within this of one of its node's directions is set along it


@dataclass(frozen=True, kw_only=True)
class SpanFrame:
    """A sloped span in the frame it was set in: its turn (degrees clockwise on the page), its
    origin, its estimated advance and its font box's ascent and descent (pt, both up from and
    down from the baseline)."""
    rotation: float
    origin: Point
    advance: float
    ascent: float
    descent: float

    def centre(self) -> Point:
        """The centre of the span's font box on the page."""
        c, s = math.cos(math.radians(self.rotation)), math.sin(math.radians(self.rotation))
        along, up = self.advance / 2, (self.ascent - self.descent) / 2
        return self.origin[0] + along * c + up * s, self.origin[1] + along * s - up * c


def span_frame(raw: RawSpan) -> SpanFrame | None:
    """A sloped span's frame from its origin, direction and box (`pdf.api.char_box`: the upright
    box of its turned font box); None for a span set (nearly) vertically or reading leftwards."""
    dx, dy = raw["dir"][0], raw["dir"][1]
    norm = math.hypot(dx, dy)
    if norm == 0:
        return None
    c, s = dx / norm, dy / norm
    if c < LEVEL_ENOUGH:
        return None
    ox, oy = raw["origin"][0], raw["origin"][1]
    y0, x1, y1 = raw["bbox"][1], raw["bbox"][2], raw["bbox"][3]  # (the left edge says nothing more)
    # The box's corners are origin + t (c, s) + u (s, -c), t in [0, L], u from -descent to ascent.
    if s >= 0:
        ascent = (oy - y0) / c
        length = (x1 - ox - ascent * s) / c
        descent = -(oy + length * s - y1) / c
    else:
        descent = (y1 - oy) / c
        length = (x1 - ox + descent * s) / c
        ascent = (oy + length * s - y0) / c
    # PDFium's loose advance is the upright box of the glyph's turned loose box, read along the
    # direction: its height added 2 |c s| of itself.
    advance = max(length - 2 * (ascent + descent) * abs(c * s), 0.2 * raw["size"])
    return SpanFrame(rotation=math.degrees(math.atan2(s, c)), origin=(ox, oy), advance=advance,
                     ascent=ascent, descent=descent)


def turn(p: Point, centre: Point, rotation: float) -> Point:
    """`p` turned `rotation` degrees clockwise on the page (y down) about `centre`."""
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    dx, dy = p[0] - centre[0], p[1] - centre[1]
    return centre[0] + c * dx - s * dy, centre[1] + s * dx + c * dy


def holds(rect: Rect, rotation: float | None, x: float, y: float) -> bool:
    """A node's box (before its turn, if it has one) holds the page point (x, y)."""
    if rotation is None:
        return rect.contains(x, y)
    ux, uy = turn((x, y), (rect.cx, rect.cy), -rotation)
    return rect.contains(ux, uy)


def same_turn(a: float, b: float) -> bool:
    return abs((a - b + 180) % 360 - 180) <= SAME_TURN


def reframe(rect: Rect, rotation: float, label: float) -> tuple[Rect, float] | None:
    """A turned node's box before the turn and its turn, described along the direction of its
    label (turned `label` degrees): the node's own turn plus the quarter turns nearest the label's,
    width and height swapped for an odd number of them. None when the label runs along none of the
    node's directions (within `SAME_FRAME` degrees)."""
    for quarter in range(4):
        turned = (rotation + 90 * quarter + 180) % 360 - 180
        if abs((label - turned + 180) % 360 - 180) <= SAME_FRAME:
            w, h = (rect.w, rect.h) if quarter % 2 == 0 else (rect.h, rect.w)
            return Rect(rect.cx - w / 2, rect.cy - h / 2, rect.cx + w / 2, rect.cy + h / 2), round(turned, 2)
    return None


def unturned_spans(spans: Sequence[tuple[Span, SpanFrame]], pivot: Point, rotation: float) -> list[Span]:
    """Sloped spans set upright about `pivot` (turned back `rotation` degrees): each its font box
    from its origin, its baseline that origin's; a glyph whose estimated end is within `ABUT` em
    of the next one's origin on its line reaches it (one word, as a level line reads it)."""
    out: list[Span] = []
    for s, f in spans:
        ox, oy = turn(f.origin, pivot, -rotation)
        out.append(replace(s, rect=Rect(ox, oy - f.ascent, ox + f.advance, oy + f.descent), baseline=oy,
                           horizontal=True))
    out.sort(key=lambda s: (round(s.baseline, 1), s.rect.x0))
    for k in range(len(out) - 1):
        a, b = out[k], out[k + 1]
        gap = b.rect.x0 - a.rect.x1
        if abs(a.baseline - b.baseline) <= 0.3 * a.size and -0.3 * a.size < gap < ABUT * a.size:
            out[k] = replace(a, rect=Rect(a.rect.x0, a.rect.y0, b.rect.x0, a.rect.y1))
    return out


def moved(s: Span, dx: float, dy: float) -> Span:
    """A span moved by (dx, dy)."""
    r = s.rect
    return replace(s, rect=Rect(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy), baseline=s.baseline + dy)
