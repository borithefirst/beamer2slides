"""Function-based and mesh shadings as PDFium draws them (cpdf_rendershading.cpp): DrawFuncShading
(type 1: every pixel through the inverted matrices and the 2-in functions, truncated to bytes),
DrawFreeGouraudShading / DrawLatticeGouraudShading (types 4 and 5: CPDF_MeshStream's vertices,
triangles filled scanline by scanline with colours stepped across each span in float) and
DrawCoonPatchMeshes (types 6 and 7: a patch cut in halves until its corner colours are close,
each piece filled as a 13-point bezier outline on an AGG device with full cover). The buffer is
render_shading's (a BGRA bitmap the clipped object's size); nothing is blended into it, pixels are
overwritten, as the C++ does. Float32 one operation at a time, x86 conversions."""

from __future__ import annotations

import numpy as np

from . import raster as R
from .raster import F
from .crt import i32_array
from .render_shading import INT_MAX, INT_MIN, U32, argb, i32


# ---------------------------------------------------------------------- type 1


def draw_function(bitmap, final, rec, alpha: int) -> None:
    """DrawFuncShading."""
    cs, funcs, a, d = rec.cs, rec.funcs, rec.a, rec.dict
    total = sum(f.outputs for f in funcs if f is not None)
    if total > U32:
        total = 0
    count = max(total, cs.n) if total else 0
    if count == 0:
        return
    dom = a.array_for(d, "Domain")
    xmin, xmax, ymin, ymax = 0.0, 1.0, 0.0, 1.0
    if dom is not None:
        xmin, xmax, ymin, ymax = (a.float_at(dom, i) for i in range(4))
    m = R.concat(R.inverse(final), R.inverse(a.matrix_for(d, "Matrix")))
    h, w = bitmap.shape
    results = [0.0] * count
    ma, mb, mc, md, me, mf = (np.float32(v) for v in m)
    with np.errstate(all="ignore"):
        cols = np.arange(w, dtype=np.float32)[None, :]
        rows = np.arange(h, dtype=np.float32)[:, None]
        px = (ma * cols + mc * rows) + me
        py = (mb * cols + md * rows) + mf
        skip = (px < np.float32(xmin)) | (px > np.float32(xmax)) | (py < np.float32(ymin)) | (py > np.float32(ymax))
    for row in range(h):
        line = bitmap[row]
        xs, ys, sk = px[row].tolist(), py[row].tolist(), skip[row].tolist()
        for col in range(w):
            if sk[col]:
                continue
            off = 0
            for f in funcs:
                if f is None:
                    continue
                n = f.call([xs[col], ys[col]], results, off)
                if n is not None:
                    off += n
            r, g, b = cs.rgb_or_zeros(results)
            line[col] = argb(alpha, i32(F(r * 255.0)), i32(F(g * 255.0)), i32(F(b * 255.0)))


# ---------------------------------------------------------------------- mesh streams

_COORD_BITS = (1, 2, 4, 8, 12, 16, 24, 32)
_COMP_BITS = (1, 2, 4, 8, 12, 16)
_FLAG_BITS = (2, 4, 8)


class _Bits:
    """CFX_BitStream."""

    def __init__(self, data: bytes):
        self.data, self.size, self.pos = data, len(data) * 8, 0

    def eof(self) -> bool:
        return self.pos >= self.size

    def remaining(self) -> int:
        return self.size - self.pos if self.size >= self.pos else 0

    def get(self, n: int) -> int:
        if n > self.size or self.pos > self.size - n:
            return 0
        p = self.pos
        first, last = p >> 3, (p + n - 1) >> 3
        v = int.from_bytes(self.data[first:last + 1], "big")
        v >>= (last + 1) * 8 - (p + n)
        self.pos = p + n
        return v & ((1 << n) - 1)

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


def shading_index(c: float, lo: float, hi: float) -> float:
    """ComponentToShadingIndex."""
    if lo == hi:
        return 0.0
    return F(F(F(c - lo) / F(hi - lo)) * 255.0)


