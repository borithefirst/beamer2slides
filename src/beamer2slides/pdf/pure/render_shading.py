"""Shadings as PDFium draws them: CPDF_RenderShading (axial and radial), the shading patterns
paths are filled or stroked with (DrawShadingPattern), the functions they are coloured by
(CPDF_Function types 0, 2, 3 and 4, the PostScript calculator included) and the colour spaces
those feed (Device Gray/RGB/CMYK, CalGray, Separation, DeviceN), value for value.

PDFium computes a shading's 256 colour steps once (GetShadingSteps), then every pixel of a BGRA
buffer the size of the clipped object: the pixel's centre-less position (column, row) goes back
through the inverted matrix in float, becomes a parameter, and the parameter an index into the
steps. The buffer is composited into the page like any bitmap (SetDIBits: CompositeRow_Argb2Rgb /
Argb2Argb under the clip mask). Everything here is float32 one C operation at a time, the float to
int conversions are x86's (cvttss2si: INT_MIN for NaN and out of range), and the transcendental
functions are the C runtime's own (ucrtbase, the one PDFium links on Windows).

What is not drawn exactly is refused (`refusal`), never approximated: function-based and mesh
shadings (types 1, 4-7), tiling patterns, CalRGB / Lab / ICCBased / Indexed colour spaces,
PostScript functions with words that are not plain numbers or operators, and a shading whose
validation fails with a valid type (PDFium's Load then succeeds on a second call only, so what is
drawn depends on history)."""

from __future__ import annotations

import ctypes
import re
from fractions import Fraction

import numpy as np

from . import raster as R
from .colors import adobe_cmyk_to_srgb
from .raster import F
from .syntax import Name, Stream, String

FILL_NONE, FILL_EVENODD, FILL_WINDING = 0, 1, 2
INT_MIN, INT_MAX = -(1 << 31), (1 << 31) - 1
U32 = 0xFFFFFFFF
FLT_MAX = float(np.finfo(np.float32).max)
PI_F = F(3.1415926535897932384626433832795)     # FXSYS_PI

_crt = ctypes.CDLL("ucrtbase")


def _cfun(name, n):
    fn = getattr(_crt, name)
    fn.restype = ctypes.c_float
    fn.argtypes = [ctypes.c_float] * n
    return fn


_powf, _hypotf, _atan2f = _cfun("powf", 2), _cfun("_hypotf", 2), _cfun("atan2f", 2)
_sinf, _cosf, _logf, _log10f = _cfun("sinf", 1), _cfun("cosf", 1), _cfun("logf", 1), _cfun("log10f", 1)


class Unsupported(Exception):
    """Something PDFium draws that this module does not (the page is refused)."""


# ---------------------------------------------------------------------- C conversions


def i32(v: float) -> int:
    """static_cast<int32_t>(float) as x86 does it (cvttss2si)."""
    if v != v or v >= 2147483648.0 or v < -2147483648.0:
        return INT_MIN
    return int(v)


def u32(v: float) -> int:
    """static_cast<uint32_t>(float) as clang does it on x64 (64-bit cvttss2si, low half)."""
    if v != v or v >= 9223372036854775808.0 or v < -9223372036854775808.0:
        return 0
    return int(v) & U32


def sat_int(v: float) -> int:
    """pdfium::saturated_cast<int>(float)."""
    if v != v:
        return 0
    if v >= 2147483647.0:
        return INT_MAX
    if v <= -2147483648.0:
        return INT_MIN
    return int(v)


def roundf(v: float) -> int:
    """FXSYS_roundf."""
    if v != v:
        return 0
    if v < -2147483648.0:
        return INT_MIN
    if v >= 2147483648.0:
        return INT_MAX
    r = int(abs(v) + 0.5) if abs(v) < 4503599627370496.0 else int(abs(v))
    return r if v >= 0 else -r


def clamp(v: float, lo: float, hi: float) -> float:
    """std::clamp (a NaN passes through)."""
    return lo if v < lo else hi if hi < v else v


def argb(a: int, r: int, g: int, b: int) -> int:
    """ArgbEncode on uint32 arguments."""
    return (((a & U32) << 24) | ((r & U32) << 16) | ((g & U32) << 8) | (b & U32)) & U32


def outer(rect) -> tuple:
    """CFX_FloatRect::GetOuterRect, NaN and infinity included (saturated_cast)."""
    def fl(v):
        return 0 if v != v else INT_MIN if v <= -2147483648.0 else INT_MAX if v >= 2147483647.0 else int(np.floor(v))

    def ce(v):
        return 0 if v != v else INT_MIN if v <= -2147483648.0 else INT_MAX if v >= 2147483647.0 else int(np.ceil(v))

    l, b, r, t = rect
    x0, y0, x1, y1 = fl(l), fl(b), ce(r), ce(t)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def fx_intersect(a, b) -> tuple:
    """FX_RECT::Intersect (both normalised; an empty result is all zeros)."""
    a = (min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3]))
    b = (min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))
    l, t, r, bt = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if l > r or t > bt:
        return 0, 0, 0, 0
    return l, t, r, bt


def float_intersect(a, b) -> tuple:
    """CFX_FloatRect::Intersect (both normalised)."""
    a = (min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3]))
    b = (min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))
    l, bt, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if l > r or bt > t:
        return 0.0, 0.0, 0.0, 0.0
    return l, bt, r, t


# ---------------------------------------------------------------------- object access


class _Access:
    """CPDF_Dictionary / CPDF_Array getters over the pure reader's objects."""

    def __init__(self, doc):
        self.doc = doc

    def r(self, v):
        return self.doc.resolve(v)

    @staticmethod
    def dict_of(obj):
        """GetDict: a dictionary, or a stream's."""
        if isinstance(obj, Stream):
            return obj.dict
        return obj if isinstance(obj, dict) else None

    def number(self, v) -> float:
        """GetNumber of a direct object (0 for anything but a number)."""
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 0.0
        return F(float(v))

    def float_at(self, arr, i: int) -> float:
        """CPDF_Array::GetFloatAt."""
        if not isinstance(arr, list) or i >= len(arr):
            return 0.0
        return self.number(self.r(arr[i]))

    def integer(self, v) -> int:
        """GetInteger of a direct object."""
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return max(INT_MIN, min(INT_MAX, v))
        if isinstance(v, float):
            return sat_int(F(v))
        return 0

    def integer_for(self, d, key) -> int:
        return self.integer(self.r(d.get(key))) if isinstance(d, dict) else 0

    def integer_at(self, arr, i: int) -> int:
        return self.integer(self.r(arr[i])) if isinstance(arr, list) and i < len(arr) else 0

    def array_for(self, d, key):
        v = self.r(d.get(key)) if isinstance(d, dict) else None
        return v if isinstance(v, list) else None

    def boolean_at(self, arr, i: int, default: bool) -> bool:
        if not isinstance(arr, list) or i >= len(arr):
            return default
        v = self.r(arr[i])
        return bool(v) if isinstance(v, bool) else default

    def matrix_for(self, d, key) -> tuple:
        """GetMatrixFor: exactly six numbers, else the identity."""
        a = self.array_for(d, key)
        if a is None or len(a) != 6:
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        return tuple(self.float_at(a, i) for i in range(6))

    def rect_for(self, d, key) -> tuple:
        """GetRectFor: exactly four numbers, else zeros; not normalised."""
        a = self.array_for(d, key)
        if a is None or len(a) != 4:
            return 0.0, 0.0, 0.0, 0.0
        return tuple(self.float_at(a, i) for i in range(4))

    @staticmethod
    def string(v) -> str:
        """GetString of a direct object, for a colour space family."""
        if isinstance(v, Name):
            return str(v)
        if isinstance(v, (String, bytes)):
            return bytes(v).decode("latin-1")
        return ""


