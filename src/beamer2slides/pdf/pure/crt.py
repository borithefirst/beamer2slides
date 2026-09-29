"""The C runtime PDFium's float functions come from, per platform: ucrtbase on Windows, glibc's libm
on Linux, libSystem on macOS. powf, sinf and friends are not correctly rounded and each library
rounds its own way, so matching PDFium to the bit means calling the one it links. Also C's own
integer arithmetic where Python's differs (casts, division, %, FXSYS_roundf), one copy for the port."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import platform
import sys
from typing import TYPE_CHECKING, Callable, Sequence, TypeVar

if TYPE_CHECKING:
    from ...arrays import Floats, Floats32, Ints

T = TypeVar("T")

_lib: ctypes.CDLL | None = None


def lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        if sys.platform == "win32":
            _lib = ctypes.CDLL("ucrtbase")
        elif sys.platform == "darwin":
            _lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        else:
            _lib = ctypes.CDLL(ctypes.util.find_library("m") or "libm.so.6")
    return _lib


def _c_function(name: str, n: int) -> ctypes._NamedFuncPointer:
    """The C runtime's function `name`, taking `n` floats and giving a float. `hypotf` is `_hypotf`
    in ucrtbase (its `hypotf` is an inline wrapper in the headers)."""
    crt = lib()
    try:
        fn = crt[name]
    except AttributeError:
        fn = crt[name.lstrip("_")]
    fn.restype = ctypes.c_float
    fn.argtypes = [ctypes.c_float] * n
    return fn


def float_fn(name: str, n: int) -> Callable[..., float]:
    """A float(float, ...) function of `n` arguments of the platform's C runtime (`float_fn1` and
    `float_fn2` say their arity to the checker)."""
    return _c_function(name, n)


def float_fn1(name: str) -> Callable[[float], float]:
    """A float(float) function of the platform's C runtime (sinf, logf...)."""
    return _c_function(name, 1)


def float_fn2(name: str) -> Callable[[float, float], float]:
    """A float(float, float) function of the platform's C runtime (powf, atan2f, _hypotf)."""
    return _c_function(name, 2)


_CMP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))


def qsort(a: list[T], cmp: Callable[[T, T], int]) -> None:
    """The C runtime's own qsort, in place (`cmp(x, y)` -> <0, 0, >0): which of two equal items comes
    first is the library's (glibc's merge sort keeps their order, the BSD and UCRT quicksorts don't).
    The C side sorts the indices of `a`, so it sees the same comparisons and makes the same moves."""
    n = len(a)
    if n < 2:
        return
    fn = lib().qsort
    fn.restype = None
    fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, _CMP]
    idx = (ctypes.c_int * n)(*range(n))
    items = list(a)

    def compare(p: ctypes._Pointer[ctypes.c_int], q: ctypes._Pointer[ctypes.c_int]) -> int:
        return max(-1, min(1, cmp(items[p.contents.value], items[q.contents.value])))

    callback = _CMP(compare)
    fn(idx, n, ctypes.sizeof(ctypes.c_int), callback)
    a[:] = [items[i] for i in idx]


_cg: ctypes.CDLL | None = None


def quartz_font(data: bytes | None) -> bool:
    """Whether CQuartz2D::CreateFont makes a CGFont of these bytes (macOS only; False elsewhere and
    for no bytes): CPDF_Type1Font::LoadGlyphMap takes its Apple-only `bCoreText` path when it does."""
    global _cg
    if not data:
        return False
    if sys.platform != "darwin":
        return False
    if _cg is None:
        _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        _cg.CGDataProviderCreateWithData.restype = ctypes.c_void_p
        _cg.CGDataProviderCreateWithData.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                                     ctypes.c_void_p]
        _cg.CGFontCreateWithDataProvider.restype = ctypes.c_void_p
        _cg.CGFontCreateWithDataProvider.argtypes = [ctypes.c_void_p]
        _cg.CGDataProviderRelease.argtypes = [ctypes.c_void_p]
        _cg.CGFontRelease.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    provider = _cg.CGDataProviderCreateWithData(None, buf, len(data), None)
    if not provider:
        return False
    font = _cg.CGFontCreateWithDataProvider(provider)
    _cg.CGDataProviderRelease(provider)
    if font:
        _cg.CGFontRelease(font)
    return bool(font)


# static_cast from float to an integer is undefined out of range, and the CPU decides what comes out:
# x86's cvttss2si gives INT_MIN (the 64-bit form's low half, for uint32), arm64's fcvtzs/fcvtzu
# saturate and turn NaN into 0 (pypdfium2's macOS wheel is arm64)
ARM = platform.machine().lower() in ("arm64", "aarch64")
INT_MIN, INT_MAX, U32 = -(1 << 31), (1 << 31) - 1, 0xFFFFFFFF


def i32(v: float) -> int:
    """static_cast<int32_t>(float)."""
    if v != v:
        return 0 if ARM else INT_MIN
    if v >= 2147483648.0:
        return INT_MAX if ARM else INT_MIN
    if v < -2147483648.0:
        return INT_MIN
    return int(v)


def u32(v: float) -> int:
    """static_cast<uint32_t>(float)."""
    if ARM:
        return 0 if v != v or v <= 0.0 else U32 if v >= 4294967296.0 else int(v)
    if v != v or v >= 9223372036854775808.0 or v < -9223372036854775808.0:
        return 0
    return int(v) & U32


def cdiv(a: int, b: int) -> int:
    """C's integer division, truncating towards zero (Python's // floors)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def cmod(a: int, b: int) -> int:
    """C's %: the remainder takes the dividend's sign."""
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def roundf(v: float) -> int:
    """FXSYS_roundf: half away from zero, NaN is 0, and the int range saturates. The upper bound is
    static_cast<float>(INT_MAX), which is 2^31, so a float32 from 2^31 - 1 up is INT_MAX."""
    if v != v:
        return 0
    if v < -2147483648.0:
        return INT_MIN
    if v >= 2147483647.0:
        return INT_MAX
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def rect_valid(rect: Sequence[int]) -> bool:
    """FX_RECT::Valid: the width and height of (left, top, right, bottom) fit an int32."""
    return INT_MIN <= rect[2] - rect[0] <= INT_MAX and INT_MIN <= rect[3] - rect[1] <= INT_MAX


def i32_array(t: Floats | Floats32) -> Ints:
    """static_cast<int32_t> over a float numpy array (int64 result)."""
    import numpy as np
    with np.errstate(invalid="ignore"):
        if ARM:
            t = np.nan_to_num(np.asarray(t, np.float64), nan=0.0, posinf=INT_MAX, neginf=INT_MIN)
            return np.clip(np.trunc(t), INT_MIN, INT_MAX).astype(np.int64)
        bad = ~np.isfinite(t) | (t >= np.float32(2147483648.0)) | (t < np.float32(-2147483648.0))
        return np.where(bad, INT_MIN, np.trunc(np.where(bad, 0, t))).astype(np.int64)