class MeshStream:
    """CPDF_MeshStream: `load()` then the readers; vertices are (x, y, r, g, b)."""

    def __init__(self, rec, stype: int):
        self.rec, self.type = rec, stype

    def load(self) -> bool:
        rec, a = self.rec, self.rec.a
        d = rec.dict
        self.bits = _Bits(bytes(a.doc.stream_data(rec.shading)))
        self.coord_bits = a.integer_for(d, "BitsPerCoordinate") & U32
        self.comp_bits = a.integer_for(d, "BitsPerComponent") & U32
        if self.coord_bits not in _COORD_BITS or self.comp_bits not in _COMP_BITS:
            return False
        self.flag_bits = a.integer_for(d, "BitsPerFlag") & U32
        if self.type != 5 and self.flag_bits not in _FLAG_BITS:
            return False
        n = rec.cs.n
        if n > 8:
            return False
        self.components = 1 if rec.funcs else n
        dec = a.array_for(d, "Decode")
        if dec is None or len(dec) != 4 + 2 * self.components:
            return False
        self.xmin, self.xmax, self.ymin, self.ymax = (a.float_at(dec, i) for i in range(4))
        self.cmin = [a.float_at(dec, 4 + 2 * i) for i in range(self.components)] + [0.0] * (8 - self.components)
        self.cmax = [a.float_at(dec, 5 + 2 * i) for i in range(self.components)] + [0.0] * (8 - self.components)
        self.coord_max = U32 if self.coord_bits == 32 else (1 << self.coord_bits) - 1
        self.comp_max = (1 << self.comp_bits) - 1
        return True

    def can_flag(self) -> bool:
        return self.bits.remaining() >= self.flag_bits

    def can_coords(self) -> bool:
        return self.bits.remaining() // 2 >= self.coord_bits

    def can_color(self) -> bool:
        return self.bits.remaining() // self.comp_bits >= self.components

    def flag(self) -> int:
        return self.bits.get(self.flag_bits) & 3

    def raw_coords(self) -> tuple:
        """ReadCoords."""
        get, n = self.bits.get, self.coord_bits
        if n == 32:
            # uint32 -> float, times float; then divided and added in double, stored as float
            x = F(self.xmin + F(F(float(get(n))) * F(self.xmax - self.xmin)) / 4294967295.0)
            y = F(self.ymin + F(F(float(get(n))) * F(self.ymax - self.ymin)) / 4294967295.0)
        else:
            cm = float(self.coord_max)
            x = F(self.xmin + F(F(F(float(get(n))) * F(self.xmax - self.xmin)) / cm))
            y = F(self.ymin + F(F(F(float(get(n))) * F(self.ymax - self.ymin)) / cm))
        return x, y

    def coords(self, matrix) -> tuple:
        return R.transform(matrix, *self.raw_coords())

    def color(self) -> tuple:
        vals = list(self.cmin)
        cm = float(self.comp_max)
        for i in range(self.components):
            lo, hi = self.cmin[i], self.cmax[i]
            vals[i] = F(lo + F(F(float(self.bits.get(self.comp_bits)) * F(hi - lo)) / cm))
        if not self.rec.funcs:
            return self.rec.cs.rgb_or_zeros(vals)
        return vals[0], 0.0, 0.0

    def vertex(self, matrix):
        """ReadVertex: (vertex, flag), or None."""
        if not self.can_flag():
            return None
        flag = self.flag()
        if not self.can_coords():
            return None
        x, y = self.coords(matrix)
        if not self.can_color():
            return None
        r, g, b = self.color()
        self.bits.align()
        return [x, y, r, g, b], flag

    def row(self, matrix, count: int) -> list:
        """ReadVertexRow (empty on any shortfall)."""
        out = []
        for _ in range(count):
            if self.bits.eof() or not self.can_coords():
                return []
            x, y = self.coords(matrix)
            if not self.can_color():
                return []
            r, g, b = self.color()
            self.bits.align()
            out.append([x, y, r, g, b])
        return out