# ---------------------------------------------------------------------- functions


def interpolate(x, xmin, xmax, ymin, ymax) -> float:
    """CPDF_Function::Interpolate."""
    divisor = F(xmax - xmin)
    if divisor != 0.0:      # a NaN divides too
        return F(ymin + F(F(F(x - xmin) * F(ymax - ymin)) / divisor))
    return F(ymin + 0.0)


class Function:
    """CPDF_Function: Init's domains and ranges, Call's clamping."""

    inputs = outputs = 0
    domains: list
    ranges: list

    def call(self, inputs: list, results: list, off: int) -> int | None:
        if len(inputs) != self.inputs:
            return None
        clamped = []
        for i in range(self.inputs):
            d1, d2 = self.domains[2 * i], self.domains[2 * i + 1]
            if d1 > d2:
                return None
            clamped.append(clamp(inputs[i], d1, d2))
        if not self.v_call(clamped, results, off):
            return None
        if not self.ranges:
            return self.outputs
        for i in range(self.outputs):
            r1, r2 = self.ranges[2 * i], self.ranges[2 * i + 1]
            if r1 > r2:
                return None
            results[off + i] = clamp(results[off + i], r1, r2)
        return self.outputs

    def v_call(self, x: list, results: list, off: int) -> bool:
        raise NotImplementedError


class ExpInt(Function):
    def v_init(self, a: _Access, obj, d, visited) -> bool:
        n = d.get("N")                      # GetNumberFor: not resolved
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return False
        self.exponent = F(float(n))
        c0, c1 = a.array_for(d, "C0"), a.array_for(d, "C1")
        if c0 is not None and self.outputs == 0:
            self.outputs = len(c0)
        if self.outputs == 0:
            self.outputs = 1
        self.begin = [a.float_at(c0, i) if c0 is not None else 0.0 for i in range(self.outputs)]
        self.end = [a.float_at(c1, i) if c1 is not None else 1.0 for i in range(self.outputs)]
        self.diff = [F(e - b) for b, e in zip(self.begin, self.end)]
        total = self.outputs * self.inputs
        if total > U32:
            return False
        self.orig = self.outputs
        self.outputs = total
        return True

    def v_call(self, x, results, off) -> bool:
        for i in range(self.inputs):
            p = _powf(x[i], self.exponent)
            for j in range(self.orig):
                results[off + i * self.orig + j] = F(self.begin[j] + F(p * self.diff[j]))
        return True


def _get_bits(data: bytes, pos: int, n: int) -> int | None:
    """CFX_BitStream::GetBits: None where it returns 0 without moving."""
    size = len(data) * 8
    if n > size or pos > size - n:
        return None
    first, last = pos // 8, (pos + n + 7) // 8
    v = int.from_bytes(data[first:last], "big")
    return (v >> (last * 8 - pos - n)) & ((1 << n) - 1)


def _fits(v: int) -> bool:
    return INT_MIN <= v <= INT_MAX


class Sampled(Function):
    def v_init(self, a: _Access, obj, d, visited) -> bool:
        if not isinstance(obj, Stream):
            return False
        size = a.array_for(d, "Size")
        if not size:
            return False
        self.bps = a.integer_for(d, "BitsPerSample") & U32
        if self.bps not in (1, 2, 4, 8, 12, 16, 24, 32):
            return False
        total = self.bps * self.outputs
        ok = total <= U32
        encode = a.array_for(d, "Encode")
        self.sizes, self.emin, self.emax = [], [], []
        for i in range(self.inputs):
            s = a.integer_at(size, i)
            if s <= 0:
                return False
            self.sizes.append(s)
            total *= s
            ok = ok and total <= U32
            if encode is not None:
                self.emin.append(a.float_at(encode, 2 * i))
                self.emax.append(a.float_at(encode, 2 * i + 1))
            else:
                self.emin.append(0.0)
                self.emax.append(F(float(1 if s == 1 else s - 1)))
        nbytes = (total + 7) // 8
        if not ok or total + 7 > U32 or nbytes == 0:
            return False
        self.sample_max = F(float(0xFFFFFFFF >> (32 - self.bps)))
        self.data = bytes(a.doc.stream_data(obj))
        if nbytes > len(self.data):
            return False
        decode = a.array_for(d, "Decode")
        self.dmin, self.dmax = [], []
        for i in range(self.outputs):
            if decode is not None:
                self.dmin.append(a.float_at(decode, 2 * i))
                self.dmax.append(a.float_at(decode, 2 * i + 1))
            else:
                self.dmin.append(self.ranges[2 * i])
                self.dmax.append(self.ranges[2 * i + 1])
        return True

    def v_call(self, x, results, off) -> bool:
        pos = 0
        enc, index, block = [], [], []
        for i in range(self.inputs):
            block.append(1 if i == 0 else (block[i - 1] * self.sizes[i - 1]) & U32)
            e = interpolate(x[i], self.domains[2 * i], self.domains[2 * i + 1], self.emin[i], self.emax[i])
            enc.append(e)
            index.append(min(max(u32(e), 0), (self.sizes[i] - 1) & U32))
            pos = ((pos + index[i] * block[i]) & U32)
            pos = pos - (1 << 32) if pos > INT_MAX else pos
        bits_out = self.outputs * self.bps
        if bits_out > INT_MAX:
            return False
        skip = pos * bits_out
        if not _fits(skip) or skip < 0:
            return False
        if not _fits(skip + bits_out):
            return False
        data = self.data
        if not data:
            return False
        cur = skip
        for i in range(self.outputs):
            sample = _get_bits(data, cur, self.bps)
            if sample is None:
                sample = 0
            else:
                cur += self.bps
            fs = F(float(sample))
            encoded = fs
            for j in range(self.inputs):
                if index[j] == self.sizes[j] - 1:
                    if index[j] == 0:
                        encoded = F(enc[j] * fs)
                else:
                    b2 = block[j]
                    if b2 > INT_MAX:
                        return False
                    b2 += pos
                    if not _fits(b2):
                        return False
                    b2 *= self.outputs
                    if not _fits(b2):
                        return False
                    b2 += i
                    if not _fits(b2):
                        return False
                    b2 *= self.bps
                    if not _fits(b2) or b2 < 0:
                        return False
                    s2 = _get_bits(data, b2, self.bps)
                    s2 = F(float(s2 or 0))
                    encoded = F(encoded + F(F(enc[j] - F(float(index[j]))) * F(s2 - fs)))
            results[off + i] = interpolate(encoded, 0.0, self.sample_max, self.dmin[i], self.dmax[i])
        return True


