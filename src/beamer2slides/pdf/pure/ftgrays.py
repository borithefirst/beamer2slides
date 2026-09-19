"""FreeType's anti-aliasing rasterizer (smooth/ftgrays.c) and LCD renderer (smooth/ftsmooth.c).

PDFium renders a glyph with ``FT_Render_Glyph(FT_RENDER_MODE_LCD)``: the outline's control box
is grown by the LCD padding and rounded outwards to whole pixels (`preset_bitmap`), the outline is
shifted into that bitmap and swept by the cell rasterizer of ftgrays.c, whose spans are spread into
three bytes per pixel. FreeType is compiled either with ``FT_CONFIG_OPTION_SUBPIXEL_RENDERING``
(the outline is stretched ×3 and every span goes through the 5-tap FIR filter 08 4D 56 4D 08) or
without it ("Harmony": the outline is rendered three times, shifted by ∓21/64 px, one byte each);
`LCD_MODE` names the one PDFium's build uses, verified against it pixel for pixel.

Everything is integer arithmetic on FreeType's own types (26.6 outline points, 24.8 raster
coordinates, 64-bit products where ftgrays.c has them), so the bitmap is FreeType's, byte for byte.
The outline is ``[(points, tags)]`` per contour, points in 26.6, tags ON/CUBIC (PostScript glyphs
have no conic points).
"""
from __future__ import annotations

ON, CUBIC, CONIC = 1, 2, 0

PIXEL_BITS = 8
ONE_PIXEL = 1 << PIXEL_BITS
INT_MIN = -(1 << 31)

SUBPIXEL = "subpixel"
HARMONY = "harmony"
LCD_MODE = SUBPIXEL

LCD_WEIGHTS = (0x08, 0x4D, 0x56, 0x4D, 0x08)
LCD_GEOMETRY = (-21, 0, 21)          # Harmony's sub[i].x (y all 0)


def _int32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _udiv_prep(d: int) -> int:
    """``FT_UDIVPREP``: 0xFFFFFFFF / d in 64-bit C division, used with the sign of the call site."""
    return 0xFFFFFFFF // abs(d) if d else 0


def _udiv(a: int, r: int) -> int:
    """``FT_UDIV``: (UInt64)a * (UInt64)r >> 32, cast to int."""
    return _int32(((a & 0xFFFFFFFFFFFFFFFF) * (r & 0xFFFFFFFFFFFFFFFF) & 0xFFFFFFFFFFFFFFFF) >> 32)


