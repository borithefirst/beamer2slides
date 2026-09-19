"""FreeType's TrueType bytecode interpreter (truetype/ttinterp.c, 2.14.3) as PDFium runs it.

v40 ("minimal subpixel hinting", the default): backward compatibility mode unless the font's prep
signs the INSTCTRL waiver, no x moves while it is on, no y moves after both IUPs. Pedantic hinting
(RenderGlyph passes FT_LOAD_PEDANTIC): every out-of-range reference is an error, and a glyph whose
program errs is loaded again unhinted by PDFium. Sizes are 64 ppem with square pixels, so the CVT
needs no stretching and MPPEM is 64. Arithmetic is on 32-bit FT_Long (Windows), as in C.

`Exec` is the execution context; `truetype.py` holds the size (fpgm / prep state) and the loader.
"""
from __future__ import annotations

from .ftoutline import Unported, i32, mulfix, divfix

TOUCH_X, TOUCH_Y = 0x08, 0x10
MAX_RUNNABLE = 1000000
CALL_SIZE = 32
FONT, CVT, GLYPH = 1, 2, 3

POP_PUSH = ((0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (0,2), (0,2), (0,0), (5,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (0,0), (0,0), (1,0), (0,0), (1,0), (1,0), (1,0), (1,0), (1,2), (1,0), (0,0), (2,2), (0,1), (1,1), (1,0), (2,0), (0,0), (1,0), (2,0), (1,0), (1,0), (0,0), (1,0), (1,0), (0,0), (0,0), (0,0), (0,0), (1,0), (1,0), (1,0), (1,0), (1,0), (0,0), (2,0), (2,0), (0,0), (0,0), (2,0), (2,0), (0,0), (0,0), (2,0), (1,1), (2,0), (1,1), (1,1), (1,1), (2,0), (2,1), (2,1), (0,1), (0,1), (0,0), (0,0), (1,0), (2,1), (2,1), (2,1), (2,1), (2,1), (2,1), (1,1), (1,1), (1,0), (0,0), (2,1), (2,1), (1,1), (1,0), (1,0), (1,0), (2,1), (2,1), (2,1), (2,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (1,1), (2,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (2,0), (2,0), (0,0), (0,0), (0,0), (0,0), (1,0), (1,0), (0,0), (2,0), (2,0), (0,0), (0,0), (1,0), (2,0), (2,0), (1,1), (1,0), (3,3), (2,1), (2,1), (1,0), (2,0), (0,0), (0,0), (0,0), (0,1), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,0), (0,1), (0,2), (0,3), (0,4), (0,5), (0,6), (0,7), (0,8), (0,1), (0,2), (0,3), (0,4), (0,5), (0,6), (0,7), (0,8), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (1,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0), (2,0))
LENGTH = (1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 3, 5, 7, 9, 11, 13, 15, 17, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)
UNKNOWN_OPS = frozenset([0x28, 0x7B, 0x83, 0x84, 0x8F, 0x90, 0x91, 0x92] + list(range(0x93, 0xB0)))


# ------------------------------------------------------------------ arithmetic (32-bit longs)


def cdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def muldiv(a: int, b: int, c: int) -> int:
    """FT_MulDiv: rounded, on magnitudes; 0x7FFFFFFF when c is 0."""
    s = 1
    if a < 0:
        a, s = -a, -s
    if b < 0:
        b, s = -b, -s
    if c < 0:
        c, s = -c, -s
    d = i32((a * b + (c >> 1)) // c) if c > 0 else 0x7FFFFFFF
    return i32(-d) if s < 0 else d


def muldiv_no_round(a: int, b: int, c: int) -> int:
    s = 1
    if a < 0:
        a, s = -a, -s
    if b < 0:
        b, s = -b, -s
    if c < 0:
        c, s = -c, -s
    d = i32(a * b // c) if c > 0 else 0x7FFFFFFF
    return i32(-d) if s < 0 else d


def mulfix14(a: int, b: int) -> int:
    ab = a * b
    return i32((ab + 0x2000 + (-1 if ab < 0 else 0)) >> 14)


def dotfix14(ax: int, ay: int, bx: int, by: int) -> int:
    t = ax * bx + ay * by
    return i32((t + 0x2000 + (-1 if t < 0 else 0)) >> 14)


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def bounds(x: int, n: int) -> bool:
    """BOUNDS / BOUNDSL: unsigned x >= n."""
    return _u32(x) >= n


def norm_len(x_: int, y_: int) -> tuple[int, int]:
    """FT_Vector_NormLen (ftcalc.c) on a non-zero vector: (x, y) of length 0x10000."""
    sx = sy = 1
    x, y = _u32(x_), _u32(y_)
    if x_ < 0:
        x, sx = _u32(-x_), -1
    if y_ < 0:
        y, sy = _u32(-y_), -1
    if x == 0:
        return x_, (sy * 0x10000 if y > 0 else y_)
    if y == 0:
        return (sx * 0x10000 if x > 0 else x_), y_
    l = x + (y >> 1) if x > y else y + (x >> 1)
    shift = 31 - (l.bit_length() - 1)
    shift -= 15 + (1 if l >= (0xAAAAAAAA >> shift) else 0)
    if shift > 0:
        x, y = _u32(x << shift), _u32(y << shift)
        l = _u32(x + (y >> 1)) if x > y else _u32(y + (x >> 1))
    else:
        x, y, l = x >> -shift, y >> -shift, l >> -shift
    b = i32(0x10000 - l)
    xs, ys = i32(x), i32(y)
    while True:
        u = _u32(xs + (i32(xs * b) >> 16))
        v = _u32(ys + (i32(ys * b) >> 16))
        z = cdiv(-i32(u * u + v * v), 0x200)
        z = cdiv(i32(z * ((0x10000 + b) >> 8)), 0x10000)
        b = i32(b + z)
        if not z > 0:
            break
    return (-u if sx < 0 else u), (-v if sy < 0 else v)


def _short(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def normalize(vx: int, vy: int, old: tuple[int, int]) -> tuple[int, int]:
    if vx == 0 and vy == 0:
        return old
    x, y = norm_len(vx, vy)
    return _short(cdiv(x, 4)), _short(cdiv(y, 4))


# ------------------------------------------------------------------ state


class Zone:
    """TT_GlyphZoneRec: lists of [x, y] shared by copies, n_points per copy."""

    def __init__(self, n_points=0, org=None, cur=None, orus=None, tags=None, contours=None, first_point=0):
        self.n_points = n_points
        self.org = org if org is not None else []
        self.cur = cur if cur is not None else []
        self.orus = orus if orus is not None else []
        self.tags = tags if tags is not None else []
        self.contours = contours if contours is not None else []
        self.n_contours = len(self.contours)
        self.first_point = first_point

    def copy(self) -> "Zone":
        z = Zone.__new__(Zone)
        z.__dict__.update(self.__dict__)
        return z


GS_SAVED = ("minimum_distance", "control_value_cutin", "single_width_cutin", "single_width_value",
            "delta_base", "delta_shift", "auto_flip", "instruct_control", "scan_control", "scan_type")


class GS:
    def __init__(self):
        self.rp0 = self.rp1 = self.rp2 = 0
        self.gep0 = self.gep1 = self.gep2 = 1
        self.dual = self.proj = self.free = (0x4000, 0)
        self.loop = 1
        self.round_state = 1
        self.minimum_distance = 64
        self.control_value_cutin = 68
        self.single_width_cutin = 0
        self.single_width_value = 0
        self.delta_base = 9
        self.delta_shift = 3
        self.auto_flip = True
        self.instruct_control = 0
        self.scan_control = False
        self.scan_type = 0

    def copy(self) -> "GS":
        g = GS.__new__(GS)
        g.__dict__.update(self.__dict__)
        return g


class Def:
    __slots__ = ("range", "start", "end", "opc", "active")

    def __init__(self):
        self.range = self.start = self.end = self.opc = 0
        self.active = False


class TTError(Exception):
    pass


class Exec:
    """TT_ExecContextRec plus the size's function/instruction definitions."""

    def __init__(self, maxp: dict, cvt_size: int, scale: int, num_glyphs: int, codes: dict):
        max_stack = maxp.get("maxStackElements", 0)
        self.stack_size = max_stack + max(max_stack // 2, 128)
        self.stack = [0] * (self.stack_size + 1)
        self.store_size = maxp.get("maxStorage", 0)
        self.max_fdefs = maxp.get("maxFunctionDefs", 0)
        self.max_idefs = maxp.get("maxInstructionDefs", 0)
        self.fdefs = [Def() for _ in range(self.max_fdefs)]
        self.idefs = [Def() for _ in range(self.max_idefs)]
        self.num_fdefs = self.num_idefs = self.max_func = self.max_ins = 0
        self.cvt_size = cvt_size
        self.scale = scale                  # tt_metrics.scale
        self.x_scale = scale                # metrics.x_scale (0x10000 for a composite's program)
        self.num_glyphs = num_glyphs
        self.codes = codes                  # range -> bytes
        self.pedantic = True
        self.bc = 0
        self.is_composite = False
        self.gs = GS()
        self.twilight = Zone()
        self.pts = Zone()
        self.cvt = self.cvt_base = [0] * cvt_size
        self.storage = self.storage_base = [0] * self.store_size

    # -- TT_Load_Context / TT_Run_Context
    def load_context(self, twilight: Zone) -> None:
        self.cvt = self.cvt_base
        self.storage = self.storage_base
        self.twilight = twilight.copy()

    def run_context(self, rng: int, pts: Zone, size_gs: GS) -> str | None:
        """TT_Run_Context: returns the error (None when the program ran to its end)."""
        self.pts = pts
        self.zp0 = self.zp1 = self.zp2 = pts
        limit = max(30, 2 * (pts.n_points + self.cvt_size))
        if self.twilight.n_points > limit:
            self.twilight.n_points = min(limit, 0xFFFF)
        if pts.n_points:
            mx = max(50, 10 * pts.n_points) + max(50, self.cvt_size // 10)
        else:
            mx = 300 + 22 * self.cvt_size
        mx = min(mx, 100 * self.num_glyphs)
        self.loopcall_max = self.neg_jump_max = mx
        self.loopcall_counter = self.neg_jump_counter = 0
        self.gs = size_gs.copy()
        self.compute_funcs()
        self.bc &= ~3
        self.top = 0
        self.call_stack = []
        self.ini_range = rng
        self.cur_range = rng
        self.code = self.codes[rng]
        self.ip = 0
        return self.run()

    # -- projections and moves
    def compute_funcs(self) -> None:
        g = self.gs
        px, py = g.proj
        fx, fy = g.free
        f = (px * fx + py * fy + 0x2000) >> 14
        if f >= 0x3FFE:
            self.mv = (fx * 4, fy * 4)
        elif -0x400 < f < 0x400:
            self.mv = (0, 0)
        else:
            self.mv = (cdiv(fx * 0x10000, f), cdiv(fy * 0x10000, f))
        if f >= 0x3FFE and fx == 0x4000:
            self.move_kind = 1
        elif f >= 0x3FFE and fy == 0x4000:
            self.move_kind = 2
        else:
            self.move_kind = 0

    def project(self, dx: int, dy: int) -> int:
        px, py = self.gs.proj
        if px == 0x4000:
            return dx
        if py == 0x4000:
            return dy
        return dotfix14(dx, dy, px, py)

    def dualproj(self, dx: int, dy: int) -> int:
        px, py = self.gs.dual
        if px == 0x4000:
            return dx
        if py == 0x4000:
            return dy
        return dotfix14(dx, dy, px, py)

    def proj_pts(self, a, b) -> int:
        return self.project(i32(a[0] - b[0]), i32(a[1] - b[1]))

    def dual_pts(self, a, b) -> int:
        return self.dualproj(i32(a[0] - b[0]), i32(a[1] - b[1]))

    def move(self, zone: Zone, p: int, d: int) -> None:
        bc = self.bc
        pt = zone.cur[p]
        if self.move_kind == 1:
            if not bc:
                pt[0] = i32(pt[0] + d)
            zone.tags[p] |= TOUCH_X
            return
        if self.move_kind == 2:
            if bc != 7:
                pt[1] = i32(pt[1] + d)
            zone.tags[p] |= TOUCH_Y
            return
        vx, vy = self.mv
        if vx != 0:
            if not bc:
                pt[0] = i32(pt[0] + mulfix(d, vx))
            zone.tags[p] |= TOUCH_X
        if vy != 0:
            if bc != 7:
                pt[1] = i32(pt[1] + mulfix(d, vy))
            zone.tags[p] |= TOUCH_Y

    def move_orig(self, zone: Zone, p: int, d: int) -> None:
        pt = zone.org[p]
        if self.move_kind == 1:
            pt[0] = i32(pt[0] + d)
        elif self.move_kind == 2:
            pt[1] = i32(pt[1] + d)
        else:
            vx, vy = self.mv
            if vx != 0:
                pt[0] = i32(pt[0] + mulfix(d, vx))
            if vy != 0:
                pt[1] = i32(pt[1] + mulfix(d, vy))

    def move_zp2(self, p: int, dx: int, dy: int) -> None:
        z = self.zp2
        if self.gs.free[0] != 0:
            if not self.bc:
                z.cur[p][0] = i32(z.cur[p][0] + dx)
            z.tags[p] |= TOUCH_X
        if self.gs.free[1] != 0:
            if self.bc != 7:
                z.cur[p][1] = i32(z.cur[p][1] + dy)
            z.tags[p] |= TOUCH_Y

    # -- rounding
    def round(self, d: int, state: int | None = None) -> int:
        s = self.gs.round_state if state is None else state
        if s == 1:                                  # Round_To_Grid
            if d >= 0:
                v = i32(d + 32) & -64
                return 0 if v < 0 else v
            v = i32(-(i32(32 - d) & -64))
            return 0 if v > 0 else v
        if s == 0:                                  # Round_To_Half_Grid
            if d >= 0:
                v = i32((d & -64) + 32)
                return 32 if v < 0 else v
            v = i32(-((i32(-d) & -64) + 32))
            return -32 if v > 0 else v
        if s == 2:                                  # Round_To_Double_Grid
            if d >= 0:
                v = i32(d + 16) & -32
                return 0 if v < 0 else v
            v = i32(-(i32(16 - d) & -32))
            return 0 if v > 0 else v
        if s == 3:                                  # Round_Up_To_Grid
            if d >= 0:
                v = i32(d + 63) & -64
                return 0 if v < 0 else v
            v = i32(-(i32(63 - d) & -64))
            return 0 if v > 0 else v
        if s == 4:                                  # Round_Down_To_Grid
            if d >= 0:
                v = d & -64
                return 0 if v < 0 else v
            v = i32(-(i32(-d) & -64))
            return 0 if v > 0 else v
        if s == 5:                                  # Round_None
            return d
        per, ph, thr = self.period, self.phase, self.threshold
        if s == 6:                                  # Round_Super
            if d >= 0:
                v = i32((i32(d + (thr - ph)) & -per) + ph)
                return ph if v < 0 else v
            v = i32(-(i32((thr - ph) - d) & -per) - ph)
            return -ph if v > 0 else v
        if d >= 0:                                  # Round_Super_45
            v = i32(cdiv(i32(d + (thr - ph)), per) * per + ph)
            return ph if v < 0 else v
        v = i32(-(cdiv(i32((thr - ph) - d), per) * per) - ph)
        return -ph if v > 0 else v

    def set_super_round(self, gp: int, sel: int) -> None:
        k = sel & 0xC0
        per = gp // 2 if k == 0 else 2 * gp if k == 0x80 else gp
        k = sel & 0x30
        ph = 0 if k == 0 else per // 4 if k == 0x10 else per // 2 if k == 0x20 else per * 3 // 4
        if sel & 0xF == 0:
            thr = per - 1
        else:
            thr = cdiv(((sel & 0xF) - 4) * per, 8)
        self.period, self.phase, self.threshold = per >> 8, ph >> 8, thr >> 8

    # -- the loop
    def fail(self, err: str) -> str:
        self.error = err
        return err

    def goto_range(self, rng: int, ip: int) -> None:
        if not 1 <= rng <= 3:
            self.error = "Bad_Argument"
            return
        code = self.codes.get(rng)
        if not code:
            self.error = "Invalid_CodeRange"
            return
        if ip > len(code):
            self.error = "Code_Overflow"
            return
        self.code, self.ip, self.cur_range = code, ip, rng
        self.length = 0

    def skip_code(self) -> bool:
        self.ip += self.length
        code = self.code
        if self.ip < len(code):
            self.opcode = code[self.ip]
            self.length = LENGTH[self.opcode]
            if self.length < 0:
                if self.ip + 1 >= len(code):
                    self.error = "Code_Overflow"
                    return False
                self.length = 2 - self.length * code[self.ip + 1]
            if self.ip + self.length <= len(code):
                return True
        self.error = "Code_Overflow"
        return False

    def run(self) -> str | None:
        """TT_RunIns."""
        if not self.code:
            return None
        count = 0
        st = self.stack
        while True:
            count += 1
            if count > MAX_RUNNABLE:
                return "Execution_Too_Long"
            self.error = None
            op = self.opcode = self.code[self.ip]
            self.length = 1
            pops, pushes = POP_PUSH[op]
            a = self.top - pops
            if a < 0:
                return "Too_Few_Arguments"
            self.args = a
            self.new_top = a + pushes
            if self.new_top > self.stack_size:
                return "Stack_Overflow"
            h = _DISPATCH[op]
            h(self, a, st)
            if self.error:
                return self.error
            self.top = self.new_top
            self.ip += self.length
            if self.ip >= len(self.code):
                if self.call_stack:
                    return "Code_Overflow"
                return None


# ------------------------------------------------------------------ instructions


def _svtca(e, a, st):
    op = e.opcode
    aa = (op & 1) << 14
    v = (aa, aa ^ 0x4000)
    if op < 4:
        e.gs.proj = e.gs.dual = v
    if op & 2 == 0:
        e.gs.free = v
    e.compute_funcs()


def _sxvtl(e, i1, i2, opc, old):
    if bounds(i1, e.zp2.n_points) or bounds(i2, e.zp1.n_points):
        e.error = "Invalid_Reference"
        return None
    p1, p2 = e.zp1.cur[i2], e.zp2.cur[i1]
    A, B = i32(p1[0] - p2[0]), i32(p1[1] - p2[1])
    if A == 0 and B == 0:
        A, opc = 0x4000, 0
    if opc & 1:
        A, B = i32(-B), A
    return normalize(A, B, old)


def _spvtl(e, a, st):
    v = _sxvtl(e, st[a + 1] & 0xFFFF, st[a] & 0xFFFF, e.opcode, e.gs.proj)
    if v is not None:
        e.gs.proj = e.gs.dual = v
        e.compute_funcs()


def _sfvtl(e, a, st):
    v = _sxvtl(e, st[a + 1] & 0xFFFF, st[a] & 0xFFFF, e.opcode, e.gs.free)
    if v is not None:
        e.gs.free = v
        e.compute_funcs()


def _spvfs(e, a, st):
    e.gs.proj = e.gs.dual = normalize(_short(st[a]), _short(st[a + 1]), e.gs.proj)
    e.compute_funcs()


def _sfvfs(e, a, st):
    e.gs.free = normalize(_short(st[a]), _short(st[a + 1]), e.gs.free)
    e.compute_funcs()


def _gpv(e, a, st):
    st[a], st[a + 1] = e.gs.proj


def _gfv(e, a, st):
    st[a], st[a + 1] = e.gs.free


def _sfvtpv(e, a, st):
    e.gs.free = e.gs.proj
    e.compute_funcs()


def _isect(e, a, st):
    point, a0, a1, b0, b1 = st[a], st[a + 1], st[a + 2], st[a + 3], st[a + 4]
    z0, z1, z2 = e.zp0, e.zp1, e.zp2
    if (bounds(b0, z0.n_points) or bounds(b1, z0.n_points) or bounds(a0, z1.n_points)
            or bounds(a1, z1.n_points) or bounds(point, z2.n_points)):
        e.error = "Invalid_Reference"
        return
    B0, B1, A0, A1 = z0.cur[b0], z0.cur[b1], z1.cur[a0], z1.cur[a1]
    dbx, dby = i32(B1[0] - B0[0]), i32(B1[1] - B0[1])
    dax, day = i32(A1[0] - A0[0]), i32(A1[1] - A0[1])
    dx, dy = i32(B0[0] - A0[0]), i32(B0[1] - A0[1])
    disc = i32(muldiv(dax, i32(-dby), 0x40) + muldiv(day, dbx, 0x40))
    dot = i32(muldiv(dax, dbx, 0x40) + muldiv(day, dby, 0x40))
    P = z2.cur[point]
    if i32(19 * abs(disc)) > abs(dot):
        val = i32(muldiv(dx, i32(-dby), 0x40) + muldiv(dy, dbx, 0x40))
        P[0] = i32(A0[0] + muldiv(val, dax, disc))
        P[1] = i32(A0[1] + muldiv(val, day, disc))
    else:
        P[0] = cdiv(i32(i32(A0[0] + A1[0]) + i32(B0[0] + B1[0])), 4)
        P[1] = cdiv(i32(i32(A0[1] + A1[1]) + i32(B0[1] + B1[1])), 4)
    z2.tags[point] |= TOUCH_X | TOUCH_Y


def _srp(e, a, st):
    v = st[a] & 0xFFFF
    setattr(e.gs, ("rp0", "rp1", "rp2")[e.opcode - 0x10], v)


def _szp(e, a, st):
    v = st[a]
    if v == 0:
        z = e.twilight
    elif v == 1:
        z = e.pts
    else:
        e.error = "Invalid_Reference"
        return
    k = e.opcode - 0x13
    if k == 0 or k == 3:
        e.zp0, e.gs.gep0 = z, v
    if k == 1 or k == 3:
        e.zp1, e.gs.gep1 = z, v
    if k == 2 or k == 3:
        e.zp2, e.gs.gep2 = z, v


def _sloop(e, a, st):
    if st[a] < 0:
        e.error = "Bad_Argument"
    else:
        e.gs.loop = min(st[a], 0xFFFF)


def _round_state(e, a, st):
    e.gs.round_state = {0x18: 1, 0x19: 0, 0x3D: 2, 0x7C: 3, 0x7D: 4, 0x7A: 5}[e.opcode]


def _smd(e, a, st):
    e.gs.minimum_distance = st[a]


def _else(e, a, st):
    n = 1
    while True:
        if not e.skip_code():
            return
        if e.opcode == 0x58:
            n += 1
        elif e.opcode == 0x59:
            n -= 1
        if n == 0:
            return


def _jmpr(e, a, st):
    off = st[a]
    if off == 0 and e.args == 0:
        e.error = "Bad_Argument"
        return
    e.ip = i32(e.ip + off)
    if e.ip < 0 or (e.call_stack and e.ip > e.call_stack[-1][3].end):
        e.error = "Bad_Argument"
        return
    e.length = 0
    if off < 0:
        e.neg_jump_counter += 1
        if e.neg_jump_counter > e.neg_jump_max:
            e.error = "Execution_Too_Long"


def _scvtci(e, a, st):
    e.gs.control_value_cutin = st[a]


def _sswci(e, a, st):
    e.gs.single_width_cutin = st[a]


def _ssw(e, a, st):
    e.gs.single_width_value = mulfix(st[a], e.scale)


def _dup(e, a, st):
    st[a + 1] = st[a]


def _pop(e, a, st):
    pass


def _clear(e, a, st):
    e.new_top = 0


def _swap(e, a, st):
    st[a], st[a + 1] = st[a + 1], st[a]


def _depth(e, a, st):
    st[a] = e.top


def _cindex(e, a, st):
    L = st[a]
    if L <= 0 or L > e.args:
        e.error = "Invalid_Reference"
        st[a] = 0
    else:
        st[a] = st[e.args - L]


def _mindex(e, a, st):
    L = st[a]
    if L <= 0 or L > e.args:
        e.error = "Invalid_Reference"
        return
    base = e.args - L
    k = st[base]
    st[base:e.args - 1] = st[base + 1:e.args]
    st[e.args - 1] = k


def _alignpts(e, a, st):
    p1, p2 = st[a] & 0xFFFF, st[a + 1] & 0xFFFF
    if bounds(p1, e.zp1.n_points) or bounds(p2, e.zp0.n_points):
        e.error = "Invalid_Reference"
        return
    d = cdiv(e.proj_pts(e.zp0.cur[p2], e.zp1.cur[p1]), 2)
    e.move(e.zp1, p1, d)
    e.move(e.zp0, p2, i32(-d))


def _utp(e, a, st):
    p = st[a]
    if bounds(p, e.zp0.n_points):
        e.error = "Invalid_Reference"
        return
    mask = 0xFF
    if e.gs.free[0] != 0:
        mask &= ~TOUCH_X
    if e.gs.free[1] != 0:
        mask &= ~TOUCH_Y
    e.zp0.tags[p] &= mask


def _find_fdef(e, f):
    if bounds(f, e.max_func + 1):
        return None
    d = e.fdefs[f] if f < len(e.fdefs) else None
    if e.max_func + 1 != e.num_fdefs or d is None or d.opc != f:
        d = None
        for r in e.fdefs[:e.num_fdefs]:
            if r.opc == f:
                d = r
                break
        if d is None:
            return None
    return d if d.active else None


def _call_def(e, d, count):
    if len(e.call_stack) >= CALL_SIZE:
        e.error = "Stack_Overflow"
        return False
    e.call_stack.append([e.cur_range, e.ip + 1, count, d])
    e.goto_range(d.range, d.start)
    e.length = 0
    return True


def _loopcall(e, a, st):
    d = _find_fdef(e, _u32(st[a + 1]))
    if d is None:
        e.error = "Invalid_Reference"
        return
    if len(e.call_stack) >= CALL_SIZE:
        e.error = "Stack_Overflow"
        return
    if st[a] > 0:
        _call_def(e, d, st[a])
        e.loopcall_counter += st[a]
        if e.loopcall_counter > e.loopcall_max:
            e.error = "Execution_Too_Long"


def _call(e, a, st):
    d = _find_fdef(e, _u32(st[a]))
    if d is None:
        e.error = "Invalid_Reference"
        return
    _call_def(e, d, 1)


def _skip_def(e, d):
    while e.skip_code():
        if e.opcode in (0x89, 0x2C):
            e.error = "Nested_DEFS"
            return
        if e.opcode == 0x2D:
            d.end = e.ip
            return


def _fdef(e, a, st):
    if e.ini_range == GLYPH:
        e.error = "DEF_In_Glyf_Bytecode"
        return
    n = _u32(st[a])
    d = None
    for r in e.fdefs[:e.num_fdefs]:
        if r.opc == n:
            d = r
            break
    if d is None:
        if e.num_fdefs >= e.max_fdefs:
            e.error = "Too_Many_Function_Defs"
            return
        d = e.fdefs[e.num_fdefs]
        e.num_fdefs += 1
    if n > 0xFFFF:
        e.error = "Too_Many_Function_Defs"
        return
    d.range, d.opc, d.start, d.active = e.cur_range, n, e.ip + 1, True
    if n > e.max_func:
        e.max_func = n
    _skip_def(e, d)


def _endf(e, a, st):
    if not e.call_stack:
        e.error = "ENDF_In_Exec_Stream"
        return
    rec = e.call_stack.pop()
    rec[2] -= 1
    e.length = 0
    if rec[2] > 0:
        e.call_stack.append(rec)
        e.ip = rec[3].start
    else:
        e.goto_range(rec[0], rec[1])


def _idef(e, a, st):
    if e.ini_range == GLYPH:
        e.error = "DEF_In_Glyf_Bytecode"
        return
    n = st[a]
    d = None
    for r in e.idefs[:e.num_idefs]:
        if r.opc == _u32(n):
            d = r
            break
    if d is None:
        if e.num_idefs >= e.max_idefs:
            e.error = "Too_Many_Instruction_Defs"
            return
        d = e.idefs[e.num_idefs]
        e.num_idefs += 1
    if not 0 <= n <= 0xFF:
        e.error = "Too_Many_Instruction_Defs"
        return
    d.opc, d.start, d.range, d.active = n, e.ip + 1, e.cur_range, True
    if n > e.max_ins:
        e.max_ins = n
    _skip_def(e, d)


def _unknown(e, a, st):
    op = e.opcode
    for d in e.idefs[:e.num_idefs]:
        if (d.opc & 0xFF) == op and d.active:
            if op in (0x91, 0x92) or POP_PUSH[op][1]:
                raise Unported(f"TrueType opcode {op:#x} defined by IDEF")
            _call_def(e, d, 1)
            return
    if op in (0x91, 0x92) or POP_PUSH[op][1]:
        raise Unported(f"TrueType opcode {op:#x}")
    e.error = "Invalid_Opcode"


def _mdap(e, a, st):
    p = st[a] & 0xFFFF
    if bounds(p, e.zp0.n_points):
        e.error = "Invalid_Reference"
        return
    if e.opcode & 1:
        c = e.zp0.cur[p]
        cur = e.project(c[0], c[1])
        d = i32(e.round(cur) - cur)
    else:
        d = 0
    e.move(e.zp0, p, d)
    e.gs.rp0 = e.gs.rp1 = p


def _iup(e, a, st):
    z = e.pts
    if z.n_contours == 0:
        return
    k = 0 if e.opcode & 1 else 1
    mask = TOUCH_X if k == 0 else TOUCH_Y
    if e.bc == 7:
        return
    if e.bc:
        e.bc |= 1 << (e.opcode & 1)
    org, cur, orus, tags = z.org, z.cur, z.orus, z.tags
    n = z.n_points

    def shift(p1, p2, p):
        dx = i32(cur[p][k] - org[p][k])
        if dx != 0:
            for i in range(p1, p):
                cur[i][k] = i32(cur[i][k] + dx)
            for i in range(p + 1, p2 + 1):
                cur[i][k] = i32(cur[i][k] + dx)

    def interp(p1, p2, r1, r2):
        if p1 > p2 or r1 >= n or r2 >= n:
            return
        o1, o2 = orus[r1][k], orus[r2][k]
        if o1 > o2:
            o1, o2, r1, r2 = o2, o1, r2, r1
        g1, g2 = org[r1][k], org[r2][k]
        c1, c2 = cur[r1][k], cur[r2][k]
        d1, d2 = i32(c1 - g1), i32(c2 - g2)
        if c1 == c2 or o1 == o2:
            for i in range(p1, p2 + 1):
                x = org[i][k]
                cur[i][k] = i32(x + d1) if x <= g1 else i32(x + d2) if x >= g2 else c1
            return
        scale = None
        for i in range(p1, p2 + 1):
            x = org[i][k]
            if x <= g1:
                x = i32(x + d1)
            elif x >= g2:
                x = i32(x + d2)
            else:
                if scale is None:
                    scale = divfix(i32(c2 - c1), i32(o2 - o1))
                x = i32(c1 + mulfix(i32(orus[i][k] - o1), scale))
            cur[i][k] = x

    point = 0
    for contour in range(z.n_contours):
        end = _u32(z.contours[contour] - z.first_point)
        first = point
        if end >= n:
            end = n - 1
        while point <= end and not tags[point] & mask:
            point += 1
        if point <= end:
            first_touched = cur_touched = point
            point += 1
            while point <= end:
                if tags[point] & mask:
                    interp(cur_touched + 1, point - 1, cur_touched, point)
                    cur_touched = point
                point += 1
            if cur_touched == first_touched:
                shift(first, end, cur_touched)
            else:
                interp(cur_touched + 1, end, cur_touched, first_touched)
                if first_touched > 0:
                    interp(first, first_touched - 1, cur_touched, first_touched)


def _displacement(e, cur):
    if e.opcode & 1:
        z, p = e.zp0, e.gs.rp1
    else:
        z, p = e.zp1, e.gs.rp2
    if bounds(p, z.n_points):
        e.error = "Invalid_Reference"
        return None
    refp = p if cur is z.cur else -1
    d = e.proj_pts(z.cur[p], z.org[p])
    return mulfix(d, e.mv[0]), mulfix(d, e.mv[1]), refp


def _shp(e, a, st):
    loop = e.gs.loop
    if e.new_top < loop:
        e.error = "Too_Few_Arguments"
        e.gs.loop = 1
        return
    e.new_top -= loop
    r = _displacement(e, None)
    if r is None:
        return
    dx, dy, _ = r
    args = a
    while loop:
        loop -= 1
        args -= 1
        p = _u32(st[args])
        if p >= e.zp2.n_points:
            e.error = "Invalid_Reference"
            return
        e.move_zp2(p, dx, dy)
    e.gs.loop = 1


def _shc(e, a, st):
    contour = st[a] & 0xFFFF
    bnd = 1 if e.gs.gep2 == 0 else e.zp2.n_contours
    if contour >= bnd:
        e.error = "Invalid_Reference"
        return
    r = _displacement(e, e.zp2.cur)
    if r is None:
        return
    dx, dy, refp = r
    z = e.zp2
    start = 0 if contour == 0 else _u32(z.contours[contour - 1] + 1 - z.first_point)
    limit = z.n_points if e.gs.gep2 == 0 else _u32(z.contours[contour] + 1 - z.first_point)
    for i in range(start, limit):
        if refp != i:
            e.move_zp2(i, dx, dy)


def _shz(e, a, st):
    v = st[a]
    if v == 0:
        cur, limit = e.twilight.cur, e.twilight.n_points
    elif v == 1:
        cur, limit = e.pts.cur, max(e.pts.n_points - 4, 0)
    else:
        e.error = "Invalid_Reference"
        return
    r = _displacement(e, cur)
    if r is None:
        return
    dx, dy, refp = r
    if dx and not e.bc:
        for i in range(limit):
            if refp != i:
                cur[i][0] = i32(cur[i][0] + dx)
    if dy and e.bc != 7:
        for i in range(limit):
            if refp != i:
                cur[i][1] = i32(cur[i][1] + dy)


def _shpix(e, a, st):
    loop = e.gs.loop
    g = e.gs
    tw = g.gep0 == 0 or g.gep1 == 0 or g.gep2 == 0
    if e.new_top < loop:
        e.error = "Too_Few_Arguments"
        g.loop = 1
        return
    e.new_top -= loop
    dx, dy = mulfix14(st[a], g.free[0]), mulfix14(st[a], g.free[1])
    args = a
    while loop:
        loop -= 1
        args -= 1
        p = _u32(st[args])
        if p >= e.zp2.n_points:
            e.error = "Invalid_Reference"
            return
        if e.bc:
            if tw or (e.bc != 7 and ((e.is_composite and g.free[1] != 0)
                                     or e.zp2.tags[p] & TOUCH_Y)):
                e.move_zp2(p, 0, dy)
        else:
            e.move_zp2(p, dx, dy)
    g.loop = 1


def _ip(e, a, st):
    g = e.gs
    loop = g.loop
    if e.new_top < loop:
        e.error = "Too_Few_Arguments"
        g.loop = 1
        return
    tw = g.gep0 == 0 or g.gep1 == 0 or g.gep2 == 0
    if bounds(g.rp1, e.zp0.n_points):
        e.error = "Invalid_Reference"
        g.loop = 1
        return
    base = e.zp0.org[g.rp1] if tw else e.zp0.orus[g.rp1]
    cur_base = e.zp0.cur[g.rp1]
    if bounds(g.rp2, e.zp1.n_points):
        old_range = cur_range = 0
    else:
        o = e.zp1.org[g.rp2] if tw else e.zp1.orus[g.rp2]
        old_range = e.dual_pts(o, base)
        cur_range = e.proj_pts(e.zp1.cur[g.rp2], cur_base)
    e.new_top -= loop
    args = a
    z = e.zp2
    while loop:
        loop -= 1
        args -= 1
        p = _u32(st[args])
        if p >= z.n_points:
            e.error = "Invalid_Reference"
            return
        org_dist = e.dual_pts(z.org[p] if tw else z.orus[p], base)
        cur_dist = e.proj_pts(z.cur[p], cur_base)
        if org_dist:
            new = muldiv(org_dist, cur_range, old_range) if old_range else org_dist
        else:
            new = 0
        e.move(z, p & 0xFFFF, i32(new - cur_dist))
    g.loop = 1


def _msirp(e, a, st):
    p = st[a] & 0xFFFF
    g = e.gs
    if bounds(p, e.zp1.n_points) or bounds(g.rp0, e.zp0.n_points):
        e.error = "Invalid_Reference"
        return
    if g.gep1 == 0:
        e.zp1.org[p][:] = e.zp0.org[g.rp0]
        e.move_orig(e.zp1, p, st[a + 1])
        e.zp1.cur[p][:] = e.zp1.org[p]
    d = e.proj_pts(e.zp1.cur[p], e.zp0.cur[g.rp0])
    e.move(e.zp1, p, i32(st[a + 1] - d))
    g.rp1 = g.rp0
    g.rp2 = p
    if e.opcode & 1:
        g.rp0 = p


def _alignrp(e, a, st):
    g = e.gs
    loop = g.loop
    if e.new_top < loop or bounds(g.rp0, e.zp0.n_points):
        e.error = "Invalid_Reference"
        g.loop = 1
        return
    e.new_top -= loop
    args = a
    while loop:
        loop -= 1
        args -= 1
        p = _u32(st[args])
        if p >= e.zp1.n_points:
            e.error = "Invalid_Reference"
            return
        d = e.proj_pts(e.zp1.cur[p], e.zp0.cur[g.rp0])
        e.move(e.zp1, p, i32(-d))
    g.loop = 1


def _miap(e, a, st):
    g = e.gs
    entry = _u32(st[a + 1])
    p = st[a] & 0xFFFF
    if bounds(p, e.zp0.n_points) or entry >= e.cvt_size:
        e.error = "Invalid_Reference"
    else:
        d = e.cvt[entry]
        z = e.zp0
        if g.gep0 == 0:
            z.org[p][:] = [mulfix14(d, g.free[0]), mulfix14(d, g.free[1])]
            z.cur[p][:] = z.org[p]
        c = z.cur[p]
        org_dist = e.project(c[0], c[1])
        if e.opcode & 1:
            delta = abs(i32(d - org_dist))
            if delta > g.control_value_cutin:
                d = org_dist
            d = e.round(d)
        e.move(z, p, i32(d - org_dist))
    g.rp0 = g.rp1 = p


def _orig_dist(e, zp_a, pa, zp_b, pb):
    g = e.gs
    if g.gep0 == 0 or g.gep1 == 0:
        return e.dual_pts(zp_a.org[pa], zp_b.org[pb])
    return mulfix(e.dual_pts(zp_a.orus[pa], zp_b.orus[pb]), e.x_scale)


def _mdrp(e, a, st):
    g = e.gs
    p = st[a] & 0xFFFF
    op = e.opcode
    if bounds(p, e.zp1.n_points) or bounds(g.rp0, e.zp0.n_points):
        e.error = "Invalid_Reference"
    else:
        org = _orig_dist(e, e.zp1, p, e.zp0, g.rp0)
        swv, swc = g.single_width_value, g.single_width_cutin
        if swc > 0 and swv - swc < org < swv + swc:
            org = swv if org >= 0 else -swv
        d = e.round(org) if op & 4 else org
        if op & 8:
            md = g.minimum_distance
            if org >= 0:
                if d < md:
                    d = md
            elif d > i32(-md):
                d = i32(-md)
        cur = e.proj_pts(e.zp1.cur[p], e.zp0.cur[g.rp0])
        e.move(e.zp1, p, i32(d - cur))
    g.rp1 = g.rp0
    g.rp2 = p
    if op & 16:
        g.rp0 = p


def _mirp(e, a, st):
    g = e.gs
    p = st[a] & 0xFFFF
    op = e.opcode
    entry = _u32(i32(st[a + 1] + 1))
    if bounds(p, e.zp1.n_points) or entry >= e.cvt_size + 1 or bounds(g.rp0, e.zp0.n_points):
        e.error = "Invalid_Reference"
    else:
        cvt = 0 if entry == 0 else e.cvt[entry - 1]
        if abs(cvt - g.single_width_value) < g.single_width_cutin:
            cvt = g.single_width_value if cvt >= 0 else -g.single_width_value
        z0, z1 = e.zp0, e.zp1
        if g.gep1 == 0:
            o = z0.org[g.rp0]
            z1.org[p][:] = [i32(o[0] + mulfix14(cvt, g.free[0])), i32(o[1] + mulfix14(cvt, g.free[1]))]
            z1.cur[p][:] = z1.org[p]
        org = e.dual_pts(z1.org[p], z0.org[g.rp0])
        cur = e.proj_pts(z1.cur[p], z0.cur[g.rp0])
        if g.auto_flip and (org ^ cvt) < 0:
            cvt = i32(-cvt)
        if op & 4:
            if g.gep0 == g.gep1:
                if abs(i32(cvt - org)) > g.control_value_cutin:
                    cvt = org
            d = e.round(cvt)
        else:
            d = cvt
        if op & 8:
            md = g.minimum_distance
            if org >= 0:
                if d < md:
                    d = md
            elif d > i32(-md):
                d = i32(-md)
        e.move(z1, p, i32(d - cur))
    g.rp1 = g.rp0
    if op & 16:
        g.rp0 = p
    g.rp2 = p


def _npush(e, a, st):
    code, ip = e.code, e.ip + 1
    if ip >= len(code):
        e.error = "Code_Overflow"
        return
    L = code[ip]
    word = e.opcode == 0x41
    if ip + (2 * L if word else L) >= len(code):
        e.error = "Code_Overflow"
        return
    if L >= e.stack_size + 1 - e.top:
        e.error = "Stack_Overflow"
        return
    for k in range(L):
        if word:
            st[a + k] = _short((code[ip + 1] << 8) | code[ip + 2])
            ip += 2
        else:
            ip += 1
            st[a + k] = code[ip]
    e.new_top += L
    e.ip = ip


def _push(e, a, st):
    op = e.opcode
    code, ip = e.code, e.ip
    word = op >= 0xB8
    L = op - (0xB8 if word else 0xB0) + 1
    if ip + (2 * L if word else L) >= len(code):
        e.error = "Code_Overflow"
        return
    for k in range(L):
        if word:
            st[a + k] = _short((code[ip + 1] << 8) | code[ip + 2])
            ip += 2
        else:
            ip += 1
            st[a + k] = code[ip]
    e.ip = ip


def _ws(e, a, st):
    i = _u32(st[a])
    if i >= e.store_size:
        e.error = "Invalid_Reference"
        return
    if e.ini_range == GLYPH and e.storage is e.storage_base:
        e.storage = list(e.storage_base)
    e.storage[i] = st[a + 1]


def _rs(e, a, st):
    i = _u32(st[a])
    if i >= e.store_size:
        e.error = "Invalid_Reference"
        return
    st[a] = e.storage[i]


def _write_cvt(e, i, v):
    if e.ini_range == GLYPH and e.cvt is e.cvt_base:
        e.cvt = list(e.cvt_base)
    e.cvt[i] = v


def _wcvtp(e, a, st):
    i = _u32(st[a])
    if i >= e.cvt_size:
        e.error = "Invalid_Reference"
        return
    _write_cvt(e, i, st[a + 1])


def _wcvtf(e, a, st):
    i = _u32(st[a])
    if i >= e.cvt_size:
        e.error = "Invalid_Reference"
        return
    _write_cvt(e, i, mulfix(st[a + 1], e.scale))


def _rcvt(e, a, st):
    i = _u32(st[a])
    if i >= e.cvt_size:
        e.error = "Invalid_Reference"
        return
    st[a] = e.cvt[i]


def _gc(e, a, st):
    L = st[a]
    z = e.zp2
    if bounds(L, z.n_points):
        e.error = "Invalid_Reference"
        st[a] = 0
        return
    if e.opcode & 1:
        v = z.org[L]
        st[a] = e.dualproj(v[0], v[1])
    else:
        v = z.cur[L]
        st[a] = e.project(v[0], v[1])


def _scfs(e, a, st):
    L = st[a] & 0xFFFF
    z = e.zp2
    if bounds(L, z.n_points):
        e.error = "Invalid_Reference"
        return
    c = z.cur[L]
    k = e.project(c[0], c[1])
    e.move(z, L, i32(st[a + 1] - k))
    if e.gs.gep2 == 0:
        z.org[L][:] = z.cur[L]


def _md(e, a, st):
    K, L = st[a + 1] & 0xFFFF, st[a] & 0xFFFF
    if bounds(L, e.zp0.n_points) or bounds(K, e.zp1.n_points):
        e.error = "Invalid_Reference"
        d = 0
    elif e.opcode & 1:
        d = e.proj_pts(e.zp0.cur[L], e.zp1.cur[K])
    else:
        d = _orig_dist(e, e.zp0, L, e.zp1, K)
    st[a] = d


def _mppem(e, a, st):
    st[a] = 64


def _mps(e, a, st):
    st[a] = 4096


def _flipon(e, a, st):
    e.gs.auto_flip = e.opcode == 0x4D


def _debug(e, a, st):
    e.error = "Debug_OpCode"


def _cmp(e, a, st):
    x, y = st[a], st[a + 1]
    op = e.opcode
    st[a] = int(x < y if op == 0x50 else x <= y if op == 0x51 else x > y if op == 0x52 else
                x >= y if op == 0x53 else x == y if op == 0x54 else x != y)


def _odd(e, a, st):
    st[a] = int((e.round(st[a]) & 127) == (64 if e.opcode == 0x56 else 0))


def _if(e, a, st):
    if st[a] != 0:
        return
    n, out = 1, False
    while not out:
        if not e.skip_code():
            return
        op = e.opcode
        if op == 0x58:
            n += 1
        elif op == 0x1B:
            out = n == 1
        elif op == 0x59:
            n -= 1
            out = n == 0


def _eif(e, a, st):
    pass


def _logic(e, a, st):
    op = e.opcode
    if op == 0x5A:
        st[a] = int(st[a] != 0 and st[a + 1] != 0)
    elif op == 0x5B:
        st[a] = int(st[a] != 0 or st[a + 1] != 0)
    else:
        st[a] = int(st[a] == 0)


def _deltap(e, a, st):
    nump = st[a]
    if nump < 0 or nump > e.new_top // 2:
        e.error = "Too_Few_Arguments"
        nump = e.new_top // 2
    e.new_top -= 2 * nump
    P = 64 - e.gs.delta_base
    op = e.opcode
    P -= 16 if op in (0x71, 0x74) else 32 if op in (0x72, 0x75) else 0
    if P & ~0xF:
        return
    P <<= 4
    F = 1 << (6 - e.gs.delta_shift)
    args = a
    cvt = op >= 0x73
    while nump > 0:
        nump -= 1
        args -= 1
        A = st[args] & 0xFFFF if not cvt else _u32(st[args])
        args -= 1
        B = st[args]
        if cvt:
            if A >= e.cvt_size:
                e.error = "Invalid_Reference"
                return
            if (B & 0xF0) == P:
                B = (B & 0xF) - 8
                if B >= 0:
                    B += 1
                _write_cvt(e, A, i32(e.cvt[A] + B * F))
            continue
        if A >= e.zp0.n_points:
            e.error = "Invalid_Reference"
            return
        if (B & 0xF0) == P:
            B = (B & 0xF) - 8
            if B >= 0:
                B += 1
            B *= F
            if e.bc:
                if e.bc != 7 and ((e.is_composite and e.gs.free[1] != 0) or e.zp0.tags[A] & TOUCH_Y):
                    e.move(e.zp0, A, B)
            else:
                e.move(e.zp0, A, B)


def _sdb(e, a, st):
    e.gs.delta_base = st[a] & 0xFFFF


def _sds(e, a, st):
    if _u32(st[a]) > 6:
        e.error = "Bad_Argument"
    else:
        e.gs.delta_shift = st[a] & 0xFFFF


def _arith(e, a, st):
    x, y = st[a], st[a + 1]
    op = e.opcode
    if op == 0x60:
        st[a] = i32(x + y)
    elif op == 0x61:
        st[a] = i32(x - y)
    elif op == 0x62:
        if y == 0:
            e.error = "Divide_By_Zero"
        else:
            st[a] = muldiv_no_round(x, 64, y)
    else:
        st[a] = muldiv(x, y, 64)


def _unary(e, a, st):
    x = st[a]
    op = e.opcode
    if op == 0x64:
        st[a] = i32(abs(x))
    elif op == 0x65:
        st[a] = i32(-x)
    elif op == 0x66:
        st[a] = x & -64
    else:
        st[a] = i32(x + 63) & -64


def _round_op(e, a, st):
    st[a] = e.round(st[a], None) if e.opcode <= 0x6B else st[a]


def _sround(e, a, st):
    e.set_super_round(0x4000 if e.opcode == 0x76 else 0x2D41, st[a])
    e.gs.round_state = 6 if e.opcode == 0x76 else 7


def _jrot(e, a, st):
    if (st[a + 1] != 0) == (e.opcode == 0x78):
        _jmpr(e, a, st)


def _fliprg(e, a, st):
    if e.bc == 7:
        return
    K, L = st[a + 1], st[a]
    n = e.pts.n_points
    if bounds(K, n) or bounds(L, n):
        e.error = "Invalid_Reference"
        return
    on = e.opcode == 0x81
    tags = e.pts.tags
    for i in range(L, K + 1):
        tags[i] = tags[i] | 1 if on else tags[i] & ~1


def _flippt(e, a, st):
    loop = e.gs.loop
    if e.new_top < loop:
        e.error = "Too_Few_Arguments"
        e.gs.loop = 1
        return
    e.new_top -= loop
    if e.bc != 7:
        args = a
        while loop:
            loop -= 1
            args -= 1
            p = st[args] & 0xFFFF
            if p >= e.pts.n_points:
                e.error = "Invalid_Reference"
                return
            e.pts.tags[p] ^= 1
    e.gs.loop = 1


def _scanctrl(e, a, st):
    A = st[a] & 0xFF
    if A == 0xFF:
        e.gs.scan_control = True
        return
    if A == 0:
        e.gs.scan_control = False
        return
    sc = e.gs.scan_control
    if st[a] & 0x100 and A <= 64:
        sc = True
    if st[a] & 0x200 and False:
        sc = True
    if st[a] & 0x400 and False:
        sc = True
    if st[a] & 0x800 and A > 64:
        sc = False
    if st[a] & 0x1000:
        sc = False
    if st[a] & 0x2000:
        sc = False
    e.gs.scan_control = sc


def _sdpvtl(e, a, st):
    p1, p2 = st[a + 1] & 0xFFFF, st[a] & 0xFFFF
    if bounds(p2, e.zp1.n_points) or bounds(p1, e.zp2.n_points):
        e.error = "Invalid_Reference"
        return
    op = e.opcode
    v1, v2 = e.zp1.org[p2], e.zp2.org[p1]
    A, B = i32(v1[0] - v2[0]), i32(v1[1] - v2[1])
    if A == 0 and B == 0:
        A, op = 0x4000, 0
    if op & 1:
        A, B = i32(-B), A
    e.gs.dual = normalize(A, B, e.gs.dual)
    v1, v2 = e.zp1.cur[p2], e.zp2.cur[p1]
    A, B = i32(v1[0] - v2[0]), i32(v1[1] - v2[1])
    if A == 0 and B == 0:
        A, op = 0x4000, 0
    if op & 1:
        A, B = i32(-B), A
    e.gs.proj = normalize(A, B, e.gs.proj)
    e.compute_funcs()


def _getinfo(e, a, st):
    x = st[a]
    k = 40 if x & 1 else 0
    if x & 64:
        k |= 1 << 13
    if x & 1024:
        k |= 1 << 17
    if x & 2048:
        k |= 1 << 18
    if x & 4096:
        k |= 1 << 19
    st[a] = k


def _roll(e, a, st):
    A, B, C = st[a + 2], st[a + 1], st[a]
    st[a + 2], st[a + 1], st[a] = C, A, B


def _minmax(e, a, st):
    st[a] = max(st[a], st[a + 1]) if e.opcode == 0x8B else min(st[a], st[a + 1])


def _scantype(e, a, st):
    if st[a] >= 0:
        e.gs.scan_type = st[a] & 0xFFFF


def _instctrl(e, a, st):
    K, L = st[a + 1], st[a]
    if K < 1 or K > 3:
        e.error = "Invalid_Reference"
        return
    kf = 1 << (K - 1)
    if L != 0:
        L = kf
    if e.ini_range == CVT:
        e.gs.instruct_control = (e.gs.instruct_control & ~kf) | L
    elif e.ini_range == GLYPH and K == 3:
        e.bc = (L & 4) ^ 4
    else:
        e.error = "Invalid_Reference"


def _nop(e, a, st):
    pass


_DISPATCH = [_unknown] * 256
for _op, _h in [(0x00, _svtca), (0x01, _svtca), (0x02, _svtca), (0x03, _svtca), (0x04, _svtca),
                (0x05, _svtca), (0x06, _spvtl), (0x07, _spvtl), (0x08, _sfvtl), (0x09, _sfvtl),
                (0x0A, _spvfs), (0x0B, _sfvfs), (0x0C, _gpv), (0x0D, _gfv), (0x0E, _sfvtpv),
                (0x0F, _isect), (0x10, _srp), (0x11, _srp), (0x12, _srp), (0x13, _szp), (0x14, _szp),
                (0x15, _szp), (0x16, _szp), (0x17, _sloop), (0x18, _round_state), (0x19, _round_state),
                (0x1A, _smd), (0x1B, _else), (0x1C, _jmpr), (0x1D, _scvtci), (0x1E, _sswci),
                (0x1F, _ssw), (0x20, _dup), (0x21, _pop), (0x22, _clear), (0x23, _swap),
                (0x24, _depth), (0x25, _cindex), (0x26, _mindex), (0x27, _alignpts), (0x29, _utp),
                (0x2A, _loopcall), (0x2B, _call), (0x2C, _fdef), (0x2D, _endf), (0x2E, _mdap),
                (0x2F, _mdap), (0x30, _iup), (0x31, _iup), (0x32, _shp), (0x33, _shp), (0x34, _shc),
                (0x35, _shc), (0x36, _shz), (0x37, _shz), (0x38, _shpix), (0x39, _ip), (0x3A, _msirp),
                (0x3B, _msirp), (0x3C, _alignrp), (0x3D, _round_state), (0x3E, _miap), (0x3F, _miap),
                (0x40, _npush), (0x41, _npush), (0x42, _ws), (0x43, _rs), (0x44, _wcvtp),
                (0x45, _rcvt), (0x46, _gc), (0x47, _gc), (0x48, _scfs), (0x49, _md), (0x4A, _md),
                (0x4B, _mppem), (0x4C, _mps), (0x4D, _flipon), (0x4E, _flipon), (0x4F, _debug),
                (0x50, _cmp), (0x51, _cmp), (0x52, _cmp), (0x53, _cmp), (0x54, _cmp), (0x55, _cmp),
                (0x56, _odd), (0x57, _odd), (0x58, _if), (0x59, _eif), (0x5A, _logic), (0x5B, _logic),
                (0x5C, _logic), (0x5D, _deltap), (0x5E, _sdb), (0x5F, _sds), (0x60, _arith),
                (0x61, _arith), (0x62, _arith), (0x63, _arith), (0x64, _unary), (0x65, _unary),
                (0x66, _unary), (0x67, _unary), (0x70, _wcvtf), (0x71, _deltap), (0x72, _deltap),
                (0x73, _deltap), (0x74, _deltap), (0x75, _deltap), (0x76, _sround), (0x77, _sround),
                (0x78, _jrot), (0x79, _jrot), (0x7A, _round_state), (0x7C, _round_state),
                (0x7D, _round_state), (0x7E, _nop), (0x7F, _nop), (0x80, _flippt), (0x81, _fliprg),
                (0x82, _fliprg), (0x85, _scanctrl), (0x86, _sdpvtl), (0x87, _sdpvtl), (0x88, _getinfo),
                (0x89, _idef), (0x8A, _roll), (0x8B, _minmax), (0x8C, _minmax), (0x8D, _scantype),
                (0x8E, _instctrl)]:
    _DISPATCH[_op] = _h
for _op in range(0x68, 0x70):
    _DISPATCH[_op] = _round_op
for _op in range(0xB0, 0xC0):
    _DISPATCH[_op] = _push
for _op in range(0xC0, 0xE0):
    _DISPATCH[_op] = _mdrp
for _op in range(0xE0, 0x100):
    _DISPATCH[_op] = _mirp
