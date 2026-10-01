"""Curved strokes as circular arcs, the curve Slides can draw and keep an arrow head on.

A .pptx brings in the preset `arc` (Slides' ARC shape) with its arrow heads, which emit copies
from a template shape as it does rounded corners (`emit_pptx._add_template_shapes`); a free path
(`a:custGeom`) loses its heads on import and cannot be made through the API at all, so a sync could
never write one (tools/probe_curves.py). A TikZ `bend left` edge is one cubic close to a circular
arc; any other stroked curve (out/in angles, a loop, a brace) is a chain of arcs and straight
pieces, each within `ARC_TOLERANCE` of the PDF's path (`path_pieces`).

A diagram line with a `sweep` (ir.DiagramLine) is such an arc: from `from` to `to`, turning
`sweep` degrees, positive clockwise on the page (y down), as the arc preset turns. Its circle is
the one through both ends (`arc_circle`), so an arc drawn with its sweep rounded to `ARC_STEP`
(the template it is copied from: `drawn_sweep`) still ends on both points, its bulge off by at
most 0.11% of its chord.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Point = tuple[float, float]

ARC_STEP = 1.0          # degrees: an arc is drawn with its sweep rounded to this (one template per sweep)
ARC_TOLERANCE = 0.4     # pt: how far an arc (as drawn, its sweep rounded) may run from the PDF's curve
MAX_PIECES = 12         # arcs and straight pieces one stroked path may become; more stays a picture
MAX_SPLITS = 4          # times a cubic is halved looking for arcs that fit it (16 pieces at most)
SAMPLES = 24            # points of a cubic its fit is checked on
MAX_RADIUS = 600.0      # pt: a flatter arc is straight pieces (an arc's shape is its whole circle's box)

End = Literal["from", "to"]


@dataclass(frozen=True, kw_only=True)
class Piece:
    """One piece of a stroked path: straight (`sweep` None) or a circular arc turning `sweep`
    degrees from `start` to `end` (positive: clockwise on the page)."""
    start: Point
    end: Point
    sweep: float | None


@dataclass(frozen=True, kw_only=True)
class Circle:
    """An arc's circle: its centre and radius, and the angle (degrees, clockwise from +x on the
    page) at which the arc starts."""
    centre: Point
    radius: float
    start: float


def drawn_sweep(sweep: float) -> float:
    """The sweep an arc is drawn with: rounded to `ARC_STEP`, its sign kept, never 0 or a full turn."""
    steps = min(max(round(abs(sweep) / ARC_STEP), 1), round(360 / ARC_STEP) - 1)
    return math.copysign(steps * ARC_STEP, sweep)


def arc_circle(start: Point, end: Point, sweep: float) -> Circle:
    """The circle on which an arc turning `sweep` degrees runs from `start` to `end`."""
    (x0, y0), (x1, y1) = start, end
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord <= 1e-9:
        raise ValueError("an arc's ends are one point")
    half = math.radians(abs(sweep)) / 2
    radius = chord / (2 * math.sin(half))
    ux, uy = (x1 - x0) / chord, (y1 - y0) / chord
    # Clockwise on the page (y down) the centre lies to the right of the way from start to end
    # (the chord turned a quarter clockwise), past the chord when the arc is more than half a turn.
    side = 1.0 if sweep > 0 else -1.0
    d = radius * math.cos(half) * side
    cx, cy = (x0 + x1) / 2 - uy * d, (y0 + y1) / 2 + ux * d
    return Circle(centre=(cx, cy), radius=radius, start=math.degrees(math.atan2(y0 - cy, x0 - cx)))


def arc_point(circle: Circle, angle: float) -> Point:
    """The point of `circle` at `angle` degrees past the arc's start (clockwise positive)."""
    a = math.radians(circle.start + angle)
    return circle.centre[0] + circle.radius * math.cos(a), circle.centre[1] + circle.radius * math.sin(a)


def end_direction(start: Point, end: Point, sweep: float, which: End) -> Point:
    """The unit vector an arc leaves by at one end, pointing away from the arc (as a straight
    line's end points away from its other end)."""
    c = arc_circle(start, end, sweep)
    a = math.radians(c.start + (sweep if which == "to" else 0.0))
    turn = 1.0 if sweep > 0 else -1.0   # the way the arc runs at angle a: d/da (cos a, sin a), signed
    tx, ty = -math.sin(a) * turn, math.cos(a) * turn
    return (tx, ty) if which == "to" else (-tx, -ty)


def extended(start: Point, end: Point, sweep: float, which: End, reach: float) -> tuple[Point, Point, float]:
    """The arc grown `reach` pt along its circle at one end (a filled arrow head's tip: TikZ stops
    the stroke where the head starts, Slides draws the head at the stroke's end): (start, end, sweep)."""
    c = arc_circle(start, end, sweep)
    more = math.copysign(math.degrees(reach / c.radius), sweep)
    if which == "to":
        return start, _rounded(arc_point(c, sweep + more)), sweep + more
    grown = Circle(centre=c.centre, radius=c.radius, start=c.start - more)
    return _rounded(arc_point(grown, 0.0)), end, sweep + more


def _rounded(p: Point) -> Point:
    return round(p[0], 2), round(p[1], 2)