class _Raster:
    """One ``gray_TWorker``: cells per scanline, then a sweep into spans."""

    def __init__(self, min_ex: int, min_ey: int, max_ex: int, max_ey: int):
        self.min_ex, self.min_ey, self.max_ex, self.max_ey = min_ex, min_ey, max_ex, max_ey
        self.rows: dict[int, dict[int, list]] = {}
        self.cell = None
        self.x = self.y = 0

    def set_cell(self, ex: int, ey: int) -> None:
        if ey < self.min_ey or ey >= self.max_ey or ex >= self.max_ex:
            self.cell = None
            return
        ex = max(ex, self.min_ex - 1)
        row = self.rows.setdefault(ey, {})
        cell = row.get(ex)
        if cell is None:
            cell = row[ex] = [0, 0]              # cover, area
        self.cell = cell

    def integrate(self, a: int, b: int) -> None:
        cell = self.cell
        if cell is not None:
            cell[0] = _int32(cell[0] + a)
            cell[1] = _int32(cell[1] + a * b)

    def move_to(self, x: int, y: int) -> None:
        x, y = x * 4, y * 4                      # UPSCALE
        self.set_cell(x >> PIXEL_BITS, y >> PIXEL_BITS)
        self.x, self.y = x, y

    def render_line(self, to_x: int, to_y: int) -> None:
        ey1 = self.y >> PIXEL_BITS
        ey2 = to_y >> PIXEL_BITS
        if (ey1 >= self.max_ey and ey2 >= self.max_ey) or (ey1 < self.min_ey and ey2 < self.min_ey):
            self.x, self.y = to_x, to_y
            return
        ex1 = self.x >> PIXEL_BITS
        ex2 = to_x >> PIXEL_BITS
        fx1 = self.x & (ONE_PIXEL - 1)
        fy1 = self.y & (ONE_PIXEL - 1)
        dx = to_x - self.x
        dy = to_y - self.y
        if ex1 == ex2 and ey1 == ey2:
            pass
        elif dy == 0:
            self.set_cell(ex2, ey2)
            self.x, self.y = to_x, to_y
            return
        elif dx == 0:
            if dy > 0:
                while True:
                    self.integrate(ONE_PIXEL - fy1, fx1 * 2)
                    fy1 = 0
                    ey1 += 1
                    self.set_cell(ex1, ey1)
                    if ey1 == ey2:
                        break
            else:
                while True:
                    self.integrate(-fy1, fx1 * 2)
                    fy1 = ONE_PIXEL
                    ey1 -= 1
                    self.set_cell(ex1, ey1)
                    if ey1 == ey2:
                        break
        else:
            prod = dx * fy1 - dy * fx1
            dx_r = _udiv_prep(dx) if ex1 != ex2 else 0
            dy_r = _udiv_prep(dy) if ey1 != ey2 else 0
            while True:
                if prod - dx * ONE_PIXEL > 0 and prod <= 0:                        # left
                    fx2 = 0
                    fy2 = _udiv(-prod, dx_r)
                    prod -= dy * ONE_PIXEL
                    self.integrate(fy2 - fy1, fx1 + fx2)
                    fx1 = ONE_PIXEL
                    fy1 = fy2
                    ex1 -= 1
                elif prod - dx * ONE_PIXEL + dy * ONE_PIXEL > 0 and prod - dx * ONE_PIXEL <= 0:  # up
                    prod -= dx * ONE_PIXEL
                    fx2 = _udiv(-prod, dy_r)
                    fy2 = ONE_PIXEL
                    self.integrate(fy2 - fy1, fx1 + fx2)
                    fx1 = fx2
                    fy1 = 0
                    ey1 += 1
                elif prod + dy * ONE_PIXEL >= 0 and prod - dx * ONE_PIXEL + dy * ONE_PIXEL <= 0:  # right
                    prod += dy * ONE_PIXEL
                    fx2 = ONE_PIXEL
                    fy2 = _udiv(prod, dx_r)
                    self.integrate(fy2 - fy1, fx1 + fx2)
                    fx1 = 0
                    fy1 = fy2
                    ex1 += 1
                else:                                                               # down
                    fx2 = _udiv(prod, dy_r)
                    fy2 = 0
                    prod += dx * ONE_PIXEL
                    self.integrate(fy2 - fy1, fx1 + fx2)
                    fx1 = fx2
                    fy1 = ONE_PIXEL
                    ey1 -= 1
                self.set_cell(ex1, ey1)
                if ex1 == ex2 and ey1 == ey2:
                    break
        fx2 = to_x & (ONE_PIXEL - 1)
        fy2 = to_y & (ONE_PIXEL - 1)
        self.integrate(fy2 - fy1, fx1 + fx2)
        self.x, self.y = to_x, to_y

    def line_to(self, x: int, y: int) -> None:
        self.render_line(x * 4, y * 4)

    def cubic_to(self, c1, c2, to) -> None:
        arc = [(to[0] * 4, to[1] * 4), (c2[0] * 4, c2[1] * 4), (c1[0] * 4, c1[1] * 4), (self.x, self.y)]
        max_ey, min_ey = self.max_ey, self.min_ey
        if all((p[1] >> PIXEL_BITS) >= max_ey for p in arc) or all((p[1] >> PIXEL_BITS) < min_ey for p in arc):
            self.x, self.y = arc[0]
            return
        # bez_stack as a flat list of x and y; arc points at base index b (b..b+3)
        xs = [0] * 49
        ys = [0] * 49
        for i, (px, py) in enumerate(arc):
            xs[i], ys[i] = px, py
        b = 0
        half = ONE_PIXEL // 2
        while True:
            if (abs(2 * xs[b] - 3 * xs[b + 1] + xs[b + 3]) > half
                    or abs(2 * ys[b] - 3 * ys[b + 1] + ys[b + 3]) > half
                    or abs(xs[b] - 3 * xs[b + 2] + 2 * xs[b + 3]) > half
                    or abs(ys[b] - 3 * ys[b + 2] + 2 * ys[b + 3]) > half):
                for v in (xs, ys):
                    v[b + 6] = v[b + 3]
                    a = v[b] + v[b + 1]
                    bb = v[b + 1] + v[b + 2]
                    c = v[b + 2] + v[b + 3]
                    v[b + 5] = c >> 1
                    c += bb
                    v[b + 4] = c >> 2
                    v[b + 1] = a >> 1
                    a += bb
                    v[b + 2] = a >> 2
                    v[b + 3] = (a + c) >> 3
                b += 3
                continue
            self.render_line(xs[b], ys[b])
            if b == 0:
                return
            b -= 3

    def spans(self):
        """``gray_sweep_direct`` with the non-zero rule: yields (y, x, len, coverage)."""
        min_ex, max_ex = self.min_ex, self.max_ex
        for y in range(self.min_ey, self.max_ey):
            row = self.rows.get(y)
            if not row:
                continue
            x = min_ex
            cover = 0
            for cx in sorted(row):
                ccover, carea = row[cx]
                if cover != 0 and cx > x:
                    yield y, x, cx - x, _coverage(cover)
                cover = _int32(cover + ccover * (ONE_PIXEL * 2))
                area = _int32(cover - carea)
                if area != 0 and cx >= min_ex:
                    yield y, cx, 1, _coverage(area)
                x = cx + 1
            if cover != 0:
                yield y, x, max_ex - x, _coverage(cover)