def shading_bbox(rec, matrix) -> tuple:
    """GetShadingBBox (the content parser's box of an `sh` mesh): every point the stream holds,
    colours skipped, through the object's matrix. The point and colour counts of a patch shrink
    for good with each patch that shares an edge, as the C++ has it."""
    from .syntax import Stream
    stream = MeshStream(rec, rec.type)
    if not isinstance(rec.shading, Stream) or not stream.load():
        return 0.0, 0.0, 0.0, 0.0
    gouraud_ = rec.type in (4, 5)
    points = {7: 16, 6: 12}.get(rec.type, 1)
    colors = 4 if rec.type in (6, 7) else 1
    rect = None
    while not stream.bits.eof():
        flag = 0
        if rec.type != 5:
            if not stream.can_flag():
                break
            flag = stream.flag()
        if not gouraud_ and flag:
            points -= 4
            colors -= 2
        for _ in range(points):
            if not stream.can_coords():
                break
            x, y = stream.raw_coords()
            if rect is None:
                rect = [x, y, x, y]
            else:
                rect = [min(rect[0], x), min(rect[1], y), max(rect[2], x), max(rect[3], y)]
        n = stream.components * stream.comp_bits * colors
        if n < 0 or n > U32:
            break
        stream.bits.pos += n
        if gouraud_:
            stream.bits.align()
    return R.transform_rect(matrix, tuple(rect) if rect is not None else (0.0, 0.0, 0.0, 0.0))


def _mesh_steps(rec, stream, alpha: int):
    """(steps or None, ok): GetShadingSteps over the first component's decode range."""
    from .render_shading import _steps
    if not rec.funcs:
        return None, True
    steps = _steps(rec, stream.cmin[0], stream.cmax[0], alpha)
    return steps, steps is not None


# ---------------------------------------------------------------------- Gouraud triangles


def _floor_i(v: float) -> int:
    return i32(float(np.floor(np.float32(v))))


def _ceil_i(v: float) -> int:
    return i32(float(np.ceil(np.float32(v))))


def _clamp_sub(a: int, b: int) -> int:
    return max(INT_MIN, min(INT_MAX, a - b))


def _div(a: float, b: float) -> float:
    """float a / float b with IEEE results for a zero divisor."""
    with np.errstate(all="ignore"):
        return float(np.float32(a) / np.float32(b))


def _intersect(y: int, p, q):
    """GetScanlineIntersect."""
    fy, sy = p[1], q[1]
    if fy == sy:
        return None
    yf = float(y)
    if fy < sy:
        if yf < fy or yf > sy:
            return None
    elif yf < sy or yf > fy:
        return None
    return F(p[0] + _div(F(F(q[0] - p[0]) * F(yf - fy)), F(sy - fy)))


def gouraud(bitmap, alpha: int, tri, steps) -> None:
    """DrawGouraud."""
    min_y = max_y = tri[0][1]
    for i in (1, 2):
        y = tri[i][1]
        if y < min_y:
            min_y = y
        if max_y < y:
            max_y = y
    if min_y == max_y:
        return
    h, w = bitmap.shape
    min_yi = max(_floor_i(min_y), 0)
    max_yi = _ceil_i(max_y)
    if max_yi >= h:
        max_yi = h - 1
    f32 = np.float32
    for y in range(min_yi, max_yi + 1):
        xs, rs, gs, bs = [], [], [], []
        for i in range(3):
            p, q = tri[i], tri[(i + 1) % 3]
            x = _intersect(y, p, q)
            if x is None:
                continue
            xs.append(x)
            t = _div(F(float(y) - p[1]), F(q[1] - p[1]))
            rs.append(F(p[2] + F(F(q[2] - p[2]) * t)))
            gs.append(F(p[3] + F(F(q[3] - p[3]) * t)))
            bs.append(F(p[4] + F(F(q[4] - p[4]) * t)))
        if len(xs) != 2:
            continue
        if xs[0] < xs[1]:
            min_x, max_x, s, e = _floor_i(xs[0]), _ceil_i(xs[1]), 0, 1
        else:
            min_x, max_x, s, e = _floor_i(xs[1]), _ceil_i(xs[0]), 1, 0
        start_x, end_x = max(0, min(min_x, w)), max(0, min(max_x, w))
        if end_x <= start_x:
            continue
        span = float(f32(_clamp_sub(max_x, min_x)))
        diff = float(f32(_clamp_sub(start_x, min_x)))
        n = end_x - start_x

        def run(c):
            unit = _div(F(c[e] - c[s]), span)
            first = F(c[s] + F(diff * unit))
            seq = np.empty(n + 1, np.float32)
            seq[0], seq[1:] = first, unit
            with np.errstate(all="ignore"):
                return np.cumsum(seq, dtype=np.float32)[1:]
        r = run(rs)
        with np.errstate(all="ignore"):
            if steps is not None:
                idx = i32_array(r)
                bitmap[y, start_x:end_x] = steps[np.clip(idx, 0, 255)]
            else:
                g, b = run(gs), run(bs)
                bitmap[y, start_x:end_x] = _encode(alpha, _trunc255(r), _trunc255(g), _trunc255(b))


