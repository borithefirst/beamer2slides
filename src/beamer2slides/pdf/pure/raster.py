"""PDFium's path rasteriser (third_party/agg23 as PDFium patched it), ported value for value.

Anti-Grain Geometry 2.3 - Copyright (C) 2002-2005 Maxim Shemanarev (http://www.antigrain.com),
under its permissive licence; the scanline algorithm is David Turner's (FreeType). This is a
translation of PDFium's copy, float32 arithmetic and integer quirks included, because the point
is to produce the same pixels PDFium does: `rasterize` takes vertices in device space and gives
back the coverage AGG would hand the renderer, `stroke` / `dash` generate the outline AGG fills
for a stroked path, `build_path` is CFX_AggDeviceDriver's BuildAggPath with its curve flattener.

Every float operation is rounded to float32 (`F`), one C operation at a time, in the order the
C++ evaluates it; integer divisions follow C's truncation."""

from __future__ import annotations

import math

import numpy as np

from .syntax import F32X1, F32X2, F32X4, F32X6, F32X8
from .syntax import float32 as F

_p1, _u1 = F32X1.pack, F32X1.unpack
_p2, _u2, _p4, _u4, _p6, _u6 = F32X2.pack, F32X2.unpack, F32X4.pack, F32X4.unpack, F32X6.pack, F32X6.unpack
_p8, _u8 = F32X8.pack, F32X8.unpack

# path commands (agg_basics.h)
STOP, MOVE_TO, LINE_TO, CURVE4 = 0, 1, 2, 4
END_POLY = 0x0F
FLAG_CCW, FLAG_CW, FLAG_CLOSE = 0x10, 0x20, 0x40
CLOSE = END_POLY | FLAG_CLOSE

# CFX_Path point types, as content.py stores them
PT_MOVE, PT_LINE, PT_BEZIER = 2, 0, 1

# line caps / joins (agg_math_stroke.h)
BUTT_CAP, SQUARE_CAP, ROUND_CAP = 0, 1, 2
MITER_JOIN, MITER_JOIN_REVERT, ROUND_JOIN, BEVEL_JOIN, MITER_JOIN_ROUND = 0, 1, 2, 3, 4

PI = F(math.pi)                 # FXSYS_PI: 3.1415926535897932384626433832795f
TWO_PI = F(2 * PI)
MAX_POS = 32000.0               # HardClip
EPS30 = F(1e-30)
VERTEX_DIST_EPS = F(1e-14)
STROKE_THETA = F(1.0 / 1000.0)
INNER_MITER_LIMIT = F(1.0 + F(1.0 / 100))
TOL_SQUARE = 0.25
TOL_MANHATTAN = 4.0


def is_vertex(c: int) -> bool:
    c &= ~0x80
    return MOVE_TO <= c < END_POLY


def is_close(c: int) -> bool:
    c &= ~0x80
    return (c & ~(FLAG_CW | FLAG_CCW)) == CLOSE


def is_end_poly(c: int) -> bool:
    return (c & ~0x80 & 0x0F) == END_POLY


# ---------------------------------------------------------------------- CFX_Matrix


def transform(m, x, y):
    """CFX_Matrix::Transform, in float."""
    a, b, c, d, e, f = m
    try:        # the same roundings, several per C call (syntax.F32X*)
        p = _u4(_p4(a * x, c * y, b * x, d * y))
        s = _u2(_p2(p[0] + p[1], p[2] + p[3]))
        return _u2(_p2(s[0] + e, s[1] + f))
    except OverflowError:
        return F(F(F(a * x) + F(c * y)) + e), F(F(F(b * x) + F(d * y)) + f)


