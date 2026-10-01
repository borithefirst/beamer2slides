"""Node shapes `diagram_from` reads off a drawing's path beyond the plain ones (a rectangle, a
rounded rectangle, an upright ellipse, a diamond, a triangle): the Slides preset that draws like
the path, and that preset's adjustment where its proportions take one.

Every recogniser takes the drawing's path and its box (PDF pt, y down) and answers None when the
path is not that shape closely enough: a near miss (an irregular hexagon, a lopsided cylinder)
stays a picture rather than becoming a preset that draws something else.

The presets are drawn by OOXML's presetShapeDefinitions (a .pptx preset is what Slides copies the
node from), `ss` being the box's shorter side:
- `can`: the ellipse on top is adj x ss tall (`y1 = ss a / 200000` is its half height);
- `hexagon`: points on the middle of the left and right sides, the top and bottom corners cut
  adj x ss in from them (`x1 = ss a / 100000`; its default vf 115470 fills the box's height);
- `octagon`: every corner cut adj x ss along both sides;
- `pentagon`, `heptagon`, `decagon`, `dodecagon`: regular polygons with a flat side at the bottom,
  fitted to their box by their default hf/vf - as TikZ's `regular polygon` draws them;
- `cloud`: a fixed outline of puffs, no adjustment;
- `flowChartPunchedTape`: straight sides, top and bottom waves a tenth of the height deep, the top
  dipping in on the left and the bottom on the right - TikZ's `tape` with its default bends;
- `roundRect` at adj 0.5: a pill (TikZ's `rounded rectangle`), its ends half circles.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .classify_model import Rect
from .ir import TemplateKind
from .raw_types import PathItem

Point = tuple[float, float]

# Regular polygons Slides has a preset for, by their number of sides (6 and 8 are read as the
# adjustable hexagon and octagon, which draw the regular ones too).
REGULAR: dict[int, TemplateKind] = {5: "PENTAGON", 7: "HEPTAGON", 10: "DECAGON", 12: "DODECAGON"}
CLOUD_PUFFS = 6       # fewer cusps than this is no cloud (a flower, a wavy blob)
CUSP = math.radians(60)  # a join whose tangent turns this much is a cusp between two puffs
TAPE_BEND = 0.1       # flowChartPunchedTape's wave: a tenth of the height deep, a tenth from the edge
TAPE_ROOM = 0.035     # ... give or take this share of the height


@dataclass(frozen=True, kw_only=True)
class Preset:
    """A node drawn as a Slides preset."""
    shape: TemplateKind
    adjust: float | None
    """The preset's adjustment as a share of the box's shorter side (OOXML adj / 100000) where its
    proportions take one: a can's ellipse height, a hexagon's or an octagon's corner cut."""
    radius: float | None
    """A rounded rectangle's corner radius (pt): a pill's is half its shorter side."""


def tolerance(r: Rect) -> float:
    """How far (pt) a path's point may lie from where the preset puts it."""
    return 0.03 * max(r.w, r.h) + 0.2