def _trunc255(v):
    """static_cast<int>(v * 255) over an array."""
    return i32_array(v * np.float32(255))


def _encode(alpha: int, r, g, b):
    m = 0xFFFFFFFF
    return ((((alpha & m) << 24) | ((r & m) << 16) | ((g & m) << 8) | (b & m)) & m).astype(np.uint32)


def draw_free(bitmap, final, rec, alpha: int) -> None:
    """DrawFreeGouraudShading."""
    stream = MeshStream(rec, 4)
    if not stream.load():
        return
    steps, ok = _mesh_steps(rec, stream, alpha)
    if not ok:
        return
    lo, hi = stream.cmin[0], stream.cmax[0]
    funcs = bool(rec.funcs)
    blank = [0.0, 0.0, 0.0, 0.0, 0.0]
    tri = [list(blank), list(blank), list(blank)]
    while not stream.bits.eof():
        got = stream.vertex(final)
        if got is None:
            return
        v, flag = got
        if funcs:
            v[2] = shading_index(v[2], lo, hi)
        if flag == 0:
            tri[0] = v
            for i in (1, 2):
                more = stream.vertex(final)
                if more is None:
                    return
                tri[i] = more[0]
                if funcs:
                    tri[i][2] = shading_index(tri[i][2], lo, hi)
        else:
            if flag == 1:
                tri[0] = tri[1]
            tri[1] = tri[2]
            tri[2] = v
        gouraud(bitmap, alpha, tri, steps)


def draw_lattice(bitmap, final, rec, alpha: int) -> None:
    """DrawLatticeGouraudShading."""
    per_row = rec.a.integer_for(rec.dict, "VerticesPerRow")
    if per_row < 2:
        return
    stream = MeshStream(rec, 5)
    if not stream.load():
        return
    steps, ok = _mesh_steps(rec, stream, alpha)
    if not ok:
        return
    lo, hi = stream.cmin[0], stream.cmax[0]
    funcs = bool(rec.funcs)
    rows = [stream.row(final, per_row), []]
    if not rows[0]:
        return
    if funcs:
        for v in rows[0]:
            v[2] = shading_index(v[2], lo, hi)
    last = 0
    while True:
        rows[1 - last] = stream.row(final, per_row)
        if not rows[1 - last]:
            return
        if funcs:
            for v in rows[1 - last]:
                v[2] = shading_index(v[2], lo, hi)
        cur, nxt = rows[last], rows[1 - last]
        for i in range(1, per_row):
            gouraud(bitmap, alpha, [cur[i], nxt[i - 1], cur[i - 1]], steps)
            gouraud(bitmap, alpha, [cur[i], nxt[i - 1], nxt[i]], steps)
        last = 1 - last


# ---------------------------------------------------------------------- Coons / tensor patches

_HALF = F(0.5)
_NINTH = F(1.0 / 9.0)


def _mid(p, q):
    """0.5f * (p + q) on CFX_PointF."""
    return F(_HALF * F(p[0] + q[0])), F(_HALF * F(p[1] + q[1]))


def _split(c0, c1, c2, c3):
    l1 = (_mid(c0, c1), _mid(c1, c2), _mid(c2, c3))
    l2 = (_mid(l1[0], l1[1]), _mid(l1[1], l1[2]))
    l3 = _mid(l2[0], l2[1])
    return (c0, l1[0], l2[0], l3), (l3, l2[1], l1[2], c3)


def _vertical(p):
    """SubdivideVertical: along the second index."""
    top, bottom = [None] * 4, [None] * 4
    for x in range(4):
        top[x], bottom[x] = _split(*p[x])
    return top, bottom


def _horizontal(p):
    """SubdivideHorizontal: along the first index."""
    left, right = [[None] * 4 for _ in range(4)], [[None] * 4 for _ in range(4)]
    for y in range(4):
        a, b = _split(p[0][y], p[1][y], p[2][y], p[3][y])
        for x in range(4):
            left[x][y], right[x][y] = a[x], b[x]
    return left, right


def _is_small(p) -> bool:
    """CFX_FloatRect::GetBBox of the 16 points: width and height under 2."""
    pts = [q for row in p for q in row]
    l = r = pts[0][0]
    b = t = pts[0][1]
    for x, y in pts[1:]:
        l, r = min(l, x), max(r, x)
        b, t = min(b, y), max(t, y)
    return F(r - l) < 2.0 and F(t - b) < 2.0