def concat(m, n):
    """m * n (CFX_Matrix::operator*: m first, then n)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, G = n
    try:
        p = _u6(_p6(a * A, b * C, a * B, b * D, c * A, d * C))
        q = _u6(_p6(c * B, d * D, e * A, f * C, e * B, f * D))
        s = _u6(_p6(p[0] + p[1], p[2] + p[3], p[4] + p[5], q[0] + q[1], q[2] + q[3], q[4] + q[5]))
        t = _u2(_p2(s[4] + E, s[5] + G))
        return s[0], s[1], s[2], s[3], t[0], t[1]
    except OverflowError:
        return (F(F(a * A) + F(b * C)), F(F(a * B) + F(b * D)),
                F(F(c * A) + F(d * C)), F(F(c * B) + F(d * D)),
                F(F(F(e * A) + F(f * C)) + E), F(F(F(e * B) + F(f * D)) + G))


def inverse(m):
    a, b, c, d, e, f = m
    i = F(F(a * d) - F(b * c))
    if i == 0:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    j = -i
    return (F(d / i), F(b / j), F(c / j), F(a / i),
            F(F(F(c * f) - F(d * e)) / i), F(F(F(a * f) - F(b * e)) / j))


def _hypotf(x, y):
    return F(math.hypot(x, y))


def x_unit(m):
    a, b = m[0], m[1]
    if b == 0:
        return abs(a)
    if a == 0:
        return abs(b)
    return _hypotf(a, b)


def y_unit(m):
    c, d = m[2], m[3]
    if c == 0:
        return abs(d)
    if d == 0:
        return abs(c)
    return _hypotf(c, d)


def transform_rect(m, rect):
    """CFX_Matrix::TransformRect of (left, bottom, right, top)."""
    l, b, r, t = rect
    pts = [transform(m, l, t), transform(m, l, b), transform(m, r, t), transform(m, r, b)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def is_identity(m) -> bool:
    return m == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def outer_rect(rect):
    """CFX_FloatRect::GetOuterRect -> FX_RECT (left, top, right, bottom), y down."""
    l, b, r, t = rect
    x0, x1 = _sat(math.floor(l)), _sat(math.ceil(r))
    y0, y1 = _sat(math.floor(b)), _sat(math.ceil(t))
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _sat(v) -> int:
    if v != v:
        return 0
    return int(max(-2147483648, min(2147483647, v)))


def rect_intersect(a, b):
    """FX_RECT::Intersect."""
    l, t, r, bt = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if l > r or t > bt:
        return 0, 0, 0, 0
    return l, t, r, bt


def rect_empty(r) -> bool:
    return r[2] <= r[0] or r[3] <= r[1]


# ---------------------------------------------------------------------- CFX_Path helpers


def _pre_rect(points) -> bool:
    """IsRectPreTransform."""
    n = len(points)
    if n not in (4, 5):
        return False
    if n == 5 and (points[0][0], points[0][1]) != (points[4][0], points[4][1]):
        return False
    if (points[0][0], points[0][1]) == (points[2][0], points[2][1]) or \
            (points[1][0], points[1][1]) == (points[3][0], points[3][1]):
        return False
    return all(p[2] == PT_LINE for p in points[1:])


def _both_differ(p, q) -> bool:
    return p[0] != q[0] and p[1] != q[1]


def _normalized(points):
    """GetNormalizedPoints (only called with more than 5 points)."""
    if (points[0][0], points[0][1]) != (points[-1][0], points[-1][1]):
        return []
    out = [points[0]]
    n = len(points)
    for k in range(1, n):
        if len(out) + (n - k) == 5:
            out.extend(points[k:])
            break
        p = points[k]
        last = out[-1]
        if p[2] == PT_LINE and not p[3] and not last[3] and (p[0], p[1]) == (last[0], last[1]):
            continue
        out.append(p)
        if len(out) > 5:
            return []
    return out


def path_is_rect(points) -> bool:
    """CFX_Path::IsRect."""
    pts = _normalized(points) if len(points) > 5 else points
    if not _pre_rect(pts):
        return False
    for i in range(1, 4):
        if _both_differ(pts[i], pts[i - 1]):
            return False
    return not _both_differ(pts[0], pts[3])


def path_get_rect(points, matrix=None):
    """CFX_Path::GetRect: the (normalised) float rectangle, or None."""
    pts = _normalized(points) if len(points) > 5 else points
    if matrix is None:
        if not path_is_rect(pts):
            return None
        p0, p2 = pts[0], pts[2]
    else:
        if not _pre_rect(pts):
            return None
        tp = []
        for i, p in enumerate(pts):
            q = transform(matrix, p[0], p[1])
            if i and _both_differ(q, tp[-1]):
                return None
            tp.append(q)
        if _both_differ(tp[0], tp[3]):
            return None
        p0, p2 = tp[0], tp[2]
    return min(p0[0], p2[0]), min(p0[1], p2[1]), max(p0[0], p2[0]), max(p0[1], p2[1])


def rect_path(l, b, r, t):
    """CFX_Path::AppendRect."""
    return [(l, b, PT_MOVE, False), (l, t, PT_LINE, False), (r, t, PT_LINE, False),
            (r, b, PT_LINE, False), (l, b, PT_LINE, True)]


# ---------------------------------------------------------------------- BuildAggPath


def _hard(v):
    return -MAX_POS if v < -MAX_POS else (MAX_POS if v > MAX_POS else v)


def build_path(points, matrix=None) -> list:
    """BuildAggPath: CFX_Path points -> path_storage vertices [(x, y, cmd)]."""
    out: list = []
    n = len(points)
    i = 0
    while i < n:
        x, y, kind, _ = points[i]
        if matrix is not None:
            x, y = transform(matrix, x, y)
        x, y = _hard(x), _hard(y)
        if kind == PT_MOVE:
            out.append((x, y, MOVE_TO))
        elif kind == PT_LINE:
            if i > 0 and points[i - 1][2] == PT_MOVE and not points[i - 1][3] and \
                    (i + 1 == n or (points[i + 1][2] == PT_MOVE and not points[i + 1][3])) and \
                    (points[i][0], points[i][1]) == (points[i - 1][0], points[i - 1][1]):
                x = F(x + 1)
            out.append((x, y, LINE_TO))
        elif kind == PT_BEZIER:
            if i > 0 and i + 2 < n:
                p0 = points[i - 1]
                p2 = points[i + 1]
                p3 = points[i + 2]
                x0, y0, x2, y2, x3, y3 = p0[0], p0[1], p2[0], p2[1], p3[0], p3[1]
                if matrix is not None:
                    x0, y0 = transform(matrix, x0, y0)
                    x2, y2 = transform(matrix, x2, y2)
                    x3, y3 = transform(matrix, x3, y3)
                curve = curve4(_hard(x0), _hard(y0), x, y, _hard(x2), _hard(y2), _hard(x3), _hard(y3))
                i += 2
                first = True
                for cx, cy in curve:
                    # add_path(curve) with solid_path: a move becomes a line on a non-empty path
                    out.append((cx, cy, LINE_TO if (not first or out) else MOVE_TO))
                    first = False
        if points[i][3]:
            end_poly(out)
        i += 1
    return out


def end_poly(out: list) -> None:
    if out and is_vertex(out[-1][2]):
        out.append((0.0, 0.0, CLOSE))


def curve4(x1, y1, x2, y2, x3, y3, x4, y4) -> list:
    """curve4_div: the flattened points, start and end included."""
    pts = [(x1, y1)]
    try:
        _bezier_fast(pts, x1, y1, x2, y2, x3, y3, x4, y4, 0)
    except OverflowError:
        pts = [(x1, y1)]
        _bezier(pts, x1, y1, x2, y2, x3, y3, x4, y4, 0)
    pts.append((x4, y4))
    return pts


def _bezier_fast(pts, x1, y1, x2, y2, x3, y3, x4, y4, level):
    """`_bezier` with its roundings done several per C call: the same operations on the same
    values (raises OverflowError where `_bezier` would meet an infinity)."""
    if level > 16:
        return
    a = _u6(_p6(x1 + x2, y1 + y2, x2 + x3, y2 + y3, x3 + x4, y3 + y4))
    x12, y12, x23, y23, x34, y34 = _u6(_p6(a[0] / 2, a[1] / 2, a[2] / 2, a[3] / 2, a[4] / 2, a[5] / 2))
    b = _u4(_p4(x12 + x23, y12 + y23, x23 + x34, y23 + y34))
    x123, y123, x234, y234 = _u4(_p4(b[0] / 2, b[1] / 2, b[2] / 2, b[3] / 2))
    c = _u4(_p4(x123 + x234, y123 + y234, x4 - x1, y4 - y1))
    x1234, y1234 = _u2(_p2(c[0] / 2, c[1] / 2))
    dx, dy = c[2], c[3]
    e = _u4(_p4(x2 - x4, y2 - y4, x3 - x4, y3 - y4))
    f = _u4(_p4(e[0] * dy, e[1] * dx, e[2] * dy, e[3] * dx))
    d2, d3 = _u2(_p2(f[0] - f[1], f[2] - f[3]))
    d2, d3 = abs(d2), abs(d3)
    if d2 > EPS30:
        if d3 > EPS30:
            s = F(d2 + d3)
            d = F(s * s)
        else:
            d = F(d2 * d2)
        if d <= F(TOL_SQUARE * F(F(dx * dx) + F(dy * dy))):
            pts.append((x23, y23))
            return
    elif d3 > EPS30:
        d = F(d3 * d3)
        if d <= F(TOL_SQUARE * F(F(dx * dx) + F(dy * dy))):
            pts.append((x23, y23))
            return
    else:
        g = _u4(_p4(x1 + x3, y1 + y3, x2 + x4, y2 + y4))
        g = _u4(_p4(g[0] - x2, g[1] - y2, g[2] - x3, g[3] - y3))
        g = _u4(_p4(g[0] - x2, g[1] - y2, g[2] - x3, g[3] - y3))
        s = F(abs(g[0]) + abs(g[1]))
        s = F(s + abs(g[2]))
        s = F(s + abs(g[3]))
        if s <= TOL_MANHATTAN:
            pts.append((x1234, y1234))
            return
    _bezier_fast(pts, x1, y1, x12, y12, x123, y123, x1234, y1234, level + 1)
    _bezier_fast(pts, x1234, y1234, x234, y234, x34, y34, x4, y4, level + 1)


def _bezier(pts, x1, y1, x2, y2, x3, y3, x4, y4, level):
    if level > 16:
        return
    x12 = F(F(x1 + x2) / 2)
    y12 = F(F(y1 + y2) / 2)
    x23 = F(F(x2 + x3) / 2)
    y23 = F(F(y2 + y3) / 2)
    x34 = F(F(x3 + x4) / 2)
    y34 = F(F(y3 + y4) / 2)
    x123 = F(F(x12 + x23) / 2)
    y123 = F(F(y12 + y23) / 2)
    x234 = F(F(x23 + x34) / 2)
    y234 = F(F(y23 + y34) / 2)
    x1234 = F(F(x123 + x234) / 2)
    y1234 = F(F(y123 + y234) / 2)
    dx = F(x4 - x1)
    dy = F(y4 - y1)
    d2 = abs(F(F(F(x2 - x4) * dy) - F(F(y2 - y4) * dx)))
    d3 = abs(F(F(F(x3 - x4) * dy) - F(F(y3 - y4) * dx)))
    case = (int(d2 > EPS30) << 1) + int(d3 > EPS30)
    if case == 0:
        s = F(abs(F(F(F(x1 + x3) - x2) - x2)) + abs(F(F(F(y1 + y3) - y2) - y2)))
        s = F(s + abs(F(F(F(x2 + x4) - x3) - x3)))
        s = F(s + abs(F(F(F(y2 + y4) - y3) - y3)))
        if s <= TOL_MANHATTAN:
            pts.append((x1234, y1234))
            return
    else:
        if case == 1:
            d = F(d3 * d3)
        elif case == 2:
            d = F(d2 * d2)
        else:
            s = F(d2 + d3)
            d = F(s * s)
        if d <= F(TOL_SQUARE * F(F(dx * dx) + F(dy * dy))):
            pts.append((x23, y23))
            return
    _bezier(pts, x1, y1, x12, y12, x123, y123, x1234, y1234, level + 1)
    _bezier(pts, x1234, y1234, x234, y234, x34, y34, x4, y4, level + 1)


# ---------------------------------------------------------------------- stroke math


def _dist(x1, y1, x2, y2):
    try:        # the same roundings, several per C call (syntax.F32X*); the scalar form below
        dx, dy = _u2(_p2(x2 - x1, y2 - y1))
        a, b = _u2(_p2(dx * dx, dy * dy))
        return _u1(_p1(math.sqrt(_u1(_p1(a + b))[0])))[0]
    except OverflowError:
        dx = F(x2 - x1)
        dy = F(y2 - y1)
        return F(math.sqrt(F(F(dx * dx) + F(dy * dy))))


class _Seq(list):
    """vertex_sequence of [x, y, dist] (or [x, y, dist, cmd]) lists."""

    @staticmethod
    def _ok(v, w) -> bool:
        v[2] = _dist(v[0], v[1], w[0], w[1])
        return v[2] > VERTEX_DIST_EPS

    def add(self, val) -> None:
        if len(self) > 1 and not self._ok(self[-2], self[-1]):
            self.pop()
        self.append(val)

    def modify_last(self, val) -> None:
        if self:
            self.pop()
        self.add(val)

    def close(self, closed: bool) -> None:
        while len(self) > 1:
            if self._ok(self[-2], self[-1]):
                break
            t = self.pop()
            self.modify_last(t)
        if closed:
            while len(self) > 1:
                if self._ok(self[-1], self[0]):
                    break
                self.pop()


def _acos_da(width, approx):
    return F(F(math.acos(F(width / F(width + F(0.125 / approx))))) * 2)


def _arc_point(x, y, width, a1):
    """`F(x + F(width * F(cos(a1))))`, `F(y + F(width * F(sin(a1))))`, batched (syntax.F32X*)."""
    try:
        c, s = _u2(_p2(math.cos(a1), math.sin(a1)))
        wc, ws = _u2(_p2(width * c, width * s))
        return _u2(_p2(x + wc, y + ws))
    except OverflowError:
        return F(x + F(width * F(math.cos(a1)))), F(y + F(width * F(math.sin(a1))))


def calc_arc(out, x, y, dx1, dy1, dx2, dy2, width, approx):
    a1 = F(math.atan2(dy1, dx1))
    a2 = F(math.atan2(dy2, dx2))
    da = F(a1 - a2)
    ccw = 0 < da < PI
    if width < 0:
        width = -width
    da = _acos_da(width, approx)
    out.append((F(x + dx1), F(y + dy1)))
    if da > 0:
        if not ccw:
            if a1 > a2:
                a2 = F(a2 + TWO_PI)
            a2 = F(a2 - F(da / 4))
            a1 = F(a1 + da)
            while a1 < a2:
                out.append(_arc_point(x, y, width, a1))
                a1 = F(a1 + da)
        else:
            if a1 < a2:
                a2 = F(a2 - TWO_PI)
            a2 = F(a2 + F(da / 4))
            a1 = F(a1 - da)
            while a1 > a2:
                out.append(_arc_point(x, y, width, a1))
                a1 = F(a1 - da)
    out.append((F(x + dx2), F(y + dy2)))


def _intersection(ax, ay, bx, by, cx, cy, dx, dy):
    try:        # the same roundings, several per C call (syntax.F32X*); the scalar form below.
        # The differences each appear twice in the C (num and den, den and the result): one
        # rounding of one expression, so computing them once is the same value.
        d = _u6(_p6(ay - cy, dx - cx, ax - cx, dy - cy, bx - ax, by - ay))
        m = _u4(_p4(d[0] * d[1], d[2] * d[3], d[4] * d[3], d[5] * d[1]))
        num, den = _u2(_p2(m[0] - m[1], m[2] - m[3]))
        if abs(den) < EPS30:
            return None
        t = _u2(_p2(d[4] * num, d[5] * num))
        q = _u2(_p2(t[0] / den, t[1] / den))
        return _u2(_p2(ax + q[0], ay + q[1]))
    except OverflowError:
        pass
    num = F(F(F(ay - cy) * F(dx - cx)) - F(F(ax - cx) * F(dy - cy)))
    den = F(F(F(bx - ax) * F(dy - cy)) - F(F(by - ay) * F(dx - cx)))
    if abs(den) < EPS30:
        return None
    return F(ax + F(F(F(bx - ax) * num) / den)), F(ay + F(F(F(by - ay) * num) / den))


def calc_miter(out, v0, v1, v2, dx1, dy1, dx2, dy2, width, join, limit, approx):
    exceeded = True
    # the two offset segments, (v0, v1) shifted by d1 and (v1, v2) by d2: the eight roundings
    # the C writes out one by one, in one C call (syntax.F32X*)
    try:
        a0x, a0y, a1x, a1y, b1x, b1y, b2x, b2y = _u8(_p8(
            v0[0] + dx1, v0[1] - dy1, v1[0] + dx1, v1[1] - dy1,
            v1[0] + dx2, v1[1] - dy2, v2[0] + dx2, v2[1] - dy2))
    except OverflowError:
        a0x, a0y, a1x, a1y = F(v0[0] + dx1), F(v0[1] - dy1), F(v1[0] + dx1), F(v1[1] - dy1)
        b1x, b1y, b2x, b2y = F(v1[0] + dx2), F(v1[1] - dy2), F(v2[0] + dx2), F(v2[1] - dy2)
    hit = _intersection(a0x, a0y, a1x, a1y, b1x, b1y, b2x, b2y)
    if hit is not None:
        xi, yi = hit
        d1 = _dist(v1[0], v1[1], xi, yi)
        if d1 <= F(width * limit):
            out.append((xi, yi))
            exceeded = False
    else:
        x2, y2 = a1x, a1y
        try:
            e = _u4(_p4(x2 - v0[0], v0[1] - y2, x2 - v2[0], v2[1] - y2))
            p = _u4(_p4(e[0] * dy1, e[1] * dx1, e[2] * dy1, e[3] * dx1))
            s1, s2 = _u2(_p2(p[0] - p[1], p[2] - p[3]))
        except OverflowError:
            s1 = F(F(F(x2 - v0[0]) * dy1) - F(F(v0[1] - y2) * dx1))
            s2 = F(F(F(x2 - v2[0]) * dy1) - F(F(v2[1] - y2) * dx1))
        if (s1 < 0) != (s2 < 0):
            out.append((a1x, a1y))
            exceeded = False
    if exceeded:
        if join == MITER_JOIN_REVERT:
            out.append((a1x, a1y))
            out.append((b1x, b1y))
        elif join == MITER_JOIN_ROUND:
            calc_arc(out, v1[0], v1[1], dx1, -dy1, dx2, -dy2, width, approx)
        else:
            out.append((F(F(v1[0] + dx1) + F(dy1 * limit)), F(F(v1[1] - dy1) + F(dx1 * limit))))
            out.append((F(F(v1[0] + dx2) - F(dy2 * limit)), F(F(v1[1] - dy2) - F(dx2 * limit))))


def calc_cap(out, v0, v1, length, cap, width, approx):
    out.clear()
    try:        # the same roundings, several per C call (syntax.F32X*)
        d = _u2(_p2(v1[1] - v0[1], v1[0] - v0[0]))
        d = _u2(_p2(d[0] / length, d[1] / length))
        dx1, dy1 = _u2(_p2(d[0] * width, d[1] * width))
    except OverflowError:
        dx1 = F(F(v1[1] - v0[1]) / length)
        dy1 = F(F(v1[0] - v0[0]) / length)
        dx1 = F(dx1 * width)
        dy1 = F(dy1 * width)
    dx2 = dy2 = 0.0
    if cap != ROUND_CAP:
        if cap == SQUARE_CAP:
            dx2, dy2 = dy1, dx1
        out.append((F(F(v0[0] - dx1) - dx2), F(F(v0[1] + dy1) - dy2)))
        out.append((F(F(v0[0] + dx1) - dx2), F(F(v0[1] - dy1) - dy2)))
    else:
        a1 = F(math.atan2(dy1, -dx1))
        a2 = F(a1 + PI)
        da = _acos_da(width, approx)
        if da < STROKE_THETA:
            da = STROKE_THETA
        out.append((F(v0[0] - dx1), F(v0[1] + dy1)))
        a1 = F(a1 + da)
        a2 = F(a2 - F(da / 4))
        while a1 < a2:
            out.append(_arc_point(v0[0], v0[1], width, a1))
            a1 = F(a1 + da)
        out.append((F(v0[0] + dx1), F(v0[1] - dy1)))


def calc_join(out, v0, v1, v2, len1, len2, width, join, miter_limit, approx):
    try:        # the same roundings, several per C call (syntax.F32X*); the four differences
        # are one rounded expression each, whether the C writes them for the d's or for `loc`
        e = _u4(_p4(v1[1] - v0[1], v1[0] - v0[0], v2[1] - v1[1], v2[0] - v1[0]))
        w = _u4(_p4(width * e[0], width * e[1], width * e[2], width * e[3]))
        dx1, dy1, dx2, dy2 = _u4(_p4(w[0] / len1, w[1] / len1, w[2] / len2, w[3] / len2))
        c = _u2(_p2(e[3] * e[0], e[2] * e[1]))
        loc = F(c[0] - c[1])
    except OverflowError:
        dx1 = F(F(width * F(v1[1] - v0[1])) / len1)
        dy1 = F(F(width * F(v1[0] - v0[0])) / len1)
        dx2 = F(F(width * F(v2[1] - v1[1])) / len2)
        dy2 = F(F(width * F(v2[0] - v1[0])) / len2)
        loc = F(F(F(v2[0] - v1[0]) * F(v1[1] - v0[1])) - F(F(v2[1] - v1[1]) * F(v1[0] - v0[0])))
    out.clear()
    if loc > 0:
        # inner join: inner_miter
        calc_miter(out, v0, v1, v2, dx1, dy1, dx2, dy2, width, MITER_JOIN_REVERT, INNER_MITER_LIMIT, 1.0)
    elif join in (MITER_JOIN, MITER_JOIN_REVERT, MITER_JOIN_ROUND):
        calc_miter(out, v0, v1, v2, dx1, dy1, dx2, dy2, width, join, miter_limit, approx)
    elif join == ROUND_JOIN:
        calc_arc(out, v1[0], v1[1], dx1, -dy1, dx2, -dy2, width, approx)
    else:
        out.append((F(v1[0] + dx1), F(v1[1] - dy1)))
        out.append((F(v1[0] + dx2), F(v1[1] - dy2)))


# ---------------------------------------------------------------------- generators


class StrokeGen:
    """vcgen_stroke."""

    def __init__(self, width, cap, join, miter_limit):
        self.width = F(width / 2)
        self.cap = cap
        self.join = join
        self.miter = miter_limit
        self.approx = 1.0
        self.src = _Seq()
        self.closed = 0

    def remove_all(self):
        self.src = _Seq()
        self.closed = 0

    def add_vertex(self, x, y, cmd):
        if cmd & ~0x80 == MOVE_TO:
            self.src.modify_last([x, y, 0.0, cmd])
        elif is_vertex(cmd):
            self.src.add([x, y, 0.0, cmd])
        else:
            self.closed = cmd & FLAG_CLOSE

    def generate(self):
        """rewind + vertex() until stop, as (x, y, cmd)."""
        src = self.src
        src.close(self.closed != 0)
        closed = self.closed
        if len(src) < 3:
            closed = 0
        n = len(src)
        if n < 2 + (1 if closed else 0):
            return []
        w, approx = self.width, self.approx
        res: list = []
        tmp: list = []

        def emit(first_cmd):
            for k, (x, y) in enumerate(tmp):
                res.append((x, y, first_cmd if k == 0 else LINE_TO))

        def join(i_prev, i_curr, i_next, len1, len2):
            calc_join(tmp, src[i_prev], src[i_curr], src[i_next], len1, len2, w, self.join, self.miter, approx)

        pending = MOVE_TO
        if not closed:
            calc_cap(tmp, src[0], src[1], src[0][2], self.cap, w, approx)
            emit(pending)
            pending = LINE_TO
            for i in range(1, n - 1):
                join((i + n - 1) % n, i, (i + 1) % n, src[(i + n - 1) % n][2], src[i][2])
                emit(pending)
            calc_cap(tmp, src[n - 1], src[n - 2], src[n - 2][2], self.cap, w, approx)
            emit(pending)
            for i in range(n - 2, 0, -1):
                join((i + 1) % n, i, (i + n - 1) % n, src[i][2], src[(i + n - 1) % n][2])
                emit(pending)
            res.append((0.0, 0.0, END_POLY | FLAG_CLOSE | FLAG_CW))
            return res
        for i in range(n):
            join((i + n - 1) % n, i, (i + 1) % n, src[(i + n - 1) % n][2], src[i][2])
            emit(pending)
            pending = LINE_TO
        res.append((0.0, 0.0, END_POLY | FLAG_CLOSE | FLAG_CCW))
        pending = MOVE_TO
        for i in range(n - 1, -1, -1):
            join((i + 1) % n, i, (i + n - 1) % n, src[i][2], src[(i + n - 1) % n][2])
            emit(pending)
            pending = LINE_TO
        res.append((0.0, 0.0, END_POLY | FLAG_CLOSE | FLAG_CW))
        return res


class DashGen:
    """vcgen_dash."""

    def __init__(self, dashes, start):
        self.dashes: list = []
        self.total = 0.0
        for d in dashes:
            if len(self.dashes) < 32:
                self.total = F(self.total + d)
                self.dashes.append(d)
        if start < 0:
            s = F(self.total * 2)
            start = F(start + F(F(math.ceil(F(-start / s))) * s))
        self.dash_start = start
        self.src = _Seq()
        self.closed = 0
        self._calc_start(start)

    def _calc_start(self, ds):
        cycle = self.total
        if len(self.dashes) % 2 == 1:
            cycle = F(cycle * 2)
        ds = F(ds - F(F(math.floor(F(ds / cycle))) * cycle))
        self.cur = 0
        self.cur_start = 0.0
        self.is_dash = True
        while ds > 0:
            if ds > self.dashes[self.cur]:
                ds = F(ds - self.dashes[self.cur])
                self._next()
                self.cur_start = 0.0
            else:
                self.cur_start = ds
                ds = 0.0

    def _next(self):
        self.cur += 1
        if self.cur >= len(self.dashes):
            self.cur = 0
        self.is_dash = not self.is_dash

    def remove_all(self):
        self.src = _Seq()
        self.closed = 0

    def add_vertex(self, x, y, cmd):
        if cmd & ~0x80 == MOVE_TO:
            self.src.modify_last([x, y, 0.0])
        elif is_vertex(cmd):
            self.src.add([x, y, 0.0])
        else:
            self.closed = cmd & FLAG_CLOSE

    def generate(self):
        src = self.src
        src.close(self.closed != 0)
        n = len(src)
        if n < 2:
            return []
        res: list = []
        sv = 1
        v1, v2 = src[0], src[1]
        rest = v1[2]
        res.append((v1[0], v1[1], MOVE_TO))
        if self.dash_start >= 0:
            self._calc_start(self.dash_start)
        while True:
            dash_rest = F(self.dashes[self.cur] - self.cur_start)
            cmd = LINE_TO if self.is_dash else MOVE_TO
            stop = False
            if rest > dash_rest:
                rest = F(rest - dash_rest)
                self._next()
                self.cur_start = 0.0
                x = F(v2[0] - F(F(F(v2[0] - v1[0]) * rest) / v1[2]))
                y = F(v2[1] - F(F(F(v2[1] - v1[1]) * rest) / v1[2]))
            else:
                self.cur_start = F(self.cur_start + rest)
                x, y = v2[0], v2[1]
                sv += 1
                v1 = v2
                rest = v1[2]
                if self.closed:
                    if sv > n:
                        stop = True
                    else:
                        v2 = src[0 if sv >= n else sv]
                else:
                    if sv >= n:
                        stop = True
                    else:
                        v2 = src[sv]
            res.append((x, y, cmd))
            if stop:
                return res


def adapt(source: list, gen) -> list:
    """conv_adaptor_vcgen: feed `source` vertices to the generator one subpath at a time."""
    out: list = []
    n = len(source)
    if not n:
        return out
    pos = 0
    sx, sy, last = source[0]
    pos = 1
    while True:
        if last == STOP:
            return out
        gen.remove_all()
        gen.add_vertex(sx, sy, MOVE_TO)
        while True:
            if pos >= n:
                last = STOP
                break
            x, y, cmd = source[pos]
            pos += 1
            if is_vertex(cmd):
                last = cmd
                if cmd & ~0x80 == MOVE_TO:
                    sx, sy = x, y
                    break
                gen.add_vertex(x, y, cmd)
            elif is_end_poly(cmd):
                gen.add_vertex(x, y, cmd)
                break
        out.extend(gen.generate())


def stroke_vertices(path: list, matrix, line_width, cap, join, miter_limit, dash, dash_phase, scale):
    """RasterizeStroke's geometry: the stroke outline of `path` (already in the stroke's space),
    transformed by `matrix` (None: none), as rasterizer vertices."""
    width = F(line_width * scale)
    unit = 1.0
    if matrix is not None:
        unit = F(1.0 / F(F(x_unit(matrix) + y_unit(matrix)) / 2))
    width = max(width, unit)
    src = path
    if dash:
        cycle = 0.0
        use = True
        for v in dash:
            if not math.isfinite(v):
                use = False
                break
            cycle = F(cycle + max(0.0, v))
        if use and F(cycle * scale) < F(0.1):
            use = False
        if use:
            lens = [abs(F((F(0.1) if d <= F(0.000001) else d) * scale)) for d in dash]
            src = adapt(path, DashGen(lens, F(dash_phase * scale)))
    agg_join = ROUND_JOIN if join == 1 else (BEVEL_JOIN if join == 2 else MITER_JOIN_REVERT)
    agg_cap = ROUND_CAP if cap == 1 else (SQUARE_CAP if cap == 2 else BUTT_CAP)
    out = adapt(src, StrokeGen(width, agg_cap, agg_join, miter_limit))
    if matrix is not None:
        out = [(*transform(matrix, x, y), c) if is_vertex(c) else (x, y, c) for x, y, c in out]
    return out


# ---------------------------------------------------------------------- rasterizer


class Rasterizer:
    """rasterizer_scanline_aa + outline_aa, clip box always set (as PDFium does)."""

    def __init__(self, width: int, height: int):
        x1, y1, x2, y2 = 0, 0, int(F(width * 256.0)), int(F(height * 256.0))
        self.clip = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.cells: dict = {}
        self.min_x = self.min_y = 0x7FFFFFFF
        self.max_x = self.max_y = -0x7FFFFFFF
        self.cx = self.cy = 0x7FFF
        self.cov = self.area = 0
        self.cur_x = self.cur_y = 0
        self.status = 0            # initial, line_to (1), closed (2)
        self.start = (0, 0)
        self.prev = (0, 0)
        self.prev_flags = 0
        self.clipped_start = (0, 0)

    # ---- outline_aa
    def _add_cur(self):
        if self.area | self.cov:
            key = (self.cy, self.cx)
            c = self.cells.get(key)
            if c is None:
                self.cells[key] = [self.cov, self.area]
            else:
                c[0] += self.cov
                c[1] += self.area

    def _set_cell(self, x, y):
        if self.cx != x or self.cy != y:
            self._add_cur()
            self.cx, self.cy, self.cov, self.area = x, y, 0, 0
            if x < self.min_x:
                self.min_x = x
            if x > self.max_x:
                self.max_x = x
            if y < self.min_y:
                self.min_y = y
            if y > self.max_y:
                self.max_y = y

    def _hline(self, ey, x1, y1, x2, y2):
        ex1, ex2 = x1 >> 8, x2 >> 8
        fx1, fx2 = x1 & 255, x2 & 255
        if y1 == y2:
            self._set_cell(ex2, ey)
            return
        if ex1 == ex2:
            delta = y2 - y1
            self.cov += delta
            self.area += (fx1 + fx2) * delta
            return
        p = (256 - fx1) * (y2 - y1)
        first = 256
        incr = 1
        dx = x2 - x1
        if dx < 0:
            p = fx1 * (y2 - y1)
            first = 0
            incr = -1
            dx = -dx
        delta, mod = divmod(p, dx)
        self.cov += delta
        self.area += (fx1 + first) * delta
        ex1 += incr
        self._set_cell(ex1, ey)
        y1 += delta
        if ex1 != ex2:
            lift, rem = divmod(256 * (y2 - y1 + delta), dx)
            mod -= dx
            while ex1 != ex2:
                delta = lift
                mod += rem
                if mod >= 0:
                    mod -= dx
                    delta += 1
                self.cov += delta
                self.area += 256 * delta
                y1 += delta
                ex1 += incr
                self._set_cell(ex1, ey)
        delta = y2 - y1
        self.cov += delta
        self.area += (fx2 + 256 - first) * delta

    def _line(self, x1, y1, x2, y2):
        dx = x2 - x1
        if dx >= 16384 << 8 or dx <= -(16384 << 8):
            cx = int((x1 + x2) / 2)     # C: truncating division
            cy = int((y1 + y2) / 2)
            self._line(x1, y1, cx, cy)
            self._line(cx, cy, x2, y2)
            # PDFium's copy goes on and renders the whole line as well
        dy = y2 - y1
        ey1, ey2 = y1 >> 8, y2 >> 8
        fy1, fy2 = y1 & 255, y2 & 255
        if ey1 == ey2:
            self._hline(ey1, x1, fy1, x2, fy2)
            return
        incr = 1
        if dx == 0:
            ex = x1 >> 8
            two_fx = (x1 - (ex << 8)) << 1
            first = 256
            if dy < 0:
                first = 0
                incr = -1
            delta = first - fy1
            self.cov += delta
            self.area += two_fx * delta
            ey1 += incr
            self._set_cell(ex, ey1)
            delta = first + first - 256
            area = two_fx * delta
            while ey1 != ey2:
                self.cov, self.area = delta, area
                ey1 += incr
                self._set_cell(ex, ey1)
            delta = fy2 - 256 + first
            self.cov += delta
            self.area += two_fx * delta
            return
        p = (256 - fy1) * dx
        if not _int32(p):
            return
        first = 256
        if dy < 0:
            p = fy1 * dx
            if not _int32(p):
                return
            first = 0
            incr = -1
            dy = -dy
        delta, mod = divmod(p, dy)
        x_from = x1 + delta
        self._hline(ey1, x1, fy1, x_from, first)
        ey1 += incr
        self._set_cell(x_from >> 8, ey1)
        if ey1 != ey2:
            if not _int32(256 * dx):
                return
            lift, rem = divmod(256 * dx, dy)
            mod -= dy
            while ey1 != ey2:
                delta = lift
                mod += rem
                if mod >= 0:
                    mod -= dy
                    delta += 1
                x_to = x_from + delta
                self._hline(ey1, x_from, 256 - first, x_to, first)
                x_from = x_to
                ey1 += incr
                self._set_cell(x_from >> 8, ey1)
        self._hline(ey1, x_from, 256 - first, x2, fy2)

    def _o_move(self, x, y):
        self._set_cell(x >> 8, y >> 8)
        self.cur_x, self.cur_y = x, y

    def _o_line(self, x, y):
        self._line(self.cur_x, self.cur_y, x, y)
        self.cur_x, self.cur_y = x, y

    # ---- rasterizer_scanline_aa (clipping on)
    def _flags(self, x, y):
        c = self.clip
        return (x > c[2]) | ((y > c[3]) << 1) | ((x < c[0]) << 2) | ((y < c[1]) << 3)

    def _move_no_clip(self, x, y):
        if self.status == 1:
            self._close_no_clip()
        self._o_move(x, y)
        self.clipped_start = (x, y)
        self.status = 1

    def _line_no_clip(self, x, y):
        if self.status != 0:
            self._o_line(x, y)
            self.status = 1

    def _close_no_clip(self):
        if self.status == 1:
            self._o_line(*self.clipped_start)
            self.status = 2

    def move_to(self, x, y):
        if self.status == 1:
            self.close_polygon()
        self.prev = self.start = (x, y)
        self.status = 0
        self.prev_flags = self._flags(x, y)
        if self.prev_flags == 0:
            self._move_no_clip(x, y)

    def line_to(self, x, y):
        self._clip_segment(x, y)

    def close_polygon(self):
        if self.status != 1:
            return
        self._clip_segment(*self.start)
        self._close_no_clip()

    def _clip_segment(self, x, y):
        flags = self._flags(x, y)
        if self.prev_flags == flags:
            if flags == 0:
                if self.status == 0:
                    self._move_no_clip(x, y)
                else:
                    self._line_no_clip(x, y)
        else:
            for px, py in _liang_barsky(self.prev[0], self.prev[1], x, y, self.clip):
                if self.status == 0:
                    self._move_no_clip(px, py)
                else:
                    self._line_no_clip(px, py)
        self.prev_flags = flags
        self.prev = (x, y)

    def add_vertex(self, x, y, cmd):
        if is_close(cmd):
            self.close_polygon()
        elif cmd & ~0x80 == MOVE_TO:
            self.move_to(int(F(x * 256.0)), int(F(y * 256.0)))
        elif is_vertex(cmd):
            self.line_to(int(F(x * 256.0)), int(F(y * 256.0)))

    def add_path(self, vertices):
        for x, y, cmd in vertices:
            self.add_vertex(x, y, cmd)

    # ---- sweep
    def coverage(self, even_odd: bool, no_smooth: bool = False):
        """rewind_scanlines + sweep_scanline for every row: (x0, y0, alpha) with alpha a uint8
        array over the cells' bounding box [min_x, max_x] x [min_y, max_y], or None."""
        self.close_polygon()
        self._add_cur()
        self.cx = self.cy = 0x7FFF  # (sorted: nothing more is added)
        self.cov = self.area = 0
        cells = self.cells
        if not cells:
            return None
        keys = np.array(list(cells.keys()), dtype=np.int64)
        vals = np.array(list(cells.values()), dtype=np.int64)
        order = np.lexsort((keys[:, 1], keys[:, 0]))
        ys, xs = keys[order, 0], keys[order, 1]
        cover, area = vals[order, 0], vals[order, 1]
        # running cover within each row
        csum = np.cumsum(cover)
        row_start = np.ones(len(ys), dtype=bool)
        row_start[1:] = ys[1:] != ys[:-1]
        starts = np.flatnonzero(row_start)
        base = np.repeat(csum[starts] - cover[starts], np.diff(np.append(starts, len(ys))))
        run = csum - base
        x0, y0 = self.min_x, self.min_y
        w = self.max_x - x0 + 2
        h = self.max_y - y0 + 1
        delta = np.zeros((h, w + 1), dtype=np.int64)
        ry = ys - y0
        # the cell itself
        has_area = area != 0
        cell_alpha = _alpha((run << 9) - area, even_odd, no_smooth)
        span_from = xs + has_area.astype(np.int64)
        # the span to the next cell of the row
        nxt = np.empty_like(xs)
        nxt[:-1] = xs[1:]
        last_in_row = np.ones(len(ys), dtype=bool)
        last_in_row[:-1] = ys[:-1] != ys[1:]
        span_alpha = _alpha(run << 9, even_odd, no_smooth)
        ok = (~last_in_row) & (nxt > span_from) & (span_alpha > 0)
        np.add.at(delta, (ry[ok], span_from[ok] - x0), span_alpha[ok])
        np.add.at(delta, (ry[ok], nxt[ok] - x0), -span_alpha[ok])
        cov = np.cumsum(delta, axis=1)[:, :w]
        sel = has_area & (cell_alpha > 0)
        cov[ry[sel], xs[sel] - x0] = cell_alpha[sel]
        return x0, y0, cov.astype(np.uint8)


