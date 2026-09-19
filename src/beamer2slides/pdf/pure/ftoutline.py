"""Glyph outlines as FreeType loads them for PDFium: the Adobe CFF engine (psaux/psintrp.c).

FreeType reads Type 1 and CFF charstrings with one interpreter, ``cf2_interpT2CharString``, in
16.16 fixed point: a typed operand stack (psstack.c), Type 1's two passes (hints first, then the
outline), othersubrs, flex, seac, and a glyph path (pshints.c) that queues each element one step
behind and emits it through the builder (psobjs.c / psft.c). PDFium loads glyphs with
``FT_LOAD_NO_HINTING``, so the engine runs at the "unity" scale 0x400 (1/64 in 16.16): a builder
point is ``MulFix(v, 0x400) >> 10``, whole font units, and t1gload / cffgload then scale it with
``FT_MulFix(v, x_scale)`` to 26.6 pixels (the face is at 64 ppem) before ``FT_Load_Glyph`` applies
the ``FT_Set_Transform`` matrix. All of it is integer arithmetic on 32-bit ``FT_Long``
(Windows' ``long``), reproduced here exactly.

``Face.outline(glyph, matrix)`` is the 26.6 outline RenderGlyph renders; ``Face.path(glyph)`` the
float path LoadGlyphPath gives the path route. A glyph FreeType fails to load is None, as in PDFium
(nothing drawn). What this module does not model raises `Unported`.
"""
from __future__ import annotations

import io
import math

from .ftgrays import CUBIC, ON

MASK32 = 0xFFFFFFFF
MAX_SUBR = 16            # CF2_MAX_SUBR and T1_MAX_SUBRS_CALLS
STACK_SIZE = 48          # CF2_OPERAND_STACK_SIZE
STORAGE_SIZE = 32        # CF2_STORAGE_SIZE
MAX_HINTS = 96           # CF2_MAX_HINTS
FIXED_MAX = 0x7FFFFFFF
UNITY = 0x400            # cf2_getScaleAndHintFlag, unhinted
INSTRUCTION_LIMIT = 20000000


class Unported(Exception):
    """Something FreeType does that this port does not reproduce (the page is refused)."""


class GlyphError(Exception):
    """FreeType's glyph load fails: RenderGlyph / LoadGlyphPath return null."""


def i32(v: int) -> int:
    v &= MASK32
    return v - (1 << 32) if v & 0x80000000 else v


def mulfix(a: int, b: int) -> int:
    """FT_MulFix: (a*b + 0x8000 + (ab >> 63)) >> 16, as a 32-bit FT_Long."""
    ab = a * b
    return i32((ab + 0x8000 + (-1 if ab < 0 else 0)) >> 16)


def divfix(a: int, b: int) -> int:
    """FT_DivFix: rounded |a| << 16 / |b| with the sign of a*b; 0x7FFFFFFF when b is 0."""
    s = 1
    if a < 0:
        a, s = -a, -s
    if b < 0:
        b, s = -b, -s
    q = 0x7FFFFFFF if b == 0 else ((a << 16) + (b >> 1)) // b
    q = i32(q)
    return i32(-q) if s < 0 else q


def int_to_fixed(i: int) -> int:
    return i32((i & MASK32) << 16)


def sqrt_fixed(x: int) -> int:
    """FT_SqrtFixed on a positive 16.16 value."""
    return i32(math.isqrt((x & MASK32) << 16))


# ------------------------------------------------------------------ font programs


def _std_name(code: int) -> str:
    from .fonts import STANDARD, _predefined_name
    return _predefined_name(STANDARD, code) or ".notdef"