class Stitch(Function):
    def v_init(self, a: _Access, obj, d, visited) -> bool:
        if self.inputs != 1:
            return False
        funcs, bounds, encode = a.array_for(d, "Functions"), a.array_for(d, "Bounds"), a.array_for(d, "Encode")
        if funcs is None or bounds is None or encode is None:
            return False
        n = len(funcs)
        if n == 0 or len(bounds) < n - 1 or len(encode) < 2 * n:
            return False
        self.subs = []
        outputs = None
        for i in range(n):
            sub = a.r(funcs[i])
            if sub is obj:
                return False
            f = load_function(a, sub, visited)
            if f is None or f.inputs != 1 or f.outputs == 0:
                return False
            if outputs is not None and outputs != f.outputs:
                return False
            outputs = f.outputs
            self.subs.append(f)
        self.outputs = outputs
        self.bounds = [self.domains[0]] + [a.float_at(bounds, i) for i in range(n - 1)] + [self.domains[1]]
        self.encode = [a.float_at(encode, i) for i in range(2 * n)]
        return True

    def v_call(self, x, results, off) -> bool:
        v = x[0]
        i = 0
        while i + 1 < len(self.subs):
            if v < self.bounds[i + 1]:
                break
            i += 1
        v = interpolate(v, self.bounds[i], self.bounds[i + 1], self.encode[2 * i], self.encode[2 * i + 1])
        return self.subs[i].call([v], results, off) is not None


class PostScript(Function):
    def v_init(self, a: _Access, obj, d, visited) -> bool:
        if not isinstance(obj, Stream):
            raise Unsupported("PostScript functions without a stream")
        self.proc = _ps_parse(bytes(a.doc.stream_data(obj)))
        return self.proc is not None

    def v_call(self, x, results, off) -> bool:
        stack: list = []
        for v in x:
            _push(stack, v)
        _ps_execute(self.proc, stack)
        if len(stack) < self.outputs:
            return False
        for i in range(self.outputs):
            results[off + self.outputs - i - 1] = _pop(stack)
        return True


_TYPES = {0: Sampled, 2: ExpInt, 3: Stitch, 4: PostScript}


def load_function(a: _Access, obj, visited: set) -> Function | None:
    """CPDF_Function::Load of a direct object, `visited` holding the ids being loaded."""
    if obj is None or id(obj) in visited:
        return None
    visited.add(id(obj))
    try:
        d = _Access.dict_of(obj)
        kind = a.integer_for(d, "FunctionType") if d is not None else -1
        cls = _TYPES.get(kind)
        if cls is None:
            return None
        f = cls()
        domains = a.array_for(d, "Domain")
        if domains is None:
            return None
        f.inputs = len(domains) // 2
        if f.inputs == 0:
            return None
        f.domains = [a.float_at(domains, i) for i in range(2 * f.inputs)]
        rng = a.array_for(d, "Range")
        f.outputs = len(rng) // 2 if rng is not None else 0
        if kind in (0, 4) and f.outputs == 0:
            return None
        f.ranges = [a.float_at(rng, i) for i in range(2 * f.outputs)] if f.outputs > 0 else []
        old = f.outputs
        if not f.v_init(a, obj, d, visited):
            return None
        if f.ranges and f.outputs > old:
            f.ranges = f.ranges + [0.0] * (2 * f.outputs - len(f.ranges))
        return f
    finally:
        visited.discard(id(obj))


# ---- PostScript calculator (CPDF_PSEngine)

_WHITE = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"
_OPS = ("abs add and atan bitshift ceiling copy cos cvi cvr div dup eq exch exp false floor ge gt idiv if "
        "ifelse index le ln log lt mod mul ne neg not or pop roll round sin sqrt sub true truncate xor").split()
