"""The C runtime PDFium's float functions come from, per platform: ucrtbase on Windows, glibc's libm
on Linux, libSystem on macOS. powf, sinf and friends are not correctly rounded and each library
rounds its own way, so matching PDFium to the bit means calling the one it links."""

from __future__ import annotations

import ctypes
import ctypes.util
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