def _int32(v: int) -> bool:
    return -2147483648 <= v <= 2147483647


def _alpha(area, even_odd: bool, no_smooth: bool):
    """calculate_alpha on an array."""
    cover = np.abs(area >> 9)
    if even_odd:
        cover = cover & 511
        cover = np.where(cover > 256, 512 - cover, cover)
    if no_smooth:
        cover = np.where(cover > 127, 255, 0)
    return np.minimum(cover, 255)


def _liang_barsky(x1, y1, x2, y2, box):
    """clip_liang_barsky on ints, float arithmetic as in agg."""
    bx1, by1, bx2, by2 = box
    fx1, fy1 = F(float(x1)), F(float(y1))
    deltax = F(F(float(x2)) - fx1)
    deltay = F(F(float(y2)) - fy1)
    out = []
    if deltax == 0:
        deltax = -EPS30 if x1 > bx1 else EPS30
    if deltax > 0:
        xin, xout = F(float(bx1)), F(float(bx2))
    else:
        xin, xout = F(float(bx2)), F(float(bx1))
    tinx = F(F(xin - fx1) / deltax)
    if deltay == 0:
        deltay = -EPS30 if y1 > by1 else EPS30
    if deltay > 0:
        yin, yout = F(float(by1)), F(float(by2))
    else:
        yin, yout = F(float(by2)), F(float(by1))
    tiny = F(F(yin - fy1) / deltay)
    if tinx < tiny:
        tin1, tin2 = tinx, tiny
    else:
        tin1, tin2 = tiny, tinx
    if tin1 <= 1.0:
        if 0 < tin1:
            out.append((int(xin), int(yin)))
        if tin2 <= 1.0:
            toutx = F(F(xout - fx1) / deltax)
            touty = F(F(yout - fy1) / deltay)
            tout1 = toutx if toutx < touty else touty
            if tin2 > 0 or tout1 > 0:
                if tin2 <= tout1:
                    if tin2 > 0:
                        if tinx > tiny:
                            out.append((int(xin), int(F(fy1 + F(deltay * tinx)))))
                        else:
                            out.append((int(F(fx1 + F(deltax * tiny))), int(yin)))
                    if tout1 < 1.0:
                        if toutx < touty:
                            out.append((int(xout), int(F(fy1 + F(deltay * toutx)))))
                        else:
                            out.append((int(F(fx1 + F(deltax * touty))), int(yout)))
                    else:
                        out.append((x2, y2))
                else:
                    if tinx > tiny:
                        out.append((int(xin), int(yout)))
                    else:
                        out.append((int(xout), int(yin)))
    return out
