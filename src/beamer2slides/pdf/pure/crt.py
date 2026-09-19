"""The C runtime PDFium's float functions come from, per platform: ucrtbase on Windows, glibc's libm
on Linux, libSystem on macOS. powf, sinf and friends are not correctly rounded and each library
rounds its own way, so matching PDFium to the bit means calling the one it links."""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import sys

_lib = None


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


def float_fn(name: str, n: int):
    """A float(float, ...) function of the platform's C runtime. `hypotf` is `_hypotf` in ucrtbase
    (its `hypotf` is an inline wrapper in the headers)."""
    crt = lib()
    fn = getattr(crt, name, None) or getattr(crt, name.lstrip("_"))
    fn.restype = ctypes.c_float
    fn.argtypes = [ctypes.c_float] * n
    return fn


_CMP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))


def qsort(a: list, cmp) -> None:
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
    callback = _CMP(lambda p, q: max(-1, min(1, cmp(items[p[0]], items[q[0]]))))
    fn(idx, n, ctypes.sizeof(ctypes.c_int), callback)
    a[:] = [items[i] for i in idx]


_cg = None


def quartz_font(data: bytes | None) -> bool:
    """Whether CQuartz2D::CreateFont makes a CGFont of these bytes (macOS only; False elsewhere and
    for no bytes): CPDF_Type1Font::LoadGlyphMap takes its Apple-only `bCoreText` path when it does."""
    global _cg
    if sys.platform != "darwin" or not data:
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
    buf = ctypes.create_string_buffer(bytes(data), len(data))
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


def i32_array(t):
    """static_cast<int32_t> over a float numpy array (int64 result)."""
    import numpy as np
    with np.errstate(invalid="ignore"):
        if ARM:
            t = np.nan_to_num(np.asarray(t, np.float64), nan=0.0, posinf=INT_MAX, neginf=INT_MIN)
            return np.clip(np.trunc(t), INT_MIN, INT_MAX).astype(np.int64)
        bad = ~np.isfinite(t) | (t >= np.float32(2147483648.0)) | (t < np.float32(-2147483648.0))
        return np.where(bad, INT_MIN, np.trunc(np.where(bad, 0, t))).astype(np.int64)