def _interpolate(p1: int, p2: int, d1: int, d2: int, over: list) -> int:
    """Interpolate over FX_SAFE_INT32 (an invalid step taints the rest, then 0)."""
    def ok(v):
        return INT_MIN <= v <= INT_MAX
    p = p2 - p1
    valid = ok(p)
    if valid:
        p *= d1
        valid = ok(p)
    if valid:
        if d2 == 0 or (p == INT_MIN and d2 == -1):
            valid = False
        else:
            q = abs(p) // abs(d2)
            p = q if (p >= 0) == (d2 > 0) else -q
    if valid:
        p += p1
        valid = ok(p)
    if not valid:
        over[0] = True
        return 0
    return p


def _bi(colors, x: int, y: int, xs: int, ys: int, over: list) -> list:
    out = []
    for i in range(3):
        x1 = _interpolate(colors[0][i], colors[3][i], x, xs, over)
        x2 = _interpolate(colors[1][i], colors[2][i], x, xs, over)
        out.append(_interpolate(x1, x2, y, ys, over))
    return out


def _dist(a, b) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _wrap(v: int) -> int:
    v &= U32
    return v - (1 << 32) if v & 0x80000000 else v


FILL_WINDING = 2        # render.FILL_WINDING (render imports this module's caller)


class _PatchDrawer:
    THRESHOLD = 4

    def __init__(self, device, alpha: int, steps):
        self.dev, self.alpha, self.steps = device, alpha, steps
        self.colors = [[0, 0, 0] for _ in range(4)]

    def draw(self, xs: int, ys: int, left: int, bottom: int, p) -> None:
        small = _is_small(p)
        over = [False]
        c0 = _bi(self.colors, left, bottom, xs, ys, over)
        if over[0]:
            return
        db = dl = dt = dr = 0
        if not small:
            c1 = _bi(self.colors, left, bottom + 1, xs, ys, over)
            if over[0]:
                return
            c2 = _bi(self.colors, left + 1, bottom + 1, xs, ys, over)
            if over[0]:
                return
            c3 = _bi(self.colors, left + 1, bottom, xs, ys, over)
            if over[0]:
                return
            db, dl, dt, dr = _dist(c3, c0), _dist(c1, c0), _dist(c1, c2), _dist(c2, c3)
        T = self.THRESHOLD
        if small or (db < T and dl < T and dt < T and dr < T):
            bnd = [p[0][0], p[0][1], p[0][2], p[0][3], p[1][3], p[2][3], p[3][3],
                   p[3][2], p[3][1], p[3][0], p[2][0], p[1][0], p[0][0]]
            points = [(bnd[0][0], bnd[0][1], R.PT_MOVE, False)] + \
                [(x, y, R.PT_BEZIER, False) for x, y in bnd[1:]]
            if self.steps is not None:
                color = int(self.steps[max(0, min(c0[0], 255))])
            else:
                color = argb(self.alpha, c0[0], c0[1], c0[2])
            self.dev.draw_path(points, None, None, color, 0, FILL_WINDING, False, full_cover=True)
            return
        if db < T and dt < T:
            top, bot = _vertical(p)
            ys, bottom = _wrap(ys * 2), _wrap(bottom * 2)
            self.draw(xs, ys, left, bottom, top)
            self.draw(xs, ys, left, _wrap(bottom + 1), bot)
        elif dl < T and dr < T:
            lp, rp = _horizontal(p)
            xs, left = _wrap(xs * 2), _wrap(left * 2)
            self.draw(xs, ys, left, bottom, lp)
            self.draw(xs, ys, _wrap(left + 1), bottom, rp)
        else:
            top, bot = _vertical(p)
            tl, tr = _horizontal(top)
            bl, br = _horizontal(bot)
            xs, ys, left, bottom = _wrap(xs * 2), _wrap(ys * 2), _wrap(left * 2), _wrap(bottom * 2)
            self.draw(xs, ys, left, bottom, tl)
            self.draw(xs, ys, left, _wrap(bottom + 1), bl)
            self.draw(xs, ys, _wrap(left + 1), bottom, tr)
            self.draw(xs, ys, _wrap(left + 1), _wrap(bottom + 1), br)