class Face:
    """One embedded Type 1 or CFF program, read again from the PDF's bytes (fontTools' parse keeps
    each charstring's bytecode; drawing one would replace it)."""

    # a multiple master font's blend (PS_Blend): None for every other font
    weight_vector: list[int] | None = None
    num_designs = 0
    len_buildchar = 0

    def __init__(self, font):
        prog = font.program
        if prog is None or not font.embedded:
            raise Unported("font without an embedded program")
        if prog.kind == "truetype":
            raise Unported("TrueType glyphs")
        self.order = prog.order
        self.local = None
        data = font.program_data
        if prog.kind == "type1":
            self._load_type1(data)
        else:
            self._load_cff(data)
        self._init_caches()

    def _init_caches(self) -> None:
        self.upem = 1000
        self.x_scale = divfix(64 * 64, self.upem)      # FT_Set_Pixel_Sizes(64): DivFix(4096, upem)
        self._units: dict[int, object] = {}
        self._advances: dict[int, int] = {}
        self._outlines: dict = {}
        self._paths: dict = {}

    @classmethod
    def from_type1(cls, t1) -> "Face":
        """A face over a Type 1 program `type1.parse` read (FreeType's own loader, blend included):
        PDFium's built-in multiple master faces."""
        face = cls.__new__(cls)
        face.order = t1.order
        face.local = None
        face.is_t1 = True
        face.charstrings = t1.charstrings
        face.subrs = t1.subrs
        face.gsubrs = []
        face.local_bias = face.global_bias = 0
        face.weight_vector = list(t1.weight_vector) if t1.weight_vector is not None else None
        face.default_weight_vector = list(face.weight_vector) if face.weight_vector is not None else None
        face.design_map = getattr(t1, "design_map", [])
        face.num_designs = t1.num_designs
        face.len_buildchar = t1.len_buildchar
        face.buildchar = [0] * t1.len_buildchar          # face->buildchar: one array for every glyph load
        face._init_caches()
        return face

    # -- loading
    def _load_type1(self, data: bytes) -> None:
        from fontTools import t1Lib
        t1 = t1Lib.T1Font.__new__(t1Lib.T1Font)
        t1.data, t1.encoding = data, "ascii"
        t1.parse()
        d = t1.font
        matrix = [float(v) for v in d.get("FontMatrix", [0.001, 0, 0, 0.001, 0, 0])]
        if matrix != [0.001, 0.0, 0.0, 0.001, 0.0, 0.0]:
            raise Unported(f"Type 1 FontMatrix {matrix}")
        self.is_t1 = True
        self.charstrings = {n: cs.bytecode for n, cs in d["CharStrings"].items()}
        priv = d.get("Private", {})
        self.subrs = [getattr(s, "bytecode", None) for s in priv.get("Subrs", [])]
        self.gsubrs = []
        self.local_bias = self.global_bias = 0

    def _load_cff(self, data: bytes) -> None:
        from fontTools.cffLib import CFFFontSet
        if data[:4] in (b"\x00\x01\x00\x00", b"true", b"ttcf"):
            # CFF outlines under a TrueType tag: FreeType's drivers open it otherwise (measured apart)
            raise Unported("CFF outlines in an sfnt not tagged OTTO")
        if data[:4] == b"OTTO":
            from fontTools.ttLib import TTFont
            data = TTFont(io.BytesIO(data), lazy=True).getTableData("CFF ")
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(data), None)
        top = cff[cff.fontNames[0]]
        matrix = [float(v) for v in getattr(top, "FontMatrix", [0.001, 0, 0, 0.001, 0, 0])]
        if matrix != [0.001, 0.0, 0.0, 0.001, 0.0, 0.0]:
            raise Unported(f"CFF FontMatrix {matrix}")
        self.cid_keyed = hasattr(top, "ROS")
        if hasattr(top, "FDArray"):
            for fd in top.FDArray:
                if "FontMatrix" in getattr(fd, "rawDict", {}):
                    raise Unported("CFF sub-font with its own FontMatrix")
        self.is_t1 = False
        cs = top.CharStrings
        self.charstrings = {}
        self.local = {}                       # glyph name -> (subrs, bias) of its Private dict
        by_private: dict = {}
        for n in top.charset:
            if n not in cs:
                continue
            c = cs[n]
            self.charstrings[n] = c.bytecode
            priv = getattr(c, "private", None)
            key = id(priv)
            if key not in by_private:
                subrs = getattr(priv, "Subrs", None) if priv is not None else None
                lst = [subrs[i].bytecode for i in range(len(subrs))] if subrs is not None else []
                by_private[key] = (lst, _bias(len(lst)))
            self.local[n] = by_private[key]
        self.subrs, self.local_bias = [], _bias(0)
        gs = cff.GlobalSubrs
        self.gsubrs = [gs[i].bytecode for i in range(len(gs))]
        self.global_bias = _bias(len(self.gsubrs))

    def charstring(self, glyph: int) -> bytes:
        name = self.order[glyph] if 0 <= glyph < len(self.order) else None
        data = self.charstrings.get(name) if name is not None else None
        if data is None:
            raise GlyphError(f"no glyph {glyph}")
        if self.local is not None:                        # CFF: the glyph's own Private dict
            self.subrs, self.local_bias = self.local[name]
        return data

    def seac_glyph(self, code: int) -> bytes:
        """t1_lookup_glyph_by_stdcharcode_ps / cff_lookup_glyph_by_stdcharcode: by standard name."""
        if not 0 <= code <= 255 or getattr(self, "cid_keyed", False):
            raise GlyphError("seac code")
        name = _std_name(code)
        data = self.charstrings.get(name)
        if data is None:
            raise GlyphError(f"seac component {name}")
        return data

    @classmethod
    def from_cff(cls, data: bytes, order: list[str]) -> "Face":
        """A face over a bare CFF program: PDFium's built-in standard faces (Foxit*.cff)."""
        face = cls.__new__(cls)
        face.order = order
        face.local = None
        face._load_cff(data)
        face._init_caches()
        return face

    # -- multiple masters (t1load.c): the blend is state of the face, shared by every font drawn with it
    def blend_key(self):
        return None if self.weight_vector is None else tuple(self.weight_vector)

    def mm_var(self) -> list[tuple[int, int, int]] | None:
        """T1_Get_MM_Var: per axis (minimum, maximum, default), 16.16; None without a blend."""
        if self.weight_vector is None or not self.design_map:
            return None
        w = self.default_weight_vector
        n = len(self.design_map)
        if n == 1:
            coords = [w[1]]
        elif n == 2:
            coords = [w[3] + w[1], w[3] + w[2]]
        else:
            raise Unported(f"multiple master font with {n} axes")
        out = []
        for (designs, blends), ncv in zip(self.design_map, coords):
            out.append((int_to_fixed(designs[0]), int_to_fixed(designs[-1]), _axis_unmap(designs, blends, ncv)))
        return out

    def set_mm_design(self, coords: list[int]) -> None:
        """FT_Set_MM_Design_Coordinates = T1_Set_MM_Design, then t1_set_mm_blend."""
        blends_out = []
        for n, (designs, blends) in enumerate(self.design_map):
            design = coords[n] if n < len(coords) else _cdiv(designs[-1] - designs[0], 2)
            before = after = -1
            the_blend = None
            for p, p_design in enumerate(designs):
                if design == p_design:
                    the_blend = blends[p]
                    break
                if design < p_design:
                    after = p
                    break
                before = p
            if the_blend is None:
                if before < 0:
                    the_blend = blends[0]
                elif after < 0:
                    the_blend = blends[-1]
                else:
                    the_blend = muldiv(design - designs[before], blends[after] - blends[before],
                                       designs[after] - designs[before])
            blends_out.append(the_blend)
        num_axis = len(self.design_map)
        for n in range(self.num_designs):
            result = 0x10000
            for m in range(num_axis):
                factor = blends_out[m]
                if not n & (1 << m):
                    factor = 0x10000 - factor
                if factor <= 0:
                    result = 0
                    break
                if factor >= 0x10000:
                    continue
                result = mulfix(result, factor)
            self.weight_vector[n] = result

    def adjust_variation(self, glyph: int, dest_width: int, weight: int) -> None:
        """CFX_Face::AdjustVariationParams: the Weight axis at `weight`, the Width axis where the
        glyph's advance comes closest to `dest_width` (linear between the axis ends)."""
        var = self.mm_var()
        if var is None:
            return
        c0 = _cdiv(var[0][2], 65536) if weight == 0 else weight
        if dest_width == 0:
            c1 = _cdiv(var[1][2], 65536)
        else:
            min_param, max_param = _cdiv(var[1][0], 65536), _cdiv(var[1][1], 65536)
            self.set_mm_design([c0, min_param])
            min_width = _cdiv(self._horiadvance(glyph) * 1000, self.upem)
            self.set_mm_design([c0, max_param])
            max_width = _cdiv(self._horiadvance(glyph) * 1000, self.upem)
            if max_width == min_width:
                return
            c1 = i32(min_param + _cdiv(i32((max_param - min_param) * (dest_width - min_width)),
                                       max_width - min_width))
        self.set_mm_design([c0, c1])

    def _horiadvance(self, glyph: int) -> int:
        a = self.advance(glyph)
        if a is None:
            raise Unported("a multiple master glyph that fails to load")   # metrics of the glyph before
        return a

    # -- the three products
    def units(self, glyph: int):
        """The unscaled outline (whole font units, psobjs' builder) or None when the load fails."""
        key = (glyph, self.blend_key())
        if key not in self._units:
            dec = Decoder(self)
            try:
                self._units[key] = dec.load(self.charstring(glyph))
                self._advances[key] = dec.advance_x
            except GlyphError:
                self._units[key] = None
        return self._units[key]

    def advance(self, glyph: int) -> int | None:
        """The glyph's unscaled horiAdvance, FIXED_TO_INT(builder.advance.x) (t1gload), or None when
        the load fails."""
        if self.units(glyph) is None:
            return None
        a = self._advances[(glyph, self.blend_key())]
        return i32((a + 0x8000 - (1 if a < 0 else 0)) & ~0xFFFF) >> 16    # FT_RoundFix, then >> 16

    def outline(self, glyph: int, matrix: tuple[int, int, int, int]):
        """FT_Load_Glyph under FT_Set_Transform(matrix = xx, xy, yx, yy): 26.6 contours
        [(points, tags)] as ftgrays takes them, or None."""
        key = (glyph, matrix, self.blend_key())
        if key in self._outlines:
            return self._outlines[key]
        units = self.units(glyph)
        out = None
        if units is not None:
            xx, xy, yx, yy = matrix
            identity = matrix == (0x10000, 0, 0, 0x10000)
            out = []
            for pts, tags in units:
                sp = []
                for x, y in pts:
                    x, y = mulfix(x, self.x_scale), mulfix(y, self.x_scale)
                    if not identity:
                        x, y = (i32(mulfix(x, xx) + mulfix(y, xy)), i32(mulfix(x, yx) + mulfix(y, yy)))
                    sp.append((x, y))
                out.append((sp, tags))
        self._outlines[key] = out
        return out

    def path(self, glyph: int, matrix: tuple[int, int, int, int] = (0x10000, 0, 0, 0x10000)):
        """CFX_Face::LoadGlyphPath: [(x, y, kind, close)] in em units (float32), or None. `matrix` is
        the FT_Set_Transform a substitute's skew makes."""
        key = (glyph, matrix, self.blend_key())
        if key in self._paths:
            return self._paths[key]
        out = self.outline(glyph, matrix)
        path = None if out is None else _glyph_path(out)
        self._paths[key] = path
        return path