def _coverage(area: int) -> int:
    """``FT_FILL_RULE`` for the non-zero rule, cast to unsigned char."""
    coverage = area >> (PIXEL_BITS * 2 + 1 - 8)
    if coverage < 0:
        coverage = ~coverage
    if coverage > 255:
        coverage = 255
    return coverage & 0xFF


def decompose(outline, raster: _Raster, dx: int = 0, dy: int = 0) -> None:
    """``FT_Outline_Decompose`` for ON/CUBIC outlines, every point shifted by (dx, dy)."""
    for points, tags in outline:
        if not points:
            continue
        if tags[0] != ON:
            raise ValueError("invalid outline")      # FreeType: Invalid_Outline
        sx, sy = points[0][0] + dx, points[0][1] + dy
        raster.move_to(sx, sy)
        i, n = 1, len(points)
        closed = False
        while i < n:
            if tags[i] == ON:
                raster.line_to(points[i][0] + dx, points[i][1] + dy)
                i += 1
            elif tags[i] == CUBIC:
                if i + 1 >= n or tags[i + 1] != CUBIC:
                    raise ValueError("invalid outline")
                c1 = (points[i][0] + dx, points[i][1] + dy)
                c2 = (points[i + 1][0] + dx, points[i + 1][1] + dy)
                if i + 2 < n:
                    raster.cubic_to(c1, c2, (points[i + 2][0] + dx, points[i + 2][1] + dy))
                    i += 3
                else:
                    raster.cubic_to(c1, c2, (sx, sy))
                    closed = True
                    break
            else:
                raise ValueError("conic outline")
        if not closed:
            raster.line_to(sx, sy)


def cbox(outline):
    xs = [p[0] for pts, _ in outline for p in pts]
    ys = [p[1] for pts, _ in outline for p in pts]
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


def render_lcd(outline, ppem: int = 64, mode: str | None = None):
    """``FT_Render_Glyph(FT_RENDER_MODE_LCD)`` on a transformed 26.6 outline.

    Returns ``(left, top, width, rows, pitch, buffer)`` (width in bytes = 3 per pixel, buffer rows
    top to bottom) or None where FreeType errs or makes an empty bitmap (PDFium draws nothing).
    """
    mode = mode or LCD_MODE
    n_points = sum(len(p) for p, _ in outline)
    if not n_points:
        return None
    x0, y0, x1, y1 = cbox(outline)
    pxmin, pymin, pxmax, pymax = x0 >> 6, y0 >> 6, x1 >> 6, y1 >> 6
    rxmin, rymin, rxmax, rymax = x0 & 63, y0 & 63, x1 & 63, y1 & 63
    if mode == SUBPIXEL:
        rxmin -= 43
        rxmax += 43
    else:
        rxmin -= max(LCD_GEOMETRY)
        rxmax -= min(LCD_GEOMETRY)
    pxmin += rxmin >> 6
    pymin += rymin >> 6
    pxmax += (rxmax + 63) >> 6
    pymax += (rymax + 63) >> 6
    left, top = pxmin, pymax
    width = pxmax - pxmin
    height = pymax - pymin
    if (width >= 0x10000 or height >= 0x10000 or pxmin < -0x1000000 or pxmax >= 0x1000000
            or pymin < -0x1000000 or pymax >= 0x1000000 or width > 16 * ppem or height > 16 * ppem):
        return None                                   # Raster_Overflow
    width *= 3
    pitch = (width + 3) & ~3
    rows = height
    if not rows or not pitch:
        return None
    buf = bytearray(rows * pitch)
    x_shift = -64 * left
    y_shift = -64 * top + 64 * rows
    if mode == SUBPIXEL:
        if width > 0x7FFF:
            return None
        imploded = [([((x + x_shift) * 3, y + y_shift) for x, y in pts], tags) for pts, tags in outline]
        raster = _Raster(0, 0, width, rows)
        decompose(imploded, raster)
        w = LCD_WEIGHTS
        size = len(buf)
        for y, x, length, cov in raster.spans():
            base = (rows - 1 - y) * pitch - 2
            adds = [(cov * wk + 85) >> 8 for wk in w]
            for px in range(x, x + length):
                d = base + px
                for k in range(5):
                    j = d + k
                    if 0 <= j < size:
                        buf[j] = (buf[j] + adds[k]) & 0xFF
    else:
        shifted = [([(x + x_shift, y + y_shift) for x, y in pts], tags) for pts, tags in outline]
        for i, sub in enumerate(LCD_GEOMETRY):
            raster = _Raster(0, 0, width // 3, rows)
            decompose(shifted, raster, -sub, 0)
            for y, x, length, cov in raster.spans():
                base = (rows - 1 - y) * pitch + i
                for px in range(x, x + length):
                    buf[base + px * 3] = cov
    return left, top, width, rows, pitch, bytes(buf)