def _lin(terms):
    """sum of k * point in order, float32 per operation (CFX_PointF arithmetic)."""
    x = y = None
    for k, (px, py) in terms:
        tx, ty = F(k * px), F(k * py)
        if x is None:
            x, y = tx, ty
        else:
            x, y = F(x + tx), F(y + ty)
    return x, y


def _padd(p, q):
    return F(p[0] + q[0]), F(p[1] + q[1])


def _inner(p00, p01, p10, p03, p30, p31, p13, p33):
    """(1/9) * (-4 p00 + 6 (p01 + p10) - 2 (p03 + p30) + 3 (p31 + p13) - 1 p33)."""
    s = _lin([(F(-4.0), p00), (F(6.0), _padd(p01, p10))])
    t = _lin([(F(2.0), _padd(p03, p30))])
    s = F(s[0] - t[0]), F(s[1] - t[1])
    u = _lin([(F(3.0), _padd(p31, p13))])
    s = F(s[0] + u[0]), F(s[1] + u[1])
    v = _lin([(F(1.0), p33)])
    s = F(s[0] - v[0]), F(s[1] - v[1])
    return F(_NINTH * s[0]), F(_NINTH * s[1])


def draw_patches(bitmap, final, rec, alpha: int) -> None:
    """DrawCoonPatchMeshes (types 6 and 7)."""
    from .render import Device
    stream = MeshStream(rec, rec.type)
    if not stream.load():
        return
    steps, ok = _mesh_steps(rec, stream, alpha)
    if not ok:
        return
    h, w = bitmap.shape
    dev = Device(w, h, True)
    dev.bgra = bitmap.view(np.uint8).reshape(h, w, 4)
    lo, hi = stream.cmin[0], stream.cmax[0]
    drawer = _PatchDrawer(dev, alpha, steps)
    coords = [(0.0, 0.0)] * 16
    count = 16 if rec.type == 7 else 12
    while not stream.bits.eof():
        if not stream.can_flag():
            break
        flag = stream.flag()
        start_pt = start_col = 0
        if flag:
            start_pt, start_col = 4, 2
            coords[0:4] = [coords[(flag * 3 + i) % 12] for i in range(4)]
            cols = drawer.colors
            drawer.colors = [cols[flag], cols[(flag + 1) % 4], cols[2], cols[3]]
        for i in range(start_pt, count):
            if not stream.can_coords():
                break
            coords[i] = stream.coords(final)
        for i in range(start_col, 4):
            if not stream.can_color():
                break
            r, g, b = stream.color()
            if not rec.funcs:
                drawer.colors[i] = [i32(F(r * 255.0)), i32(F(g * 255.0)), i32(F(b * 255.0))]
            else:
                drawer.colors[i] = [i32(shading_index(r, lo, hi)), 0, 0]
        xs = [c[0] for c in coords[:count]]
        ys = [c[1] for c in coords[:count]]
        left = right = xs[0]
        bottom = top = ys[0]
        for x, y in zip(xs[1:], ys[1:]):
            left, right = min(left, x), max(right, x)
            bottom, top = min(bottom, y), max(top, y)
        if right <= 0 or left >= float(w) or top <= 0 or bottom >= float(h):
            continue
        c = coords
        p = [[None] * 4 for _ in range(4)]
        p[0] = [c[0], c[1], c[2], c[3]]
        p[1][3], p[2][3], p[3][3] = c[4], c[5], c[6]
        p[3][2], p[3][1], p[3][0] = c[7], c[8], c[9]
        p[2][0], p[1][0] = c[10], c[11]
        if rec.type == 7:
            p[1][1], p[1][2], p[2][2], p[2][1] = c[12], c[13], c[14], c[15]
        else:
            p[1][1] = _inner(p[0][0], p[0][1], p[1][0], p[0][3], p[3][0], p[3][1], p[1][3], p[3][3])
            p[1][2] = _inner(p[0][3], p[0][2], p[1][3], p[0][0], p[3][3], p[3][2], p[1][0], p[3][0])
            p[2][1] = _inner(p[3][0], p[3][1], p[2][0], p[3][3], p[0][0], p[0][1], p[2][3], p[0][3])
            p[2][2] = _inner(p[3][3], p[3][2], p[2][3], p[3][0], p[0][3], p[0][2], p[2][0], p[0][0])
        drawer.draw(1, 1, 0, 0, p)