def _cdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


# --------------------------------------------------------------- FT_Outline_Embolden (ftoutln.c)
# Synthetic bold, which PDFium applies to a system substitute heavier than its face. Ported line by
# line from FreeType, but PDFium never reaches it with the Foxit faces (their weight goes into the
# multiple master blend instead), so no oracle has checked it yet.


def _u32(v: int) -> int:
    return v & MASK32


def _msb(v: int) -> int:
    return v.bit_length() - 1


def vector_normlen(x_: int, y_: int) -> tuple[int, int, int]:
    """FT_Vector_NormLen: (unit x, unit y in 16.16, length)."""
    x_, y_ = i32(x_), i32(y_)
    sx = sy = 1
    x, y = x_, y_
    if x < 0:
        x, sx = _u32(-x), -1
    if y < 0:
        y, sy = _u32(-y), -1
    if x == 0:
        return x_, (sy * 0x10000 if y > 0 else y_), y
    if y == 0:
        return sx * 0x10000, y_, x
    ln = x + (y >> 1) if x > y else y + (x >> 1)
    ln = _u32(ln)
    shift = 31 - _msb(ln)
    shift -= 15 + (1 if ln >= (0xAAAAAAAA >> shift) else 0)
    if shift > 0:
        x, y = _u32(x << shift), _u32(y << shift)
        ln = _u32(x + (y >> 1) if x > y else y + (x >> 1))
    else:
        x, y, ln = x >> -shift, y >> -shift, ln >> -shift
    b = i32(0x10000 - i32(ln))
    xs, ys = i32(x), i32(y)
    while True:
        u = _u32(xs + (i32(xs * b) >> 16))
        v = _u32(ys + (i32(ys * b) >> 16))
        z = _cdiv(-i32(u * u + v * v), 0x200)
        z = _cdiv(i32(z * ((0x10000 + b) >> 8)), 0x10000)
        b = i32(b + z)
        if z <= 0:
            break
    vx = -u if sx < 0 else u
    vy = -v if sy < 0 else v
    ln = _u32(0x10000 + _cdiv(i32(u * x + v * y), 0x10000))
    ln = (ln + (1 << (shift - 1))) >> shift if shift > 0 else _u32(ln << -shift)
    return vx, vy, ln


def _orientation(outline) -> int:
    """FT_Outline_Get_Orientation (the FT_INT64 shoelace): 1 TrueType, 2 PostScript, 0 none."""
    if not any(pts for pts, _ in outline):
        return 1
    area = 0
    for pts, _tags in outline:
        if not pts:
            continue
        px, py = pts[-1]
        for cx, cy in pts:
            area += (cy - py) * (cx + px)
            px, py = cx, cy
    return 2 if area > 0 else 1 if area < 0 else 0


def embolden(outline, strength: int):
    """FT_Outline_Embolden(outline, strength) on 26.6 contours [(points, tags)]: a new outline."""
    xs = ys = _cdiv(strength, 2)
    if xs == 0 and ys == 0:
        return outline
    orient = _orientation(outline)
    if orient == 0:
        return outline                      # Invalid_Argument, which PDFium ignores: left as it was
    out = []
    for pts, tags in outline:
        points = [list(p) for p in pts]
        first, last = 0, len(points) - 1
        l_in = 0
        in_x = in_y = anchor_x = anchor_y = 0
        l_anchor = 0
        i, j, k = last, first, -1
        while j != i and i != k:
            if j != k:
                ox, oy, l_out = vector_normlen(points[j][0] - points[i][0], points[j][1] - points[i][1])
                if l_out == 0:
                    j = j + 1 if j < last else first
                    continue
            else:
                ox, oy, l_out = anchor_x, anchor_y, l_anchor
            if l_in != 0:
                if k < 0:
                    k, anchor_x, anchor_y, l_anchor = i, in_x, in_y, l_in
                d = mulfix(in_x, ox) + mulfix(in_y, oy)
                if d > -0xF000:
                    d = d + 0x10000
                    shx, shy = in_y + oy, in_x + ox
                    if orient == 1:
                        shx = -shx
                    else:
                        shy = -shy
                    q = mulfix(ox, in_y) - mulfix(oy, in_x)
                    if orient == 1:
                        q = -q
                    lm = min(l_in, l_out)
                    shx = muldiv(shx, xs, d) if mulfix(xs, q) <= mulfix(lm, d) else muldiv(shx, lm, q)
                    shy = muldiv(shy, ys, d) if mulfix(ys, q) <= mulfix(lm, d) else muldiv(shy, lm, q)
                else:
                    shx = shy = 0
                while i != j:
                    points[i][0] = i32(points[i][0] + xs + shx)
                    points[i][1] = i32(points[i][1] + ys + shy)
                    i = i + 1 if i < last else first
            else:
                i = j
            in_x, in_y, l_in = ox, oy, l_out
            j = j + 1 if j < last else first
        out.append(([tuple(p) for p in points], tags))
    return out


def muldiv(a: int, b: int, c: int) -> int:
    """FT_MulDiv: rounded a*b/c with the sign of the product, 32-bit FT_Long."""
    s = 1
    if a < 0:
        a, s = -a, -s
    if b < 0:
        b, s = -b, -s
    if c < 0:
        c, s = -c, -s
    d = (a * b + (c >> 1)) // c if c > 0 else 0x7FFFFFFF
    d = i32(d)
    return i32(-d) if s < 0 else d


def _axis_unmap(designs: list[int], blends: list[int], ncv: int) -> int:
    """mm_axis_unmap: the design coordinate (16.16) of a normalized one."""
    if ncv <= blends[0]:
        return int_to_fixed(designs[0])
    for j in range(1, len(designs)):
        if ncv <= blends[j]:
            return int_to_fixed(designs[j - 1] + muldiv(ncv - blends[j - 1], designs[j] - designs[j - 1],
                                                        blends[j] - blends[j - 1]))
    return int_to_fixed(designs[-1])


def _bias(n: int) -> int:
    return 107 if n < 1240 else 1131 if n < 33900 else 32768


# --------------------------------------------------------------- LoadGlyphPath


MOVE, LINE, BEZIER = "M", "L", "C"