Cubic = tuple[Point, Point, Point, Point]


def _at(c: Cubic, t: float) -> Point:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = c
    u = 1 - t
    a, b, d, e = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return a * x0 + b * x1 + d * x2 + e * x3, a * y0 + b * y1 + d * y2 + e * y3


def _halves(c: Cubic) -> tuple[Cubic, Cubic]:
    """De Casteljau at t = 0.5."""
    p0, p1, p2, p3 = c

    def mid(a: Point, b: Point) -> Point:
        return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    a, b, d = mid(p0, p1), mid(p1, p2), mid(p2, p3)
    e, f = mid(a, b), mid(b, d)
    m = mid(e, f)
    return (p0, a, e, m), (m, f, d, p3)


def _samples(c: Cubic) -> list[Point]:
    return [_at(c, i / SAMPLES) for i in range(SAMPLES + 1)]


def _off_segment(p: Point, a: Point, b: Point) -> float:
    """How far `p` lies from the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 <= 1e-12 else min(1.0, max(0.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def arc_deviation(points: Sequence[Point], start: Point, end: Point, sweep: float) -> float:
    """How far the farthest of `points` lies from the arc (its circle within the arc's span, else
    the nearer end)."""
    c = arc_circle(start, end, sweep)
    worst = 0.0
    for p in points:
        angle = math.degrees(math.atan2(p[1] - c.centre[1], p[0] - c.centre[0]))
        past = ((angle - c.start) * (1 if sweep > 0 else -1)) % 360  # how far along the arc, 0..360
        if past <= abs(sweep) + 1e-6:
            off = abs(math.hypot(p[0] - c.centre[0], p[1] - c.centre[1]) - c.radius)
        else:
            off = min(math.dist(p, start), math.dist(p, end))
        worst = max(worst, off)
    return worst


def _sweep_through(a: Point, m: Point, b: Point) -> float | None:
    """The signed sweep of the arc from a through m to b (degrees, clockwise positive), None when
    the three points are on one line or two of them coincide."""
    cross = (m[0] - a[0]) * (b[1] - m[1]) - (m[1] - a[1]) * (b[0] - m[0])
    ab, am, mb = math.dist(a, b), math.dist(a, m), math.dist(m, b)
    if min(ab, am, mb) <= 1e-6 or abs(cross) <= 1e-9 * ab * ab:
        return None
    # The arc's angle at the centre is twice the inscribed angle at the far side: 360 - 2 * (angle at m).
    cos_m = ((m[0] - a[0]) * (m[0] - b[0]) + (m[1] - a[1]) * (m[1] - b[1])) / (am * mb)
    at_m = math.degrees(math.acos(max(-1.0, min(1.0, cos_m))))
    return math.copysign(360 - 2 * at_m, cross)


def _fit(c: Cubic, depth: int, tol: float) -> list[Piece] | None:
    p0, _, _, p3 = c
    points = _samples(c)
    if math.dist(p0, p3) > 1e-6 and max(_off_segment(p, p0, p3) for p in points) <= tol:
        return [Piece(start=p0, end=p3, sweep=None)]
    sweep = _sweep_through(p0, _at(c, 0.5), p3)
    if sweep is not None and abs(sweep) >= ARC_STEP / 2:
        drawn = drawn_sweep(sweep)
        if arc_circle(p0, p3, drawn).radius <= MAX_RADIUS and arc_deviation(points, p0, p3, drawn) <= tol:
            return [Piece(start=p0, end=p3, sweep=round(sweep, 2))]
    if depth >= MAX_SPLITS:
        return None
    first, second = _halves(c)
    a = _fit(first, depth + 1, tol)
    b = _fit(second, depth + 1, tol) if a is not None else None
    return None if a is None or b is None else a + b


def path_pieces(path: Sequence[tuple[str, Sequence[Sequence[float]]]], tol: float) -> list[Piece] | None:
    """A stroked path of lines ('l') and cubics ('c', its start point first) as straight pieces
    and circular arcs, each within `tol` pt of it; None when that takes more than `MAX_PIECES`
    pieces (a coil, a wave) or the path holds anything else."""
    out: list[Piece] = []
    for op, pts in path:
        ps = [(float(p[0]), float(p[1])) for p in pts]
        if op == "l" and len(ps) == 2:
            if math.dist(ps[0], ps[1]) > 1e-6:
                out.append(Piece(start=ps[0], end=ps[1], sweep=None))
        elif op == "c" and len(ps) == 4:
            pieces = _fit((ps[0], ps[1], ps[2], ps[3]), 0, tol)
            if pieces is None:
                return None
            out += pieces
        else:
            return None
        if len(out) > MAX_PIECES:
            return None
    return _joined(out)


def _joined(pieces: list[Piece]) -> list[Piece]:
    """Straight pieces running on in one direction are one (a cubic TikZ wrote along a straight line)."""
    out: list[Piece] = []
    for p in pieces:
        last = out[-1] if out else None
        if last is not None and last.sweep is None and p.sweep is None and math.dist(last.end, p.start) <= 1e-6 \
                and _off_segment(last.end, last.start, p.end) <= 0.01:
            out[-1] = Piece(start=last.start, end=p.end, sweep=None)
        else:
            out.append(p)
    return out