_NUMBER = re.compile(rb"-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_PROC = "proc"


class _Words:
    """CPDF_SimpleParser::GetWord."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def word(self) -> bytes:
        data, n = self.data, len(self.data)
        while True:
            if self.pos >= n:
                return b""
            c = data[self.pos]
            self.pos += 1
            while c in _WHITE:
                if self.pos >= n:
                    return b""
                c = data[self.pos]
                self.pos += 1
            if c != 0x25:  # %
                break
            while True:
                if self.pos >= n:
                    return b""
                c = data[self.pos]
                self.pos += 1
                if c in b"\r\n":
                    break
        start = self.pos - 1
        if c not in _DELIM:
            while self.pos < n and data[self.pos] not in _DELIM and data[self.pos] not in _WHITE:
                self.pos += 1
            return data[start:self.pos]
        if c == 0x2F:  # /
            while self.pos < n:
                if data[self.pos] in _WHITE or data[self.pos] in _DELIM:
                    return data[start:self.pos]
                self.pos += 1
            return b""
        if c == 0x3C:  # <
            if self.pos >= n:
                return data[start:self.pos]
            c = data[self.pos]
            self.pos += 1
            if c == 0x3C:
                return data[start:self.pos]
            while self.pos < n and c != 0x3E:
                c = data[self.pos]
                self.pos += 1
            return data[start:self.pos]
        if c == 0x3E:  # >
            if self.pos < n and data[self.pos] == 0x3E:
                self.pos += 1
            return data[start:self.pos]
        if c == 0x28:  # (
            level = 1
            while self.pos < n and level > 0:
                c = data[self.pos]
                self.pos += 1
                if c == 0x28:
                    level += 1
                elif c == 0x29:
                    level -= 1
            return data[start:self.pos]
        return data[start:self.pos]


def _decimal_f32(text: bytes) -> float:
    """fast_float::from_chars<float> of a plain decimal: correctly rounded (ties to even)."""
    fr = Fraction(text.decode("ascii"))
    if fr == 0:
        return -0.0 if text.startswith(b"-") else 0.0
    d = fr.numerator / fr.denominator            # correctly rounded double
    c = np.float32(d)
    if not np.isfinite(c) or abs(float(c)) < 1.1754943508222875e-38:
        raise Unsupported("a PostScript number out of float range")
    best = None
    for k in (np.nextafter(c, np.float32(-np.inf)), c, np.nextafter(c, np.float32(np.inf))):
        if not np.isfinite(k):
            continue
        err = abs(Fraction(float(k)) - fr)
        even = (int(np.array(k, np.float32).view(np.uint32)) & 1) == 0
        if best is None or err < best[0] or (err == best[0] and even):
            best = (err, float(k))
    return best[1]


def _string_to_float(word: bytes) -> float:
    """StringToFloat, for the words it reads exactly: plain decimal numbers."""
    s = 0
    while s < len(word) and word[s] in b" +-":
        s += 1
    if s > 0 and word[s - 1] == 0x2D:
        s -= 1
    body = word[s:]
    if not _NUMBER.fullmatch(body):
        raise Unsupported("a PostScript word that is neither an operator nor a plain number")
    return _decimal_f32(body)


def _ps_parse(data: bytes):
    """CPDF_PSEngine::Parse: a list of operators (str), constants (float), procedures (lists)."""
    words = _Words(data)
    if words.word() != b"{":
        return None
    return _ps_proc(words, 0)


def _ps_proc(words: _Words, depth: int):
    if depth > 128:
        return None
    ops: list = []
    while True:
        w = words.word()
        if not w:
            return None
        if w == b"}":
            return ops
        if w == b"{":
            sub = _ps_proc(words, depth + 1)
            if sub is None:
                return None
            ops.append(sub)
            continue
        name = w.decode("latin-1")
        ops.append(name if name in _OPS else _string_to_float(w))


def _push(stack: list, v: float) -> None:
    if len(stack) < 100:
        stack.append(v)


def _pop(stack: list) -> float:
    return stack.pop() if stack else 0.0


def _pop_int(stack: list) -> int:
    return sat_int(_pop(stack))


def _ps_execute(ops: list, stack: list) -> bool:
    for i, op in enumerate(ops):
        if isinstance(op, list):
            continue
        if isinstance(op, float):
            _push(stack, op)
            continue
        if op == "if":
            if i == 0 or not isinstance(ops[i - 1], list):
                return False
            if _pop_int(stack):
                _ps_execute(ops[i - 1], stack)
        elif op == "ifelse":
            if i < 2 or not isinstance(ops[i - 1], list) or not isinstance(ops[i - 2], list):
                return False
            _ps_execute(ops[i - 2] if _pop_int(stack) else ops[i - 1], stack)
        else:
            _ps_operator(op, stack)
    return True


def _cdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _cmod(a: int, b: int) -> int:
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def _ps_operator(op: str, st: list) -> None:
    pop, push, pop_int = _pop, _push, _pop_int
    with np.errstate(all="ignore"):
        if op == "add":
            d1 = pop(st)
            d2 = pop(st)
            push(st, F(d1 + d2))
        elif op == "sub":
            d2 = pop(st)
            d1 = pop(st)
            push(st, F(d1 - d2))
        elif op == "mul":
            d1 = pop(st)
            d2 = pop(st)
            push(st, F(d1 * d2))
        elif op == "div":
            d2 = pop(st)
            d1 = pop(st)
            push(st, float(np.float32(d1) / np.float32(d2)) if d2 != 0.0 else 0.0)
        elif op in ("idiv", "mod"):
            i2 = pop_int(st)
            i1 = pop_int(st)
            if not i2 or (i1 == INT_MIN and i2 == -1):
                push(st, 0.0)
            else:
                push(st, F(float(_cdiv(i1, i2) if op == "idiv" else _cmod(i1, i2))))
        elif op == "neg":
            push(st, -pop(st))
        elif op == "abs":
            push(st, abs(pop(st)))
        elif op == "ceiling":
            push(st, float(np.ceil(np.float32(pop(st)))))
        elif op == "floor":
            push(st, float(np.floor(np.float32(pop(st)))))
        elif op == "round":
            d1 = pop(st)
            if d1 != d1:
                push(st, 0.0)
            elif d1 > FLT_MAX:
                push(st, FLT_MAX)
            else:
                push(st, float(np.floor(np.float32(F(d1 + 0.5)))))
        elif op in ("truncate", "cvi"):
            push(st, F(float(pop_int(st))))
        elif op == "sqrt":
            push(st, float(np.sqrt(np.float32(pop(st)))))
        elif op == "sin":
            push(st, _sinf(F(F(pop(st) * PI_F) / 180.0)))
        elif op == "cos":
            push(st, _cosf(F(F(pop(st) * PI_F) / 180.0)))
        elif op == "atan":
            d2 = pop(st)
            d1 = pop(st)
            d = F(_atan2f(d1, d2) * 180.0 / PI_F)
            if d < 0:
                d = F(d + 360.0)
            push(st, d)
        elif op == "exp":
            d2 = pop(st)
            d1 = pop(st)
            push(st, _powf(d1, d2))
        elif op == "ln":
            push(st, _logf(pop(st)))
        elif op == "log":
            push(st, _log10f(pop(st)))
        elif op == "cvr":
            pass
        elif op in ("eq", "ne", "gt", "ge", "lt", "le"):
            d2 = pop(st)
            d1 = pop(st)
            r = {"eq": d1 == d2, "ne": d1 != d2, "gt": d1 > d2, "ge": d1 >= d2, "lt": d1 < d2, "le": d1 <= d2}[op]
            push(st, 1.0 if r else 0.0)
        elif op in ("and", "or", "xor"):
            i1 = pop_int(st)
            i2 = pop_int(st)
            push(st, F(float(i1 & i2 if op == "and" else i1 | i2 if op == "or" else i1 ^ i2)))
        elif op == "not":
            push(st, 0.0 if pop_int(st) else 1.0)
        elif op == "bitshift":
            shift = pop_int(st)
            x = pop_int(st)
            if shift > 0:
                if x >= 0 and shift < 31:
                    v = (x << shift) & U32
                    v = v - (1 << 32) if v > INT_MAX else v
                    res = v if (v >> shift) == x else 0
                else:
                    res = 0
            else:
                n = 0 if shift == INT_MIN else -shift
                res = 0 if n >= 32 else x >> n
            push(st, F(float(res)))
        elif op == "true":
            push(st, 1.0)
        elif op == "false":
            push(st, 0.0)
        elif op == "pop":
            pop(st)
        elif op == "exch":
            d2 = pop(st)
            d1 = pop(st)
            push(st, d2)
            push(st, d1)
        elif op == "dup":
            d1 = pop(st)
            push(st, d1)
            push(st, d1)
        elif op == "copy":
            n = pop_int(st)
            if n < 0 or len(st) + n > 100 or n > len(st):
                return
            st.extend(st[len(st) - n:])
        elif op == "index":
            n = pop_int(st)
            if n < 0 or n >= len(st):
                return
            push(st, st[len(st) - n - 1])
        elif op == "roll":
            j = pop_int(st)
            n = pop_int(st)
            if j == 0 or n == 0 or not st:
                return
            if n < 0 or n > len(st):
                return
            j = _cmod(j, n)
            if j > 0:
                j -= n
            seg = st[len(st) - n:]
            st[len(st) - n:] = seg[-j:] + seg[:-j]


# ---------------------------------------------------------------------- colour spaces


class ColorSpace:
    """CPDF_ColorSpace for what shadings read: family, component count, GetRGB."""

    def __init__(self, family: str, n: int, base=None, func=None, none=False):
        self.family, self.n, self.base, self.func, self.none = family, n, base, func, none

    @property
    def special(self) -> bool:
        return self.family in ("Pattern", "Indexed", "Separation", "DeviceN")

    def rgb(self, buf: list):
        f = self.family
        if f == "DeviceGray":
            g = clamp(buf[0], 0.0, 1.0)
            return g, g, g
        if f == "DeviceRGB":
            return clamp(buf[0], 0.0, 1.0), clamp(buf[1], 0.0, 1.0), clamp(buf[2], 0.0, 1.0)
        if f == "DeviceCMYK":
            q = [clamp(v, 0.0, 1.0) for v in buf[:4]]
            ints = [i32(F(F(v * 255.0) + 0.49999997)) & 0xFF for v in q]
            k = F(1.0 / 255)
            return tuple(F(v * k) for v in adobe_cmyk_to_srgb(*ints))
        if f == "CalGray":
            return buf[0], buf[0], buf[0]
        if f == "Separation":
            if self.none:
                return None
            if self.func is None:
                if self.base is None:
                    return None
                return self.base.rgb([buf[0]] * self.base.n)
            results = [0.0] * max(self.func.outputs, 16)
            if not self.func.call(buf[:1], results, 0):
                return None
            return self.base.rgb(results) if self.base is not None else None
        if f == "DeviceN":
            if self.func is None:
                return None
            results = [0.0] * max(self.func.outputs, 16)
            if not self.func.call(buf[:self.n], results, 0):
                return None
            return self.base.rgb(results)
        return None

    def rgb_or_zeros(self, buf: list):
        v = self.rgb(buf)
        return (0.0, 0.0, 0.0) if v is None else v


_STOCK = {"DeviceRGB": ("DeviceRGB", 3), "RGB": ("DeviceRGB", 3), "DeviceGray": ("DeviceGray", 1),
          "G": ("DeviceGray", 1), "DeviceCMYK": ("DeviceCMYK", 4), "CMYK": ("DeviceCMYK", 4),
          "Pattern": ("Pattern", 1)}


def _stock(name: str) -> ColorSpace | None:
    s = _STOCK.get(name)
    return ColorSpace(*s) if s else None


def get_colorspace(a: _Access, obj) -> ColorSpace | None:
    """CPDF_DocPageData::GetColorSpace(obj, nullptr)."""
    return _cs_internal(a, obj, set(), set())


def _cs_internal(a: _Access, obj, visited: set, internal: set):
    if obj is None or id(obj) in internal:
        return None
    internal.add(id(obj))
    try:
        if isinstance(obj, Name):
            return _stock(str(obj))
        if not isinstance(obj, list) or not obj:
            return None
        if len(obj) == 1:
            return _cs_internal(a, a.r(obj[0]), visited, internal)
        return _cs_load(a, obj, visited)
    finally:
        internal.discard(id(obj))


def _cs_load(a: _Access, obj, visited: set):
    """CPDF_ColorSpace::Load."""
    if obj is None or id(obj) in visited:
        return None
    visited.add(id(obj))
    try:
        if isinstance(obj, Name):
            return _stock(str(obj))
        if isinstance(obj, Stream):
            for key in sorted(obj.dict, key=lambda k: str(k).encode("latin-1")):
                v = obj.dict[key]
                if isinstance(v, Name) and _stock(str(v)) is not None:
                    return _stock(str(v))
            return None
        if not isinstance(obj, list) or not obj:
            return None
        fam = a.r(obj[0])
        if fam is None:
            return None
        family = _Access.string(fam)
        if len(obj) == 1:
            return _stock(family)
        key = (family.encode("latin-1") + b"\0\0\0\0")[:4]
        if key == b"CalG":
            return _calgray(a, obj)
        if key == b"Sepa":
            return _separation(a, obj, visited)
        if key == b"Devi":
            return _devicen(a, obj, visited)
        if key in (b"CalR", b"Lab\0", b"ICCB", b"Inde", b"I\0\0\0", b"Patt"):
            raise Unsupported(f"{family} colour spaces in shadings")
        return None
    finally:
        visited.discard(id(obj))


def _calgray(a: _Access, obj):
    d = _Access.dict_of(a.r(obj[1]))
    if d is None:
        return None
    wp = a.array_for(d, "WhitePoint")
    if wp is None or len(wp) != 3:
        return None
    w = [a.float_at(wp, i) for i in range(3)]
    if not (w[0] > 0 and w[1] == 1.0 and w[2] > 0):
        return None
    return ColorSpace("CalGray", 1)


def _separation(a: _Access, obj, visited):
    name = a.r(obj[1])
    if _Access.string(name) == "None" and isinstance(name, (Name, String, bytes)):
        return ColorSpace("Separation", 1, none=True)
    alt = a.r(obj[2]) if len(obj) > 2 else None
    if alt is obj:
        return None
    base = _cs_load(a, alt, visited)
    if base is None or base.special:
        return None
    fobj = a.r(obj[3]) if len(obj) > 3 else None
    func = None
    if fobj is not None and not isinstance(fobj, Name):
        f = load_function(a, fobj, set())
        if f is not None and f.outputs >= base.n:
            func = f
    return ColorSpace("Separation", 1, base, func)


def _devicen(a: _Access, obj, visited):
    names = a.r(obj[1])
    if not isinstance(names, list):
        return None
    alt = a.r(obj[2]) if len(obj) > 2 else None
    if alt is None or alt is obj:
        return None
    base = _cs_load(a, alt, visited)
    func = load_function(a, a.r(obj[3]) if len(obj) > 3 else None, set())
    if base is None or func is None or base.special or func.outputs < base.n:
        return None
    if not names:
        return None
    return ColorSpace("DeviceN", len(names), base, func)


# ---------------------------------------------------------------------- shading patterns


class Record:
    """A CPDF_ShadingPattern (`sh` resource or PatternType 2) or a tiling pattern, as the
    document's pattern cache holds it."""

    def __init__(self, doc, obj, kind: str, parent_matrix: tuple):
        self.a = _Access(doc)
        self.obj, self.kind, self.parent_matrix = obj, kind, parent_matrix
        self._loaded = None
        self.reason: str | None = None
        self.pattern_to_form = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        if kind == "pattern":
            self.pattern_to_form = R.concat(self.a.matrix_for(_Access.dict_of(obj), "Matrix"), parent_matrix)

    def load(self) -> bool:
        """CPDF_ShadingPattern::Load (true: drawn; false: nothing; a refusal sets `reason`)."""
        if self._loaded is None:
            try:
                self._loaded = self._load()
            except Unsupported as e:
                self.reason, self._loaded = str(e), False
        return self._loaded

    def _load(self) -> bool:
        a = self.a
        if self.kind == "tiling":
            raise Unsupported("tiling patterns")
        if self.kind == "?":
            raise Unsupported("a pattern state PDFium's parser keeps differently")
        sobj = self.obj if self.kind == "shading" else a.r(_Access.dict_of(self.obj).get("Shading"))
        sd = _Access.dict_of(sobj)
        if sd is None:
            return False
        self.shading, self.dict = sobj, sd
        funcs = []
        f = a.r(sd.get("Function"))
        if f is not None:
            if isinstance(f, list):
                funcs = [load_function(a, a.r(f[i]), set()) for i in range(min(len(f), 4))]
            else:
                funcs = [load_function(a, f, set())]
        cs_obj = a.r(sd.get("ColorSpace"))
        if cs_obj is None:
            return False
        cs = get_colorspace(a, cs_obj)
        if cs is None or cs.family == "Pattern":
            return False
        stype = a.integer_for(sd, "ShadingType")
        if not 1 <= stype <= 7:
            return False
        self.cs, self.funcs, self.type = cs, funcs, stype
        if not self._validate():
            raise Unsupported("a shading that fails validation (PDFium draws it on a second Load only)")
        if stype not in (2, 3):
            raise Unsupported("function-based and mesh shadings")
        return True

    def shade_load(self) -> bool:
        """CPDF_ShadingPattern::Load as Handle_ShadeFill calls it, for the page object: the
        dictionary, the functions, a colour space that is no Pattern space, a type from 1 to 7,
        then Validate. Once the type was read, Load answers true without validating again, and
        the document keeps the pattern: a shading that fails Validate is drawn from its second
        `sh` on (measured on a mutated beamer ball)."""
        if getattr(self, "_typed", False):
            return True
        a = self.a
        sd = _Access.dict_of(self.obj)
        if sd is None:
            return False
        funcs = []
        f = a.r(sd.get("Function"))
        if f is not None:
            if isinstance(f, list):
                funcs = [load_function(a, a.r(f[i]), set()) for i in range(min(len(f), 4))]
            else:
                funcs = [load_function(a, f, set())]
        cs_obj = a.r(sd.get("ColorSpace"))
        if cs_obj is None:
            return False
        try:
            cs = get_colorspace(a, cs_obj)
        except Unsupported:
            # A colour space the renderer can't draw yet (CalRGB, Lab, ICCBased, Indexed): taken
            # as loaded, so the page object is there and the render refuses in load(). Indexed
            # fails Validate, so its first `sh` draws nothing, as in PDFium.
            stype = a.integer_for(sd, "ShadingType")
            if not 1 <= stype <= 7:
                return False
            self._typed = True
            fam = cs_obj[0] if isinstance(cs_obj, list) and cs_obj else None
            name = _Access.string(a.r(fam)) if fam is not None else ""
            return not (name == "I" or name.startswith("Inde"))
        if cs is None or cs.family == "Pattern":
            return False
        stype = a.integer_for(sd, "ShadingType")
        if not 1 <= stype <= 7:
            return False
        self._typed = True
        self.shading, self.dict, self.cs, self.funcs, self.type = self.obj, sd, cs, funcs, stype
        return self._validate()

    def _validate(self) -> bool:
        if self.type >= 4 and not isinstance(self.shading, Stream):
            return False
        if self.cs.family == "Indexed":
            return False
        n = self.cs.n

        def ok(count, inputs, outputs):
            return len(self.funcs) == count and all(
                f is not None and f.inputs == inputs and f.outputs == outputs for f in self.funcs)
        if self.type == 1:
            return ok(1, 2, n) or ok(n, 2, 1)
        if self.type in (2, 3):
            return ok(1, 1, n) or ok(n, 1, 1)
        return not self.funcs or ok(1, 1, n) or ok(n, 1, 1)


def _cache(parser) -> dict:
    c = getattr(parser, "shading_cache", None)
    if c is None:
        c = parser.shading_cache = {}
    return c


def find_shading(parser, obj, parent_matrix) -> Record:
    """FindShading for `sh`: the document's cache keeps one pattern per object."""
    c = _cache(parser)
    rec = c.get(id(obj))
    if rec is None:
        rec = c[id(obj)] = Record(parser.doc, obj, "shading", parent_matrix)
    if rec.kind != "shading":
        return Record(parser.doc, obj, "?", parent_matrix)
    return rec


def find_pattern(parser, obj, parent_matrix):
    """FindPattern: None when PDFium finds nothing (its colour state stays as it was)."""
    if not isinstance(obj, (dict, Stream)):
        return None
    a = _Access(parser.doc)
    kind = {1: "tiling", 2: "pattern"}.get(a.integer_for(_Access.dict_of(obj), "PatternType"))
    if kind is None:
        return None
    c = _cache(parser)
    rec = c.get(id(obj))
    if rec is None:
        rec = c[id(obj)] = Record(parser.doc, obj, kind, parent_matrix)
    if rec.kind != kind or rec.parent_matrix != parent_matrix:
        # a pattern made for another form (or as a shading) may still be alive in PDFium's cache
        return Record(parser.doc, obj, "?", parent_matrix)
    return rec


# ---------------------------------------------------------------------- drawing


def _steps(rec: Record, t_min: float, t_max: float, alpha: int):
    """GetShadingSteps: 256 ARGB values, or None."""
    cs, funcs = rec.cs, rec.funcs
    total = sum(f.outputs for f in funcs if f is not None)
    if total > U32:
        total = 0
    count = max(total, cs.n) if total else 0
    if count == 0:
        return None
    results = [0.0] * count
    diff = F(t_max - t_min)
    out = np.zeros(256, np.uint32)
    for i in range(256):
        x = F(F(F(diff * float(i)) / 256.0) + t_min)
        off = 0
        for f in funcs:
            if f is None:
                continue
            n = f.call([x], results, off)
            if n is not None:
                off += n
        r, g, b = cs.rgb_or_zeros(results)
        out[i] = argb(alpha, roundf(F(r * 255.0)), roundf(F(g * 255.0)), roundf(F(b * 255.0)))
    return out


def _grid(final, w: int, h: int):
    """inverse(final).Transform(column, row) for every pixel, in float."""
    a, b, c, d, e, f = (np.float32(v) for v in R.inverse(final))
    cols = np.arange(w, dtype=np.float32)[None, :]
    rows = np.arange(h, dtype=np.float32)[:, None]
    return (a * cols + c * rows) + e, (b * cols + d * rows) + f


def _index(s):
    """static_cast<int32_t>(s * 255) on an array."""
    t = s * np.float32(255)
    bad = ~np.isfinite(t) | (t >= np.float32(2147483648.0)) | (t < np.float32(-2147483648.0))
    return np.where(bad, INT_MIN, np.trunc(np.where(bad, 0, t)).astype(np.int64))


def _paint(bitmap, idx, steps, start_ext: bool, end_ext: bool, live=None):
    ok = np.ones(idx.shape, bool) if live is None else live
    lo, hi = idx < 0, idx >= 256
    ok = ok & (~lo | start_ext) & (~hi | end_ext)
    k = np.clip(idx, 0, 255)
    bitmap[ok] = steps[k[ok]]


def _coords(rec: Record, n: int):
    a = rec.a
    coords = a.array_for(rec.dict, "Coords")
    if coords is None:
        return None
    vals = [a.float_at(coords, i) for i in range(n)]
    t_min, t_max = 0.0, 1.0
    dom = a.array_for(rec.dict, "Domain")
    if dom is not None:
        t_min, t_max = a.float_at(dom, 0), a.float_at(dom, 1)
    ext = a.array_for(rec.dict, "Extend")
    start_ext = ext is not None and a.boolean_at(ext, 0, False)
    end_ext = ext is not None and a.boolean_at(ext, 1, False)
    return vals, t_min, t_max, start_ext, end_ext


def _axial(bitmap, final, rec: Record, alpha: int) -> None:
    got = _coords(rec, 4)
    if got is None:
        return
    (sx, sy, ex, ey), t_min, t_max, s_ext, e_ext = got
    xs, ys = F(ex - sx), F(ey - sy)
    als = F(F(xs * xs) + F(ys * ys))
    steps = _steps(rec, t_min, t_max, alpha)
    if steps is None:
        return
    h, w = bitmap.shape
    with np.errstate(all="ignore"):
        px, py = _grid(final, w, h)
        f32 = np.float32
        scale = ((px - f32(sx)) * f32(xs) + (py - f32(sy)) * f32(ys)) / f32(als)
        _paint(bitmap, _index(scale), steps, s_ext, e_ext)


def _radial(bitmap, final, rec: Record, alpha: int) -> None:
    got = _coords(rec, 6)
    if got is None:
        return
    (sx, sy, sr, ex, ey, er), t_min, t_max, s_ext, e_ext = got
    steps = _steps(rec, t_min, t_max, alpha)
    if steps is None:
        return
    dx, dy, dr = F(ex - sx), F(ey - sy), F(er - sr)
    a = F(F(F(dx * dx) + F(dy * dy)) - F(dr * dr))
    a_zero = abs(a) < 0.0001
    decreasing = dr < 0 and float(i32(_hypotf(dx, dy))) < -dr
    h, w = bitmap.shape
    f32 = np.float32
    with np.errstate(all="ignore"):
        px, py = _grid(final, w, h)
        pdx, pdy = px - f32(sx), py - f32(sy)
        b = f32(-2) * ((pdx * f32(dx) + pdy * f32(dy)) + f32(F(sr * dr)))
        c = (pdx * pdx + pdy * pdy) - f32(F(sr * sr))
        b_zero = np.abs(b) < f32(0.0001)
        fa = f32(a)
        s = np.sqrt(-c / fa)
        live = np.ones(b.shape, bool)
        if a_zero:
            s = np.where(b_zero, s, -c / b)
        else:
            d = b * b - f32(4) * (fa * c)
            root = np.sqrt(d)
            s1 = (-b - root) / (f32(2) * fa)
            s2 = (-b + root) / (f32(2) * fa)
            if a <= 0:
                s1, s2 = s2, s1
            if decreasing:
                q = np.where((s1 >= 0) | s_ext, s1, s2)
            else:
                q = np.where((s2 <= f32(1)) | e_ext, s2, s1)
            quad = ~b_zero
            live = ~(quad & (d < 0)) & ~(quad & ((f32(sr) + q * f32(dr)) < 0))
            s = np.where(b_zero, s, q)
        _paint(bitmap, _index(s), steps, s_ext, e_ext, live)


def draw(dev, rec: Record, matrix, clip_rect, alpha: int) -> None:
    """CPDF_RenderShading::Draw on the AGG device, through a CPDF_DeviceBuffer."""
    cs, d, a = rec.cs, rec.dict, rec.a
    background = 0
    if rec.kind == "pattern" and "Background" in d:
        back = a.array_for(d, "Background")
        if back is not None and len(back) >= cs.n:
            r, g, b = cs.rgb_or_zeros([a.float_at(back, i) for i in range(cs.n)])
            background = argb(255, i32(F(r * 255.0)), i32(F(g * 255.0)), i32(F(b * 255.0)))
    rect = clip_rect
    if "BBox" in d:
        rect = fx_intersect(rect, outer(R.transform_rect(matrix, a.rect_for(d, "BBox"))))
    l, t, r, b = rect
    buf_m = (1.0, 0.0, 0.0, 1.0, F(float(-l)), F(float(-t)))
    br = outer(R.transform_rect(buf_m, (float(l), float(t), float(r), float(b))))
    w, h = br[2] - br[0], br[3] - br[1]
    if w <= 0 or h <= 0:
        return
    bitmap = np.zeros((h, w), np.uint32)
    if background:
        bitmap[...] = background
    final = R.concat(matrix, buf_m)
    if rec.type == 2:
        _axial(bitmap, final, rec, alpha)
    elif rec.type == 3:
        _radial(bitmap, final, rec, alpha)
    set_dibits(dev, bitmap, l, t)


def set_dibits(dev, bitmap: np.ndarray, left: int, top: int) -> None:
    """SetDIBitsWithBlend (Normal) -> CFX_AggDeviceDriver::SetDIBits -> CompositeBitmap of a
    BGRA bitmap under the clip region."""
    h, w = bitmap.shape
    cb = dev.clip_box()
    dest = fx_intersect((left, top, left + w, top + h), cb)
    if R.rect_empty(dest):
        return
    wh, ww = dev.bgra.shape[:2]
    dest = R.rect_intersect(dest, (dev.ox, dev.oy, dev.ox + ww, dev.oy + wh))
    if R.rect_empty(dest):
        return
    l, t, r, b = dest
    src = bitmap[t - top:b - top, l - left:r - left]
    s = np.stack([src & 0xFF, (src >> 8) & 0xFF, (src >> 16) & 0xFF, src >> 24], -1).astype(np.int32)
    dst = dev.bgra[t - dev.oy:b - dev.oy, l - dev.ox:r - dev.ox]
    d = dst.astype(np.int32)
    mask = None
    if dev.clip is not None and dev.clip.mask is not None:
        mask = dev.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
    sa = s[..., 3] if mask is None else s[..., 3] * mask // 255
    merge = (d[..., :3] * (255 - sa[..., None]) + s[..., :3] * sa[..., None]) // 255
    if not dev.alpha:
        out = np.where((sa == 255)[..., None], s[..., :3], np.where((sa > 0)[..., None], merge, d[..., :3]))
        dst[..., :3] = out.astype(np.uint8)
        return
    da = d[..., 3]
    fresh = da == 0
    mix = ~fresh & (sa > 0)
    union = (da + sa - da * sa // 255) & 0xFF
    ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)
    mixed = (d[..., :3] * (255 - ratio[..., None]) + s[..., :3] * ratio[..., None]) // 255
    out = np.where(mix[..., None], mixed, d[..., :3])
    out = np.where(fresh[..., None], s[..., :3], out)
    dst[..., :3] = out.astype(np.uint8)
    dst[..., 3] = np.where(fresh, sa, np.where(mix, union, da)).astype(np.uint8)


# ---------------------------------------------------------------------- render status


def _point_box(points) -> tuple:
    """CFX_Path::GetBoundingBox (CFX_ClipPath's paths too)."""
    if not points:
        return 0.0, 0.0, 0.0, 0.0
    l = r = points[0][0]
    b = t = points[0][1]
    for p in points[1:]:
        x, y = p[0], p[1]
        if x < l:
            l = x
        if y < b:
            b = y
        if r < x:
            r = x
        if t < y:
            t = y
    return l, b, r, t


def shading_rect(obj) -> tuple:
    """A shading object's GetRect: the clip path's box (CPDF_ClipPath::GetClipBox), in float."""
    if not obj.clip_paths:
        return tuple(F(v) for v in obj.rect)
    rect = _point_box(obj.clip_paths[0][0])
    for points, _ in obj.clip_paths[1:]:
        rect = float_intersect(rect, _point_box(points))
    return rect


def path_rect(obj) -> tuple:
    """CPDF_PathObject::CalcBoundingBox, in float."""
    from .render import stroke_bbox
    width = F(obj.line_width)
    if obj.stroked and width != 0:
        rect = stroke_bbox(obj.points, width)
    else:
        rect = _point_box(obj.points)
    with np.errstate(all="ignore"):
        rect = R.transform_rect(obj.matrix, rect)
        if width == 0 and obj.stroked:
            rect = (F(rect[0] - 0.5), F(rect[1] - 0.5), F(rect[2] + 0.5), F(rect[3] + 0.5))
    return rect


def _alpha(value: float) -> int:
    return roundf(F(255.0 * F(value)))


def process_shading(status, obj, matrix) -> None:
    """CPDF_RenderStatus::ProcessShading."""
    rec = obj.shading_record
    if rec is None or not rec.load():
        return
    dev = status.dev
    with np.errstate(all="ignore"):
        rect = fx_intersect(outer(R.transform_rect(matrix, shading_rect(obj))), dev.clip_box())
        if R.rect_empty(rect):
            return
        m = R.concat(obj.shading_matrix, matrix)
        draw(dev, rec, m, rect, _alpha(obj.fill_alpha))


def path_pattern(status, obj, matrix) -> tuple:
    """ProcessPathPattern: draws the pattern fill and stroke, returns what is left to paint."""
    fill_type, stroke = obj.fill_type, obj.stroked
    if fill_type != FILL_NONE and getattr(obj, "fill_pattern", None) is not None:
        _draw_pattern(status, obj, matrix, obj.fill_pattern, False)
        fill_type = FILL_NONE
    if stroke and getattr(obj, "stroke_pattern", None) is not None:
        _draw_pattern(status, obj, matrix, obj.stroke_pattern, True)
        stroke = False
    return fill_type, stroke


def _draw_pattern(status, obj, matrix, rec, stroke: bool) -> None:
    """DrawPathWithPattern -> DrawShadingPattern."""
    if rec is False or not isinstance(rec, Record) or not rec.load():
        return
    dev = status.dev
    dev.save()
    try:
        with np.errstate(all="ignore"):
            pm = R.concat(obj.matrix, matrix)
            if stroke:
                if dev.clip is None:
                    from .render import Clip
                    dev.clip = Clip((0, 0, dev.w, dev.h))
                if F(R.x_unit(pm) + R.y_unit(pm)) == 0.0:
                    # RasterizeStroke's unit would be 1 / 0: a stroke of infinite width through a
                    # matrix that maps everything to one point, NaN vertices in AGG
                    from ..api import PdfError
                    raise PdfError("the pure reader cannot render a pattern stroked through a matrix of zeros yet")
                rz = R.Rasterizer(dev.w, dev.h)
                rz.add_path(R.stroke_vertices(R.build_path(obj.points, None), pm, F(obj.line_width), obj.line_cap,
                                              obj.line_join, F(obj.miter), tuple(F(v) for v in obj.dash),
                                              F(obj.dash_phase), 1.0))
                dev._set_clip_mask(rz, False)
            else:
                dev.set_clip_fill(obj.points, pm, obj.fill_type == FILL_EVENODD)
            rect = fx_intersect(outer(R.transform_rect(matrix, path_rect(obj))), dev.clip_box())
            if R.rect_empty(rect):
                return
            m = R.concat(rec.pattern_to_form, matrix)
            draw(dev, rec, m, rect, _alpha(obj.stroke_alpha if stroke else obj.fill_alpha))
    finally:
        dev.restore(False)


def refusal(obj) -> str | None:
    """What of a shading object or a pattern-painted path is not drawn exactly, if anything."""
    recs = []
    if getattr(obj, "shading_record", None) is not None:
        recs.append(obj.shading_record)
    if getattr(obj, "fill_type", FILL_NONE) != FILL_NONE and getattr(obj, "fill_pattern", None) is not None:
        recs.append(obj.fill_pattern)
    if getattr(obj, "stroked", False) and getattr(obj, "stroke_pattern", None) is not None:
        recs.append(obj.stroke_pattern)
    for rec in recs:
        if rec is False:
            continue
        if not isinstance(rec, Record):
            return "a pattern state PDFium's parser keeps differently"
        rec.load()
        if rec.reason:
            return rec.reason
    return None