def _glyph_path(outline):
    """FT_Outline_Decompose into Outline_MoveTo/LineTo/ConicTo/CubicTo, then
    Outline_CheckEmptyContour and ClosePath. Points are [x, y, kind, close] with
    x = (float)pos / 4096. A conic becomes a cubic with controls cur + (ctrl - cur) * 2 / 3 and
    ctrl + (to - ctrl) / 3, in C's truncating 32-bit FT_Pos arithmetic."""
    from .ftgrays import outline_decompose
    from .raster import F
    pts: list[list] = []
    cur = [0, 0]

    def pt(x, y):
        return F(F(x) / 4096.0), F(F(y) / 4096.0)

    def close():
        if pts:
            pts[-1][3] = True

    def move_to(p):
        _check_empty(pts)
        close()
        pts.append([*pt(*p), MOVE, False])
        cur[:] = p

    def line_to(p):
        pts.append([*pt(*p), LINE, False])
        cur[:] = p

    def conic_to(c, p):
        cx, cy = cur
        pts.append([*pt(i32(cx + _cdiv(i32(i32(c[0] - cx) * 2), 3)),
                        i32(cy + _cdiv(i32(i32(c[1] - cy) * 2), 3))), BEZIER, False])
        pts.append([*pt(i32(c[0] + _cdiv(i32(p[0] - c[0]), 3)),
                        i32(c[1] + _cdiv(i32(p[1] - c[1]), 3))), BEZIER, False])
        pts.append([*pt(*p), BEZIER, False])
        cur[:] = p

    def cubic_to(c1, c2, p):
        pts.append([*pt(*c1), BEZIER, False])
        pts.append([*pt(*c2), BEZIER, False])
        pts.append([*pt(*p), BEZIER, False])
        cur[:] = p

    try:
        outline_decompose(outline, move_to, line_to, conic_to, cubic_to)
    except ValueError:
        return None                                       # Invalid_Outline: FT_Load_Glyph erred first
    if not pts:
        return None
    _check_empty(pts)
    close()
    return pts


def _cdiv(a: int, b: int) -> int:
    """C's integer division (truncating towards 0)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _check_empty(pts: list) -> None:
    size = len(pts)
    if size >= 2 and pts[size - 2][2] == MOVE and not pts[size - 2][3] \
            and pts[size - 2][:2] == pts[size - 1][:2]:
        size -= 2
    if size >= 4 and pts[size - 4][2] == MOVE and not pts[size - 4][3] \
            and pts[size - 3][2] == BEZIER and not pts[size - 3][3] \
            and pts[size - 3][:2] == pts[size - 4][:2] and pts[size - 2][:2] == pts[size - 4][:2] \
            and pts[size - 1][:2] == pts[size - 4][:2]:
        size -= 4
    del pts[size:]


# --------------------------------------------------------------- the interpreter


class Builder:
    """psobjs.c's PS_Builder over one FT_Outline (points in whole units, tags ON / CUBIC)."""

    def __init__(self):
        self.points: list[tuple[int, int]] = []
        self.tags: list[int] = []
        self.contours: list[int] = []
        self.n_contours = 0
        self.path_begun = False

    def add_point(self, x: int, y: int, on: bool) -> None:
        self.points.append((i32(x) >> 10, i32(y) >> 10))
        self.tags.append(ON if on else CUBIC)

    def _set_contour(self, i: int, v: int) -> None:
        while len(self.contours) <= i:
            self.contours.append(0)
        self.contours[i] = v

    def add_contour(self) -> None:
        if self.n_contours > 0:
            self._set_contour(self.n_contours - 1, len(self.points) - 1)
        self.n_contours += 1

    def start_point(self, x: int, y: int) -> None:
        if not self.path_begun:
            self.path_begun = True
            self.add_contour()
            self.add_point(x, y, True)

    def close_contour(self) -> None:
        n_points = len(self.points)
        first = 0 if self.n_contours <= 1 else self.contours[self.n_contours - 2] + 1
        if self.n_contours and first == n_points:
            self.n_contours -= 1
            return
        if n_points > 1:
            if self.points[first] == self.points[-1] and self.tags[-1] == ON:
                self.points.pop()
                self.tags.pop()
        if self.n_contours > 0:
            if first == len(self.points) - 1:
                self.n_contours -= 1
                self.points.pop()
                self.tags.pop()
            else:
                self._set_contour(self.n_contours - 1, len(self.points) - 1)

    # cf2 outline callbacks (psft.c)
    def cb_move(self) -> None:
        self.close_contour()
        self.path_begun = False

    def cb_line(self, p0, p1) -> None:
        if not self.path_begun:
            self.start_point(*p0)
        self.add_point(p1[0], p1[1], True)

    def cb_cube(self, p0, p1, p2, p3) -> None:
        if not self.path_begun:
            self.start_point(*p0)
        self.add_point(p1[0], p1[1], False)
        self.add_point(p2[0], p2[1], False)
        self.add_point(p3[0], p3[1], True)

    def result(self):
        out = []
        first = 0
        for c in range(self.n_contours):
            last = self.contours[c]
            if last < first or last >= len(self.points):
                raise GlyphError("bad contour")         # FT_Outline_Check would refuse it
            out.append((self.points[first:last + 1], self.tags[first:last + 1]))
            first = last + 1
        return out


def _hint(v: int) -> int:
    """cf2_glyphpath_hintPoint unhinted: MulFix by the unity scale, outer transform identity."""
    return mulfix(v, UNITY)


class GlyphPath:
    """pshints.c's CF2_GlyphPath without darkening: offsets are 0, so joins never intersect."""

    def __init__(self, dec: "Decoder"):
        self.dec = dec
        self.b = dec.builder
        self.move_pending = True
        self.path_open = False
        self.closing = False
        self.queued = False
        self.prev_op = None
        self.prev = ()
        self.cur_cs = (0, 0)
        self.start = (0, 0)
        self.current_ds = (0, 0)
        self.offset_start0 = (0, 0)
        self.offset_start1 = (0, 0)

    @staticmethod
    def ds(p):
        return _hint(p[0]), _hint(p[1])

    def move_to(self, x: int, y: int) -> None:
        self.close_open()
        self.cur_cs = self.start = (x, y)
        self.move_pending = True
        d = self.dec
        if not d.map_valid or d.mask_new:
            d.build_map()

    def _push_move(self, start) -> None:
        d = self.dec
        if not d.map_valid:
            self.move_to(*self.start)
        pt1 = self.ds(start)
        self.b.cb_move()
        self.current_ds = pt1
        self.offset_start0 = start

    def _push_prev(self, next_p0, close: bool) -> None:
        # prevP1 == nextP0 always (no darkening offsets): no intersection.
        if self.prev_op == "L":
            p1 = self.ds(self.prev[1])
            if p1 != self.current_ds:
                self.b.cb_line(self.current_ds, p1)
                self.current_ds = p1
        else:
            p1, p2, p3 = self.ds(self.prev[1]), self.ds(self.prev[2]), self.ds(self.prev[3])
            self.b.cb_cube(self.current_ds, p1, p2, p3)
            self.current_ds = p3
        # !useIntersection: connecting line to the next start
        p1 = self.ds(next_p0)
        if p1 != self.current_ds:
            self.b.cb_line(self.current_ds, p1)
            self.current_ds = p1

    def line_to(self, x: int, y: int) -> None:
        d = self.dec
        new_map = d.mask_new and not self.closing
        if self.cur_cs == (x, y) and not new_map:
            return
        p0, p1 = self.cur_cs, (x, y)
        if self.move_pending:
            self._push_move(p0)
            self.move_pending = False
            self.path_open = True
            self.offset_start1 = p1
        if self.queued:
            self._push_prev(p0, False)
        self.queued = True
        self.prev_op = "L"
        self.prev = (p0, p1)
        if new_map:
            d.build_map()
        self.cur_cs = (x, y)

    def curve_to(self, x1, y1, x2, y2, x3, y3) -> None:
        d = self.dec
        p0 = self.cur_cs
        if self.move_pending:
            self._push_move(p0)
            self.move_pending = False
            self.path_open = True
            self.offset_start1 = (x1, y1)
        if self.queued:
            self._push_prev(p0, False)
        self.queued = True
        self.prev_op = "C"
        self.prev = (p0, (x1, y1), (x2, y2), (x3, y3))
        if d.mask_new:
            d.build_map()
        self.cur_cs = (x3, y3)

    def close_open(self) -> None:
        if self.path_open:
            self.closing = True
            self.line_to(*self.start)
            if self.queued:
                self._push_prev(self.offset_start0, True)
            self.move_pending = True
            self.path_open = False
            self.closing = False
            self.queued = False