def bezier(pts: Sequence[Sequence[float]], t: float) -> Point:
    """The point at `t` of a cubic curve [start, control, control, end]."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts[0], pts[1], pts[2], pts[3]
    u = 1 - t
    return (u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
            u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3)


def chained(path: list[PathItem], tol: float) -> bool:
    """The pieces join end to start, the last back to the first: one closed outline."""
    return all(math.dist(path[k][1][0], path[k - 1][1][-1]) <= tol for k in range(len(path)))


def corners_of(points: Sequence[Sequence[float]], tol: float) -> list[Point]:
    """A polyline's corners: its points with repeats, a closing repeat of the first, and points
    the outline runs straight through (a chamfer drawn as two halves) left out."""
    vs: list[Point] = []
    for p in points:
        if not vs or math.dist(p, vs[-1]) > tol / 4:
            vs.append((p[0], p[1]))
    if len(vs) > 2 and math.dist(vs[0], vs[-1]) <= tol / 4:
        vs.pop()
    k = 0
    while len(vs) > 3 and k < len(vs):
        a, v, b = vs[k - 1], vs[k], vs[(k + 1) % len(vs)]
        ux, uy, wx, wy = v[0] - a[0], v[1] - a[1], b[0] - v[0], b[1] - v[1]
        lengths = math.hypot(ux, uy) * math.hypot(wx, wy)
        if lengths > 0 and abs(ux * wy - uy * wx) / lengths < 0.02 and ux * wx + uy * wy > 0:
            vs.pop(k)
        else:
            k += 1
    return vs


def _matches(vs: Sequence[Point], expected: Sequence[Point], tol: float) -> bool:
    """Every expected corner is one of `vs`, and there are no others."""
    return len(vs) == len(expected) and all(any(math.dist(v, e) <= tol for v in vs) for e in expected)


def hexagon_cut(vs: Sequence[Point], r: Rect, tol: float) -> float | None:
    """The corner cut (pt) of a hexagon drawn as OOXML's: points on the middle of the left and
    right sides, the top and bottom sides `cut` in from them."""
    if len(vs) != 6:
        return None
    top = sorted(v[0] for v in vs if abs(v[1] - r.y0) <= tol)
    if len(top) != 2:
        return None
    cut = (top[0] - r.x0 + r.x1 - top[1]) / 2
    if cut <= tol or cut >= r.w / 2 - tol:
        return None
    expected = [(r.x0, r.cy), (r.x0 + cut, r.y0), (r.x1 - cut, r.y0), (r.x1, r.cy), (r.x1 - cut, r.y1), (r.x0 + cut, r.y1)]
    return cut if _matches(vs, expected, tol) else None


def octagon_cut(vs: Sequence[Point], r: Rect, tol: float) -> float | None:
    """The corner cut (pt) of an octagon drawn as OOXML's: every corner cut as far along both
    sides (a chamfered rectangle whose two cuts differ is no octagon Slides draws)."""
    if len(vs) != 8:
        return None
    top = sorted(v[0] for v in vs if abs(v[1] - r.y0) <= tol)
    left = sorted(v[1] for v in vs if abs(v[0] - r.x0) <= tol)
    if len(top) != 2 or len(left) != 2:
        return None
    cut = (top[0] - r.x0 + r.x1 - top[1] + left[0] - r.y0 + r.y1 - left[1]) / 4
    if cut <= tol or cut >= min(r.w, r.h) / 2 - tol:
        return None
    x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
    expected = [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
                (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut)]
    return cut if _matches(vs, expected, tol) else None


def regular_sides(vs: Sequence[Point], tol: float) -> int | None:
    """The number of sides of a regular polygon standing on a flat side (TikZ's `regular
    polygon`), else None."""
    n = len(vs)
    if n < 5:
        return None
    cx, cy = sum(v[0] for v in vs) / n, sum(v[1] for v in vs) / n
    radii = [math.dist(v, (cx, cy)) for v in vs]
    mean = sum(radii) / n
    if mean <= 0 or max(radii) - min(radii) > 0.03 * mean:
        return None
    angles = sorted(math.atan2(v[1] - cy, v[0] - cx) for v in vs)
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 2 * math.pi - angles[-1]]
    if any(abs(g - 2 * math.pi / n) > 0.05 for g in gaps):
        return None
    low = sorted(v[1] for v in vs)[-2:]
    return n if low[1] - low[0] <= tol else None


def polygon_preset(points: Sequence[Sequence[float]], r: Rect) -> Preset | None:
    """A closed polygon as a hexagon, an octagon (a chamfered rectangle too) or a regular
    pentagon, heptagon, decagon or dodecagon."""
    tol = tolerance(r)
    vs = corners_of(points, tol)
    ss = min(r.w, r.h)
    cut = hexagon_cut(vs, r, tol)
    if cut is not None:
        return Preset(shape="HEXAGON", adjust=round(cut / ss, 3), radius=None)
    cut = octagon_cut(vs, r, tol)
    if cut is not None:
        return Preset(shape="OCTAGON", adjust=round(cut / ss, 3), radius=None)
    sides = regular_sides(vs, tol)
    if sides is not None and sides in REGULAR:
        return Preset(shape=REGULAR[sides], adjust=None, radius=None)
    return None


def _on_ellipse(p: Point, cx: float, cy: float, rx: float, ry: float) -> bool:
    return abs(math.hypot((p[0] - cx) / rx, (p[1] - cy) / ry) - 1) <= 0.08


def can_adjust(path: list[PathItem], r: Rect) -> float | None:
    """An upright cylinder as TikZ's `cylinder, shape border rotate=90` draws it (the bottom's
    front half, the right side, the top's back half, the left side, then the top's front half):
    its ellipse height as a share of the shorter side (CAN's adjustment). None for a cylinder
    lying down, or one whose top and bottom ellipses differ (no CAN draws that)."""
    if [op for op, _ in path] != list("cclcclcc"):
        return None
    tol = tolerance(r)
    if not chained(path[:6], tol) or math.dist(path[6][1][-1], path[7][1][0]) > tol:
        return None
    sides = [path[2][1], path[5][1]]
    if any(abs(a[0] - b[0]) > tol or min(abs(a[0] - r.x0), abs(a[0] - r.x1)) > tol for a, b in sides) \
            or abs(sides[0][0][0] - sides[1][0][0]) < r.w / 2:
        return None  # not the two sides of the box
    ys = [p[1] for a, b in sides for p in (a, b)]
    top, bottom = min(ys), max(ys)
    ry_top, ry_bottom = top - r.y0, r.y1 - bottom
    if ry_top <= tol or abs(ry_top - ry_bottom) > tol:
        return None  # flat, or lopsided
    ry, rx = (ry_top + ry_bottom) / 2, r.w / 2
    if 2 * ry > r.h / 2:
        return None  # deeper than CAN's adjustment goes
    # the bottom curves on the lower ellipse, below its centre; the top's on the upper one,
    # behind (above) its centre, and the rim in front (below) it
    for k, cy, below in ((0, bottom, True), (1, bottom, True), (3, top, False), (4, top, False), (6, top, True), (7, top, True)):
        pts = path[k][1]
        if not all(_on_ellipse(bezier(pts, t), r.cx, cy, rx, ry) for t in (0.0, 0.5, 1.0)):
            return None
        if (bezier(pts, 0.5)[1] > cy) != below:
            return None
    return round(2 * ry / min(r.w, r.h), 3)


def _turn(a: Point, b: Point) -> float:
    """The angle between two directions."""
    la, lb = math.hypot(*a), math.hypot(*b)
    if la == 0 or lb == 0:
        return 0.0
    return math.acos(max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (la * lb))))


def _tangent_out(pts: Sequence[Sequence[float]]) -> Point:
    """A curve's direction where it starts."""
    for c in pts[1:]:
        if math.dist(c, pts[0]) > 1e-6:
            return c[0] - pts[0][0], c[1] - pts[0][1]
    return 0.0, 0.0


def _tangent_in(pts: Sequence[Sequence[float]]) -> Point:
    """A curve's direction where it ends."""
    for c in reversed(pts[:-1]):
        if math.dist(c, pts[-1]) > 1e-6:
            return pts[-1][0] - c[0], pts[-1][1] - c[1]
    return 0.0, 0.0


def cloud_puffs(path: list[PathItem], r: Rect) -> int | None:
    """A cloud: one closed outline of curves going once around the box's centre, each bulging
    outwards, meeting in cusps (at least `CLOUD_PUFFS` of them). The number of puffs, or None (a
    smooth ellipse of many curves has no cusp)."""
    if len(path) < 2 * CLOUD_PUFFS or any(op != "c" for op, _ in path):
        return None
    if not chained(path, tolerance(r)) or r.w <= 0 or r.h <= 0:
        return None
    cx, cy, rx, ry = r.cx, r.cy, r.w / 2, r.h / 2

    def reach(p: Point) -> float:
        return math.hypot((p[0] - cx) / rx, (p[1] - cy) / ry)

    def angle(p: Sequence[float]) -> float:
        return math.atan2((p[1] - cy) / ry, (p[0] - cx) / rx)
    ends = [pts[-1] for _, pts in path]
    steps = [(angle(ends[k]) - angle(ends[k - 1]) + math.pi) % (2 * math.pi) - math.pi for k in range(len(ends))]
    if not (all(s > 0 for s in steps) or all(s < 0 for s in steps)) or abs(abs(sum(steps)) - 2 * math.pi) > 0.1:
        return None  # not once around the centre
    for _, pts in path:
        start, end = (pts[0][0], pts[0][1]), (pts[-1][0], pts[-1][1])
        if reach(start) < 0.55 or reach(bezier(pts, 0.5)) <= reach(((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)):
            return None  # a dent, or a curve bulging in
    cusps = sum(_turn(_tangent_in(path[k - 1][1]), _tangent_out(path[k][1])) > CUSP for k in range(len(path)))
    return cusps if cusps >= CLOUD_PUFFS else None


def punched_tape(path: list[PathItem], r: Rect) -> bool:
    """TikZ's `tape` with its default bends as flowChartPunchedTape draws it: straight left and
    right sides, a wave a tenth of the height from the top and from the bottom, each a tenth of
    the height deep, its top dipping in on the left and its bottom on the right."""
    ops = [op for op, _ in path]
    if ops.count("c") != 4 or not set(ops) <= {"c", "l"}:
        return False
    tol = tolerance(r)
    if not chained(path, tol):
        return False
    for op, pts in path:
        if op == "l" and math.dist(pts[0], pts[-1]) > tol and \
                (abs(pts[0][0] - pts[-1][0]) > tol or min(abs(pts[0][0] - r.x0), abs(pts[0][0] - r.x1)) > tol):
            return False  # a line that is not one of the sides
    curves = [pts for op, pts in path if op == "c"]
    top = [pts for pts in curves if bezier(pts, 0.5)[1] < r.cy]
    bottom = [pts for pts in curves if bezier(pts, 0.5)[1] >= r.cy]
    if len(top) != 2 or len(bottom) != 2:
        return False
    depth = TAPE_BEND * r.h
    room = TAPE_ROOM * r.h
    for side, edge, inward_left in ((top, r.y0 + depth, True), (bottom, r.y1 - depth, False)):
        for pts in side:
            if any(abs(p[1] - edge) > room for p in (pts[0], pts[-1])):
                return False  # its ends not a tenth in from the edge
            left = bezier(pts, 0.5)[0] < r.cx
            bulge = bezier(pts, 0.5)[1] - (pts[0][1] + pts[-1][1]) / 2  # (down is +)
            inward = bulge > 0 if side is top else bulge < 0
            if abs(abs(bulge) - depth) > room or inward != (left == inward_left):
                return False
    return True


def stadium_radius(path: list[PathItem], r: Rect) -> float | None:
    """A pill (TikZ's `rounded rectangle`): two straight sides and two half circles, its radius
    half the shorter side - ROUND_RECTANGLE at its largest rounding."""
    ops = [op for op, _ in path]
    if ops.count("c") != 4 or not set(ops) <= {"c", "l"}:
        return None
    tol = tolerance(r)
    if not chained(path, tol):
        return None
    radius = min(r.w, r.h) / 2
    wide = r.w >= r.h
    caps = [(r.x0 + radius, r.cy), (r.x1 - radius, r.cy)] if wide else [(r.cx, r.y0 + radius), (r.cx, r.y1 - radius)]
    for op, pts in path:
        if op == "l":
            if math.dist(pts[0], pts[-1]) <= tol:
                continue
            on = [(p[1] - r.y0, p[1] - r.y1) if wide else (p[0] - r.x0, p[0] - r.x1) for p in (pts[0], pts[-1])]
            if any(min(abs(a), abs(b)) > tol for a, b in on):
                return None  # a straight piece off the long sides
        elif any(abs(min(math.dist(bezier(pts, t), c) for c in caps) - radius) > tol for t in (0.0, 0.25, 0.5, 0.75, 1.0)):
            return None  # a curve off the half circles
    return round(radius, 2)


def preset_shape(path: list[PathItem], r: Rect, filled: bool) -> Preset | None:
    """The Slides preset a drawing's path is, beyond the plain shapes `diagram_from` reads itself."""
    ops = "".join(op for op, _ in path)
    tol = tolerance(r)
    if ops == "cclcclcc":
        adjust = can_adjust(path, r)
        return Preset(shape="CAN", adjust=adjust, radius=None) if adjust is not None else None
    if set(ops) == {"c"}:
        return Preset(shape="CLOUD", adjust=None, radius=None) if cloud_puffs(path, r) is not None else None
    if set(ops) == {"l"}:
        points = [p for _, pts in path for p in pts]
        if not filled and math.dist(points[0], points[-1]) > tol:
            return None  # an open polyline
        return polygon_preset(points, r)
    if set(ops) == {"c", "l"}:
        radius = stadium_radius(path, r)
        if radius is not None:
            return Preset(shape="ROUND_RECTANGLE", adjust=None, radius=radius)
        if punched_tape(path, r):
            return Preset(shape="FLOW_CHART_PUNCHED_TAPE", adjust=None, radius=None)
    return None


def turned_angle(path: list[PathItem], r: Rect) -> float | None:
    """How far (degrees, counterclockwise as the page shows it) an ellipse or a rectangle is
    turned (TikZ's `rotate=30`), or None for one standing upright or a path that is neither. A
    node is not turned yet (it would take a rotation in the IR, a turned transform in emit, its
    label measured along it and sync reading a turned box back): `diagram_from` says so rather
    than calling it an unknown shape."""
    tol = tolerance(r)
    ops = "".join(op for op, _ in path)
    if ops == "cccc" and chained(path, tol):
        # TikZ draws an ellipse as four quarters from the ends of its axes
        joins = [(pts[0][0], pts[0][1]) for _, pts in path]
        a = (joins[0][0] - joins[2][0], joins[0][1] - joins[2][1])
        b = (joins[1][0] - joins[3][0], joins[1][1] - joins[3][1])
        la, lb = math.hypot(*a), math.hypot(*b)
        centre = ((joins[0][0] + joins[2][0]) / 2, (joins[0][1] + joins[2][1]) / 2)
        if la <= tol or lb <= tol or math.dist(centre, ((joins[1][0] + joins[3][0]) / 2, (joins[1][1] + joins[3][1]) / 2)) > tol \
                or abs(a[0] * b[0] + a[1] * b[1]) > 0.05 * la * lb:
            return None
        ux, uy = a[0] / la, a[1] / la
        for _, pts in path:
            px, py = bezier(pts, 0.5)
            x, y = (px - centre[0]) * ux + (py - centre[1]) * uy, (py - centre[1]) * ux - (px - centre[0]) * uy
            if abs(math.hypot(x / (la / 2), y / (lb / 2)) - 1) > 0.08:
                return None  # not an ellipse
        major = a if la >= lb else b
        angle = -math.degrees(math.atan2(major[1], major[0]))
        angle = (angle + 90) % 180 - 90
        return angle if min(abs(angle), 90 - abs(angle)) > 1 else None
    if set(ops) == {"l"}:
        vs = corners_of([p for _, pts in path for p in pts], tol)
        if len(vs) != 4:
            return None
        sides = [(vs[(k + 1) % 4][0] - vs[k][0], vs[(k + 1) % 4][1] - vs[k][1]) for k in range(4)]
        lengths = [math.hypot(*s) for s in sides]
        if min(lengths) <= tol or any(abs(lengths[k] - lengths[k + 2]) > tol for k in range(2)) \
                or any(abs(s[0] * t[0] + s[1] * t[1]) > 0.05 * math.hypot(*s) * math.hypot(*t) for s, t in zip(sides, sides[1:])):
            return None  # no rectangle
        angle = (-math.degrees(math.atan2(sides[0][1], sides[0][0])) + 45) % 90 - 45
        return angle if abs(angle) > 1 else None
    return None


def split_rectangle(path: list[PathItem], r: Rect) -> list[Rect] | None:
    """A rectangle split into parts by lines across it (TikZ's `rectangle split`, a UML class):
    the parts, top to bottom (or left to right), else None."""
    if len(path) < 2 or path[0][0] != "re" or any(op != "l" for op, _ in path[1:]):
        return None
    tol = tolerance(r)
    (ax, ay), (bx, by) = path[0][1][0], path[0][1][-1]
    if abs(min(ax, bx) - r.x0) > tol or abs(max(ax, bx) - r.x1) > tol or abs(min(ay, by) - r.y0) > tol \
            or abs(max(ay, by) - r.y1) > tol:
        return None
    across: list[float] = []
    down: list[float] = []
    for _, pts in path[1:]:
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        if abs(y0 - y1) <= tol and abs(min(x0, x1) - r.x0) <= tol and abs(max(x0, x1) - r.x1) <= tol \
                and r.y0 + tol < y0 < r.y1 - tol:
            across.append((y0 + y1) / 2)
        elif abs(x0 - x1) <= tol and abs(min(y0, y1) - r.y0) <= tol and abs(max(y0, y1) - r.y1) <= tol \
                and r.x0 + tol < x0 < r.x1 - tol:
            down.append((x0 + x1) / 2)
        else:
            return None  # a line that splits nothing
    if across and down:
        return None  # a grid: a table's
    cuts = sorted(across or down)
    if any(b - a <= tol for a, b in zip(cuts, cuts[1:])):
        return None
    if across:
        edges = [r.y0] + cuts + [r.y1]
        return [Rect(r.x0, a, r.x1, b) for a, b in zip(edges, edges[1:])]
    edges = [r.x0] + cuts + [r.x1]
    return [Rect(a, r.y0, b, r.y1) for a, b in zip(edges, edges[1:])]