class Stack:
    """psstack.c: 48 typed entries; errors fail the glyph."""

    def __init__(self):
        self.v: list[int] = []
        self.is_int: list[bool] = []

    def count(self) -> int:
        return len(self.v)

    def push_int(self, i: int) -> None:
        if len(self.v) >= STACK_SIZE:
            raise GlyphError("stack overflow")
        self.v.append(i32(i))
        self.is_int.append(True)

    def push_fixed(self, r: int) -> None:
        if len(self.v) >= STACK_SIZE:
            raise GlyphError("stack overflow")
        self.v.append(i32(r))
        self.is_int.append(False)

    def pop_int(self) -> int:
        if not self.v:
            raise GlyphError("stack underflow")
        if not self.is_int[-1]:
            raise GlyphError("syntax error")
        self.is_int.pop()
        return self.v.pop()

    def pop_fixed(self) -> int:
        if not self.v:
            raise GlyphError("stack underflow")
        v, is_int = self.v.pop(), self.is_int.pop()
        return int_to_fixed(v) if is_int else v

    def get(self, idx: int) -> int:
        if not 0 <= idx < len(self.v):
            raise GlyphError("stack overflow")
        return int_to_fixed(self.v[idx]) if self.is_int[idx] else self.v[idx]

    def set(self, idx: int, r: int) -> None:
        if not 0 <= idx < len(self.v):
            raise GlyphError("stack overflow")
        self.v[idx] = i32(r)
        self.is_int[idx] = False

    def pop(self, num: int) -> None:
        if num > len(self.v):
            raise GlyphError("stack underflow")
        if num:
            del self.v[-num:]
            del self.is_int[-num:]

    def roll(self, count: int, shift: int) -> None:
        if count < 2:
            return
        if count > len(self.v):
            raise GlyphError("stack overflow")
        offset = len(self.v) - count
        if shift < 0:
            shift = -((-shift) % count)
        else:
            shift %= count
        if shift == 0:
            return
        start = idx = -1
        last_v = last_t = None
        for _ in range(count):
            if start == idx:
                start += 1
                idx = start
                last_v, last_t = self.v[idx + offset], self.is_int[idx + offset]
            idx += shift
            if idx >= count:
                idx -= count
            elif idx < 0:
                idx += count
            j = idx + offset
            self.v[j], last_v = last_v, self.v[j]
            self.is_int[j], last_t = last_t, self.is_int[j]

    def clear(self) -> None:
        self.v.clear()
        self.is_int.clear()


class _Buf:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def end(self) -> bool:
        return self.pos >= len(self.data)

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise GlyphError("read past the charstring")
        b = self.data[self.pos]
        self.pos += 1
        return b


class Decoder:
    """One glyph load: the PS_Decoder state shared by the top charstring and seac components."""

    def __init__(self, face: Face):
        self.face = face
        self.is_t1 = face.is_t1
        self.builder = Builder()
        self.flex_state = 0
        self.num_flex_vectors = 0
        self.lsb_x = self.lsb_y = 0
        self.advance_x = self.advance_y = 0              # builder.advance (hsbw / sbw)
        self.limit = INSTRUCTION_LIMIT
        # per-interp hint state lives here while that interp runs (see interp)
        self.mask_valid = False
        self.mask_new = False
        self.map_valid = False
        self.n_stems = 0
        self.have_width = False

    def load(self, charstring: bytes):
        self.interp(charstring, False, 0, 0)
        self.builder.close_contour()                     # cf2_outline_close
        return self.builder.result()

    # hint map bookkeeping (only what decides the glyph path's behaviour)
    def build_map(self) -> None:
        if not self.mask_valid:
            # cf2_hintmask_setAll: no stems leaves the mask invalid (no error), more than 96 is an
            # error that Type 1 clears; either way the map stays invalid and the build returns.
            if self.n_stems > MAX_HINTS and not self.is_t1:
                raise GlyphError("too many hints")
            if self.n_stems == 0 or self.n_stems > MAX_HINTS:
                return
            self.mask_valid = True
            self.mask_new = True
        self.map_valid = True
        self.mask_new = False

    def interp(self, data: bytes, doing_seac: bool, cur_x: int, cur_y: int) -> None:
        saved = (self.mask_valid, self.mask_new, self.map_valid, self.n_stems, self.have_width)
        self.mask_valid = self.mask_new = self.map_valid = False
        self.n_stems = 0
        self.have_width = False                          # haveWidth, a local of each interp call
        try:
            self._interp(data, doing_seac, cur_x, cur_y)
        finally:
            self.mask_valid, self.mask_new, self.map_valid, self.n_stems, self.have_width = saved

    def _stems(self, st: Stack) -> None:
        self.have_width = True                           # cf2_doStems defines a width
        count = st.count()
        start = 1 if count & 1 else 0
        for i in range(start, count, 2):
            st.get(i)
            st.get(i + 1)
            self.n_stems += 1
        st.clear()

    def _interp(self, data: bytes, doing_seac: bool, cur_x: int, cur_y: int) -> None:
        face, t1 = self.face, self.is_t1
        st = Stack()
        gp = GlyphPath(self)
        storage = [0] * STORAGE_SIZE
        flex_store = [0] * 6
        results = [0] * 3
        result_cnt = 0
        known = 0
        large_int = False
        initial_map_ready = False
        subr_stack: list[_Buf] = [_Buf(data)]
        buf = subr_stack[0]

        while True:
            if buf.end():
                op = 11 if len(subr_stack) > 1 else 14
            else:
                op = buf.byte()
            if t1:
                if not initial_map_ready and not (op in (1, 3, 13, 10, 11, 12, 14) or op >= 32):
                    st.clear()
                    continue
                if result_cnt > 0 and not (op in (10, 11, 12) or op >= 32):
                    result_cnt = 0
                if large_int and not (op >= 32 or op == 12):
                    large_int = False
            self.limit -= 1
            if self.limit == 0:
                raise GlyphError("instruction limit")

            clear = True
            if op in (0, 2, 17, 15, 16):
                pass
            elif op in (1, 18):                                   # hstem(hm)
                if not t1 and self.mask_valid:
                    pass
                else:
                    self._stems(st)
            elif op in (3, 23):                                   # vstem(hm)
                if not t1 and self.mask_valid:
                    pass
                else:
                    self._stems(st)
            elif op == 4:                                         # vmoveto
                self.have_width = True
                cur_y = i32(cur_y + st.pop_fixed())
                if not self.flex_state:
                    gp.move_to(cur_x, cur_y)
            elif op == 5:                                         # rlineto
                count = st.count()
                for idx in range(0, count, 2):
                    cur_x = i32(cur_x + st.get(idx))
                    cur_y = i32(cur_y + st.get(idx + 1))
                    gp.line_to(cur_x, cur_y)
                st.clear()
                clear = False
            elif op in (6, 7):                                    # hlineto / vlineto
                is_x = op == 6
                for idx in range(st.count()):
                    v = st.get(idx)
                    if is_x:
                        cur_x = i32(cur_x + v)
                    else:
                        cur_y = i32(cur_y + v)
                    is_x = not is_x
                    gp.line_to(cur_x, cur_y)
                st.clear()
                clear = False
            elif op in (8, 24):                                   # rrcurveto / rcurveline
                count, idx = st.count(), 0
                while idx + 6 <= count:
                    x1 = i32(st.get(idx) + cur_x)
                    y1 = i32(st.get(idx + 1) + cur_y)
                    x2 = i32(st.get(idx + 2) + x1)
                    y2 = i32(st.get(idx + 3) + y1)
                    x3 = i32(st.get(idx + 4) + x2)
                    y3 = i32(st.get(idx + 5) + y2)
                    gp.curve_to(x1, y1, x2, y2, x3, y3)
                    cur_x, cur_y = x3, y3
                    idx += 6
                if op == 24:
                    cur_x = i32(cur_x + st.get(idx))
                    cur_y = i32(cur_y + st.get(idx + 1))
                    gp.line_to(cur_x, cur_y)
                st.clear()
                clear = False
            elif op == 9:                                         # closepath
                if t1:
                    gp.close_open()
                    self.have_width = True
            elif op in (10, 29):                                  # callsubr / callgsubr
                if len(subr_stack) - 1 >= MAX_SUBR:
                    raise GlyphError("subroutine nesting")
                num = st.pop_int()
                if op == 29:
                    idx, table = num + face.global_bias, face.gsubrs
                else:
                    idx, table = num + face.local_bias, face.subrs
                idx &= MASK32
                if idx >= len(table) or table[idx] is None:
                    raise GlyphError("subroutine index")
                buf = _Buf(table[idx])
                subr_stack.append(buf)
                continue
            elif op == 11:                                        # return
                if len(subr_stack) < 2:
                    raise GlyphError("return from the charstring")
                subr_stack.pop()
                buf = subr_stack[-1]
                continue
            elif op == 12:
                op2 = buf.byte()
                r = self._esc(op2, st, gp, locals())
                if r is not None:
                    kind, vals = r
                    if kind == "exit":
                        return
                    (cur_x, cur_y, result_cnt, known, large_int, clear) = vals
                    if not clear:
                        continue
            elif op == 13:                                        # hsbw
                if t1:
                    self.advance_x = st.pop_fixed()
                    self.advance_y = 0
                    lsb = st.pop_fixed()
                    self.have_width = True
                    self.lsb_x = i32(self.lsb_x + lsb)
                    if initial_map_ready:
                        cur_x = i32(cur_x + lsb)
            elif op == 14:                                        # endchar
                if t1 and not initial_map_ready:
                    gp.move_to(cur_x, cur_y)
                    initial_map_ready = True
                    self.n_stems = 0
                    self.mask_valid = False
                    self.mask_new = True
                    del subr_stack[1:]
                    buf = subr_stack[0]
                    buf.pos = 0
                    st.clear()
                    continue
                gp.close_open()
                if not t1 and st.count() > 1:                    # implied seac
                    if doing_seac:
                        raise GlyphError("nested seac")
                    achar = st.pop_int()
                    bchar = st.pop_int()
                    cur_y = st.pop_fixed()
                    cur_x = st.pop_fixed()
                    self.interp(face.seac_glyph(achar), True, cur_x, cur_y)
                    self.interp(face.seac_glyph(bchar), True, 0, 0)
                return
            elif op in (19, 20):                                  # hintmask / cntrmask
                if st.count() > 1 and self.mask_valid:
                    pass
                else:
                    self._stems(st)
                    if self.n_stems > MAX_HINTS:
                        raise GlyphError("too many hints")
                    if self.n_stems:                     # cf2_hintmask_read: 0 stems reads nothing
                        if op == 19:
                            self.mask_valid = True
                            self.mask_new = True
                        for _ in range((self.n_stems + 7) // 8):
                            buf.byte()
            elif op == 21:                                        # rmoveto
                self.have_width = True
                cur_y = i32(cur_y + st.pop_fixed())
                cur_x = i32(cur_x + st.pop_fixed())
                if not self.flex_state:
                    gp.move_to(cur_x, cur_y)
            elif op == 22:                                        # hmoveto
                self.have_width = True
                cur_x = i32(cur_x + st.pop_fixed())
                if not self.flex_state:
                    gp.move_to(cur_x, cur_y)
            elif op == 25:                                        # rlinecurve
                count, idx = st.count(), 0
                while idx + 6 < count:
                    cur_x = i32(cur_x + st.get(idx))
                    cur_y = i32(cur_y + st.get(idx + 1))
                    gp.line_to(cur_x, cur_y)
                    idx += 2
                while idx < count:
                    x1 = i32(st.get(idx) + cur_x)
                    y1 = i32(st.get(idx + 1) + cur_y)
                    x2 = i32(st.get(idx + 2) + x1)
                    y2 = i32(st.get(idx + 3) + y1)
                    x3 = i32(st.get(idx + 4) + x2)
                    y3 = i32(st.get(idx + 5) + y2)
                    gp.curve_to(x1, y1, x2, y2, x3, y3)
                    cur_x, cur_y = x3, y3
                    idx += 6
                st.clear()
                clear = False
            elif op in (26, 27):                                  # vvcurveto / hhcurveto
                count1 = st.count()
                count = count1 & ~2
                idx = count1 - count
                while idx < count:
                    if op == 26:
                        if (count - idx) & 1:
                            x1 = i32(st.get(idx) + cur_x)
                            idx += 1
                        else:
                            x1 = cur_x
                        y1 = i32(st.get(idx) + cur_y)
                        x2 = i32(st.get(idx + 1) + x1)
                        y2 = i32(st.get(idx + 2) + y1)
                        x3 = x2
                        y3 = i32(st.get(idx + 3) + y2)
                    else:
                        if (count - idx) & 1:
                            y1 = i32(st.get(idx) + cur_y)
                            idx += 1
                        else:
                            y1 = cur_y
                        x1 = i32(st.get(idx) + cur_x)
                        x2 = i32(st.get(idx + 1) + x1)
                        y2 = i32(st.get(idx + 2) + y1)
                        x3 = i32(st.get(idx + 3) + x2)
                        y3 = y2
                    gp.curve_to(x1, y1, x2, y2, x3, y3)
                    cur_x, cur_y = x3, y3
                    idx += 4
                st.clear()
                clear = False
            elif op in (30, 31):                                  # vhcurveto / hvcurveto
                count1 = st.count()
                count = count1 & ~2
                idx = count1 - count
                alternate = op == 31
                while idx < count:
                    if alternate:
                        x1 = i32(st.get(idx) + cur_x)
                        y1 = cur_y
                        x2 = i32(st.get(idx + 1) + x1)
                        y2 = i32(st.get(idx + 2) + y1)
                        y3 = i32(st.get(idx + 3) + y2)
                        if count - idx == 5:
                            x3 = i32(st.get(idx + 4) + x2)
                            idx += 1
                        else:
                            x3 = x2
                        alternate = False
                    else:
                        x1 = cur_x
                        y1 = i32(st.get(idx) + cur_y)
                        x2 = i32(st.get(idx + 1) + x1)
                        y2 = i32(st.get(idx + 2) + y1)
                        x3 = i32(st.get(idx + 3) + x2)
                        if count - idx == 5:
                            y3 = i32(st.get(idx + 4) + y2)
                            idx += 1
                        else:
                            y3 = y2
                        alternate = True
                    gp.curve_to(x1, y1, x2, y2, x3, y3)
                    cur_x, cur_y = x3, y3
                    idx += 4
                st.clear()
                clear = False
            elif op == 28:                                        # shortint
                b1, b2 = buf.byte(), buf.byte()
                v = (b1 << 8) | b2
                st.push_int(v - 0x10000 if v & 0x8000 else v)
                continue
            elif 32 <= op <= 246:
                st.push_int(op - 139)
                continue
            elif 247 <= op <= 250:
                st.push_int((op - 247) * 256 + buf.byte() + 108)
                continue
            elif 251 <= op <= 254:
                st.push_int(-((op - 251) * 256 + buf.byte()) - 108)
                continue
            elif op == 255:
                v = i32((buf.byte() << 24) | (buf.byte() << 16) | (buf.byte() << 8) | buf.byte())
                if t1:
                    if v > 32000 or v < -32000:
                        large_int = True
                    st.push_int(v)
                else:
                    st.push_fixed(v)
                continue
            if clear:
                st.clear()

    def _esc(self, op2, st: Stack, gp: GlyphPath, env: dict):
        """The two-byte operators. Returns None (break: clear the stack), ("exit", None) or
        ("state", (cur_x, cur_y, result_cnt, known, large_int, clear))."""
        t1 = self.is_t1
        cur_x, cur_y = env["cur_x"], env["cur_y"]
        result_cnt, known, large_int = env["result_cnt"], env["known"], env["large_int"]
        initial_map_ready, doing_seac = env["initial_map_ready"], env["doing_seac"]
        results, storage, flex_store = env["results"], env["storage"], env["flex_store"]

        def state(clear):
            return "state", (cur_x, cur_y, result_cnt, known, large_int, clear)

        if op2 in (34, 35, 36, 37):
            rfs = {34: (1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 0), 35: (1,) * 12,
                   36: (1, 1, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0), 37: (1,) * 10 + (0, 0)}[op2]
            cur_x, cur_y = self._flex(st, gp, cur_x, cur_y, rfs, op2 == 37)
            return state(op2 == 35)                       # flex "breaks" (clears again), others continue
        if op2 in (8, 13, 19, 25, 31, 32) or op2 >= 38:
            return state(True)
        if t1 and result_cnt > 0 and op2 != 17:
            result_cnt = 0
            return state(True)
        if op2 == 0:                                      # dotsection
            return state(True)
        if op2 in (1, 2):                                 # vstem3 / hstem3
            if t1:
                v0, v1, v2 = st.get(0), st.get(2), st.get(4)
                st.set(2, i32(i32(v1 - v0) - st.get(1)))
                st.set(4, i32(i32(v2 - v1) - st.get(3)))
                self._stems(st)
            return state(True)
        if op2 in (3, 4):                                 # and / or
            a2, a1 = st.pop_fixed(), st.pop_fixed()
            st.push_int(int(bool(a1 and a2)) if op2 == 3 else int(bool(a1 or a2)))
            return state(False)
        if op2 == 5:                                      # not
            st.push_int(int(not st.pop_fixed()))
            return state(False)
        if op2 == 6:                                      # seac
            if not t1:
                return state(True)
            achar, bchar = st.pop_int(), st.pop_int()
            ady, adx, asb = st.pop_fixed(), st.pop_fixed(), st.pop_fixed()
            if doing_seac:
                raise GlyphError("nested seac")
            adx = i32(adx + self.lsb_x)
            base, accent = self.face.seac_glyph(bchar), self.face.seac_glyph(achar)
            # the seac glyph's left bearing and advance, which the component loads overwrite
            bearing, advance = (self.lsb_x, self.lsb_y), (self.advance_x, self.advance_y)
            self.interp(base, True, 0, 0)
            if not self.have_width:                       # no (h)sbw of its own: the base glyph's
                bearing, advance = (self.lsb_x, self.lsb_y), (self.advance_x, self.advance_y)
            self.lsb_x = self.lsb_y = 0
            self.interp(accent, True, i32(adx - asb), ady)
            (self.lsb_x, self.lsb_y), (self.advance_x, self.advance_y) = bearing, advance
            return "exit", None
        if op2 == 7:                                      # sbw
            if t1:
                self.advance_y = st.pop_fixed()
                self.advance_x = st.pop_fixed()
                self.have_width = True
                lsb_y, lsb_x = st.pop_fixed(), st.pop_fixed()
                self.lsb_x = i32(self.lsb_x + lsb_x)
                self.lsb_y = i32(self.lsb_y + lsb_y)
                if initial_map_ready:
                    cur_x = i32(cur_x + lsb_x)
                    cur_y = i32(cur_y + lsb_y)
            return state(True)
        if op2 in (9, 14):                                # abs / neg
            a = st.pop_fixed()
            if a < -FIXED_MAX:
                st.push_fixed(FIXED_MAX)
            else:
                st.push_fixed(abs(a) if op2 == 9 else -a)
            return state(False)
        if op2 in (10, 11):                               # add / sub
            b, a = st.pop_fixed(), st.pop_fixed()
            st.push_fixed(i32(a + b) if op2 == 10 else i32(a - b))
            return state(False)
        if op2 == 12:                                     # div
            if t1 and large_int:
                divisor, dividend = st.pop_int(), st.pop_int()
                large_int = False
            else:
                divisor, dividend = st.pop_fixed(), st.pop_fixed()
            st.push_fixed(divfix(dividend, divisor))
            return state(False)
        if op2 == 15:                                     # eq
            b, a = st.pop_fixed(), st.pop_fixed()
            st.push_int(int(a == b))
            return state(False)
        if op2 == 16:                                     # callothersubr
            if not t1:
                return state(True)
            subr_no, arg_cnt = st.pop_int(), st.pop_int()
            count = st.count()
            if (arg_cnt & MASK32) > count:
                raise GlyphError("othersubr underflow")
            known = 0
            result_cnt = 0
            if subr_no == 0:
                if arg_cnt != 3:
                    raise GlyphError("othersubr 0")
                if initial_map_ready and (not self.flex_state or self.num_flex_vectors != 7):
                    raise GlyphError("unexpected flex end")
                st.push_fixed(cur_x)
                st.push_fixed(cur_y)
                known = 2
            elif subr_no == 1:
                if arg_cnt != 0:
                    raise GlyphError("othersubr 1")
                if initial_map_ready:
                    self.flex_state = 1
                    self.num_flex_vectors = 0
            elif subr_no == 2:
                if arg_cnt != 0:
                    raise GlyphError("othersubr 2")
                if initial_map_ready:
                    if not self.flex_state:
                        raise GlyphError("missing flex start")
                    idx = self.num_flex_vectors
                    self.num_flex_vectors += 1
                    if 0 < idx < 7:
                        idx2 = (idx - 3 if idx > 3 else idx) * 2
                        flex_store[idx2 - 2] = cur_x
                        flex_store[idx2 - 1] = cur_y
                        if idx in (3, 6):
                            gp.curve_to(*flex_store)
            elif subr_no == 3:
                if arg_cnt != 1:
                    raise GlyphError("othersubr 3")
                if initial_map_ready:
                    self.n_stems = 0
                    self.mask_valid = False
                    self.mask_new = True
                known = 1
            elif subr_no in (12, 13):
                st.clear()
            elif subr_no in (14, 15, 16, 17, 18):         # multiple masters: blend num_points values
                wv = self.face.weight_vector
                if wv is None:
                    raise GlyphError("multiple master othersubr")   # no blend: Invalid_Glyph_Format
                designs = self.face.num_designs
                num_points = subr_no - 13 + (subr_no == 18)
                if arg_cnt != num_points * designs:
                    raise GlyphError("multiple master arguments")
                # a0 + (a1-a0)*w1 + ... + (ak-a0)*wk, the deltas stored after the num_points bases
                op_idx = count - arg_cnt
                delta, values = op_idx + num_points, op_idx
                for _ in range(num_points):
                    tmp = st.get(values)
                    for mm in range(1, designs):
                        tmp = i32(tmp + mulfix(st.get(delta), wv[mm]))
                        delta += 1
                    st.set(values, tmp)
                    values += 1
                st.pop(arg_cnt - num_points)
                known = num_points
            elif subr_no == 19:                           # WeightVector into BuildCharArray[idx]
                if arg_cnt != 1 or self.face.weight_vector is None:
                    raise GlyphError("invalid othersubr")
                idx = st.pop_int() & MASK32
                n = self.face.len_buildchar
                if n < self.face.num_designs or n - self.face.num_designs < idx:
                    raise GlyphError("invalid othersubr")
                self.face.buildchar[idx:idx + self.face.num_designs] = self.face.weight_vector
            elif subr_no == 24:                           # BuildCharArray[idx] = val
                if arg_cnt != 2 or self.face.weight_vector is None:
                    raise GlyphError("invalid othersubr")
                idx = st.pop_int() & MASK32
                if idx >= self.face.len_buildchar:
                    raise GlyphError("invalid othersubr")
                self.face.buildchar[idx] = st.pop_fixed()
            elif subr_no == 25:                           # push BuildCharArray[idx]
                if arg_cnt != 1 or self.face.weight_vector is None:
                    raise GlyphError("invalid othersubr")
                idx = st.pop_int() & MASK32
                if idx >= self.face.len_buildchar:
                    raise GlyphError("invalid othersubr")
                st.push_fixed(self.face.buildchar[idx])
                known = 1
            elif subr_no in (20, 21, 22, 23):
                if arg_cnt != 2:
                    raise GlyphError("othersubr arithmetic")
                b, a = st.pop_fixed(), st.pop_fixed()
                if subr_no == 20:
                    st.push_fixed(i32(a + b))
                elif subr_no == 21:
                    st.push_fixed(i32(a - b))
                elif subr_no == 22:
                    st.push_fixed(mulfix(a, b))
                else:
                    if b == 0:
                        raise GlyphError("othersubr division by 0")
                    st.push_fixed(divfix(a, b))
                known = 1
            elif subr_no == 27:
                if arg_cnt != 4:
                    raise GlyphError("othersubr 27")
                c2, c1, a2, a1 = st.pop_fixed(), st.pop_fixed(), st.pop_fixed(), st.pop_fixed()
                st.push_fixed(a1 if c1 <= c2 else a2)
                known = 1
            elif subr_no == 28:
                raise Unported("random othersubr")
            elif arg_cnt >= 0 and subr_no >= 0:
                n = min(arg_cnt, 3)
                result_cnt = n
                for i in range(1, n + 1):
                    results[result_cnt - i] = st.pop_fixed()
            else:
                raise GlyphError("invalid othersubr")
            return state(False)
        if op2 == 17:                                     # pop
            if not t1:
                return state(True)
            if known > 0:
                known -= 1
                return state(False)
            if result_cnt == 0:
                raise GlyphError("no othersubr result")
            result_cnt -= 1
            st.push_fixed(results[result_cnt])
            return state(False)
        if op2 == 18:                                     # drop
            st.pop_fixed()
            return state(False)
        if op2 == 20:                                     # put
            idx = st.pop_int() & MASK32
            val = st.pop_fixed()
            if idx < STORAGE_SIZE:
                storage[idx] = val
            return state(False)
        if op2 == 21:                                     # get
            idx = st.pop_int() & MASK32
            if idx < STORAGE_SIZE:
                st.push_fixed(storage[idx])
            return state(False)
        if op2 == 22:                                     # ifelse
            c2, c1, a2, a1 = st.pop_fixed(), st.pop_fixed(), st.pop_fixed(), st.pop_fixed()
            st.push_fixed(a1 if c1 <= c2 else a2)
            return state(False)
        if op2 == 23:                                     # random
            raise Unported("random operator")
        if op2 == 24:                                     # mul
            b, a = st.pop_fixed(), st.pop_fixed()
            st.push_fixed(mulfix(a, b))
            return state(False)
        if op2 == 26:                                     # sqrt
            a = st.pop_fixed()
            st.push_fixed(sqrt_fixed(a) if a > 0 else 0)
            return state(False)
        if op2 == 27:                                     # dup
            a = st.pop_fixed()
            st.push_fixed(a)
            st.push_fixed(a)
            return state(False)
        if op2 == 28:                                     # exch
            b, a = st.pop_fixed(), st.pop_fixed()
            st.push_fixed(b)
            st.push_fixed(a)
            return state(False)
        if op2 == 29:                                     # index
            idx = st.pop_int()
            size = st.count()
            if size > 0:
                g = size - 1 if idx < 0 else 0 if idx >= size else size - 1 - idx
                st.push_fixed(st.get(g))
            return state(False)
        if op2 == 30:                                     # roll
            idx = st.pop_int()
            count = st.pop_int()
            st.roll(count, idx)
            return state(False)
        if op2 == 33:                                     # setcurrentpoint
            if t1 and initial_map_ready:
                cur_y = st.pop_fixed()
                cur_x = st.pop_fixed()
                self.flex_state = 0
            return state(True)
        return state(True)

    def _flex(self, st: Stack, gp: GlyphPath, cur_x: int, cur_y: int, rfs, conditional: bool):
        vals = [0] * 14
        vals[0], vals[1] = cur_x, cur_y
        idx = 0
        hflex = not rfs[9]
        top = 9 if hflex else 10
        for i in range(top):
            vals[i + 2] = vals[i]
            if rfs[i]:
                vals[i + 2] = i32(vals[i + 2] + st.get(idx))
                idx += 1
        if hflex:
            vals[11] = cur_y
        if conditional:
            last_is_x = abs(i32(vals[10] - cur_x)) > abs(i32(vals[11] - cur_y))
            last = st.get(idx)
            if last_is_x:
                vals[12], vals[13] = i32(vals[10] + last), cur_y
            else:
                vals[12], vals[13] = cur_x, i32(vals[11] + last)
        else:
            if rfs[10]:
                vals[12] = i32(vals[10] + st.get(idx))
                idx += 1
            else:
                vals[12] = cur_x
            vals[13] = i32(vals[11] + st.get(idx)) if rfs[11] else cur_y
        for j in range(2):
            gp.curve_to(*vals[j * 6 + 2:j * 6 + 8])
        st.clear()
        return vals[12], vals[13]


def face_of(font) -> Face:
    """The Face of a pure `fonts.Font`, made once per font object."""
    face = font.__dict__.get("_b2s_face")
    if face is None:
        prog = font.program
        if prog is not None and font.embedded and prog.kind == "truetype":
            from .truetype import TrueTypeFace
            face = TrueTypeFace(font)
        else:
            face = Face(font)
        font.__dict__["_b2s_face"] = face
    return face
