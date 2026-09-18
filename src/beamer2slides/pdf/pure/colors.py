"""Colour spaces as PDFium reads them for vector colours (CPDF_ColorSpace::GetRGB and
CPDF_Color::GetColorRef): what `FPDFPageObj_GetFillColor` and `FPDFText_GetFillColor` report."""

from __future__ import annotations

import base64
import math

from .cmyk_table import TABLE
from .syntax import Name, Stream

_CMYK = base64.b64decode("".join(TABLE))


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def adobe_cmyk_to_srgb(c: int, m: int, y: int, k: int) -> tuple[int, int, int]:
    """fxge::AdobeCmykToStandardRgb: 0-255 CMYK through PDFium's 9^4 table."""
    def at(ci, mi, yi, ki):
        i = 3 * (729 * ci + 81 * mi + 9 * yi + ki)
        return _CMYK[i], _CMYK[i + 1], _CMYK[i + 2]

    def div32(v):  # C integer division truncates towards zero
        return -((-v) // 32) if v < 0 else v // 32

    fix = [c << 8, m << 8, y << 8, k << 8]
    idx = [(f + 4096) >> 13 for f in fix]
    start = at(*idx)
    rgb = [start[0] << 8, start[1] << 8, start[2] << 8]
    for axis in range(4):
        other = fix[axis] >> 13
        if other == idx[axis]:
            other = other - 1 if other == 8 else other + 1
        moved = list(idx)
        moved[axis] = other
        neighbour = at(*moved)
        rate = (fix[axis] - (idx[axis] << 13)) * (idx[axis] - other)
        for ch in range(3):
            rgb[ch] += div32((start[ch] - neighbour[ch]) * rate)
    return tuple(max(v, 0) >> 8 for v in rgb)


def _f32(v: float) -> float:
    import struct
    return struct.unpack("f", struct.pack("f", v))[0]


def _round255(v: float) -> int:
    """static_cast<int>(v * 255.f + 0.49999997f) in float32."""
    return int(_f32(_f32(_f32(v) * 255.0) + 0.49999997))


class ColorSpace:
    """One colour space: `n` components, `rgb(values)` -> 0-1 floats or None."""

    def __init__(self, family: str, n: int, base: "ColorSpace | None" = None, extra=None):
        self.family, self.n, self.base, self.extra = family, n, base, extra

    @property
    def is_pattern(self) -> bool:
        return self.family == "Pattern"

    def initial(self) -> list[float]:
        if self.family == "DeviceCMYK":
            return [0.0, 0.0, 0.0, 1.0]
        if self.family in ("Separation", "DeviceN"):
            return [1.0] * self.n
        return [0.0] * self.n

    def rgb(self, v: list[float]):
        f = self.family
        if f in ("DeviceGray", "CalGray"):
            g = _clamp(v[0])
            return g, g, g
        if f in ("DeviceRGB", "CalRGB"):
            return _clamp(v[0]), _clamp(v[1]), _clamp(v[2])
        if f == "DeviceCMYK":
            r, g, b = adobe_cmyk_to_srgb(*(_round255(_clamp(x)) for x in v[:4]))
            return r / 255.0, g / 255.0, b / 255.0
        if f == "Lab":
            return _lab_rgb(*v[:3])
        if f == "ICCBased":  # the profile is not interpreted: its alternate (or Device* by N) stands in
            return self.base.rgb(v) if self.base else None
        if f == "Indexed":
            base, hival, lookup = self.base, self.extra[0], self.extra[1]
            i = int(v[0])
            if base is None or not 0 <= i <= hival:
                return None
            comps = lookup[i * base.n:(i + 1) * base.n]
            if len(comps) < base.n:
                return None
            return base.rgb([c / 255.0 for c in comps])
        if f in ("Separation", "DeviceN"):
            fn = self.extra
            if self.base is None or fn is None:
                return None
            out = fn(v[:self.n])
            return self.base.rgb(out) if out is not None and len(out) >= self.base.n else None
        return None

    def colorref(self, v: list[float]) -> int | None:
        """CPDF_Color::GetColorRef as 0xRRGGBB (roundf of each channel x 255)."""
        rgb = self.rgb(v) if len(v) >= self.n else None
        if rgb is None:
            return None
        r, g, b = (int(math.floor(_f32(_f32(_clamp(x)) * 255.0) + 0.5)) for x in rgb)
        return (r << 16) | (g << 8) | b


DEVICE = {name: ColorSpace(name, n) for name, n in (("DeviceGray", 1), ("DeviceRGB", 3), ("DeviceCMYK", 4))}
PATTERN = ColorSpace("Pattern", 0)
_ABBREV = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}


def _lab_rgb(l, a, b):
    """CPDF_LabCS::GetRGB (D65 white point assumed, sRGB matrix, no gamma)."""
    m = (l + 16) / 116
    x_, y_, z_ = m + a / 500, m, m - b / 200

    def f(t):
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)

    x, y, z = 0.9505 * f(x_), f(y_), 1.089 * f(z_)
    r = 3.240479 * x - 1.53715 * y - 0.498535 * z
    g = -0.969256 * x + 1.875992 * y + 0.041556 * z
    bb = 0.055648 * x - 0.204043 * y + 1.057311 * z
    return _clamp(r), _clamp(g), _clamp(bb)


def _function(doc, obj):
    """A PDF function (types 2 and 4 partly, 0 not) as a callable, or None."""
    obj = doc.resolve(obj)
    d = obj.dict if isinstance(obj, Stream) else obj
    if isinstance(obj, list):
        fns = [_function(doc, f) for f in obj]
        if any(f is None for f in fns):
            return None
        return lambda v: [f(v)[0] for f in fns]
    if not isinstance(d, dict):
        return None
    ftype = doc.resolve(d.get("FunctionType"))
    if ftype == 2:
        c0 = [float(doc.resolve(x)) for x in (doc.resolve(d.get("C0")) or [0.0])]
        c1 = [float(doc.resolve(x)) for x in (doc.resolve(d.get("C1")) or [1.0])]
        n = float(doc.resolve(d.get("N")) or 1.0)
        return lambda v: [a + (v[0] ** n if v[0] > 0 else 0.0) * (b - a) for a, b in zip(c0, c1)]
    return None


def load_colorspace(doc, obj, resources: dict | None, depth: int = 0) -> ColorSpace | None:
    """CPDF_DocPageData::GetColorSpace for a colour space object (a name or an array)."""
    r = doc.resolve
    obj = r(obj)
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, Name) or isinstance(obj, str):
        name = _ABBREV.get(str(obj), str(obj))
        if name in DEVICE:
            default = _defaults(doc, resources, name, depth)
            return default or DEVICE[name]
        if name == "Pattern":
            return PATTERN
        spaces = r(resources.get("ColorSpace")) if isinstance(resources, dict) else None
        if isinstance(spaces, dict) and name in spaces:
            return load_colorspace(doc, spaces[name], None, depth + 1)
        return None
    if not isinstance(obj, list) or not obj:
        return None
    family = str(r(obj[0]))
    family = _ABBREV.get(family, family)
    if family in DEVICE and len(obj) == 1:
        return DEVICE[family]
    if family == "CalGray":
        return ColorSpace("CalGray", 1)
    if family == "CalRGB":
        return ColorSpace("CalRGB", 3)
    if family == "Lab":
        return ColorSpace("Lab", 3)
    if family == "ICCBased":
        stream = r(obj[1]) if len(obj) > 1 else None
        if not isinstance(stream, Stream):
            return None
        n = r(stream.get("N"))
        alt = load_colorspace(doc, stream.get("Alternate"), None, depth + 1)
        if alt is None or (isinstance(n, int) and alt.n != n):
            alt = {1: DEVICE["DeviceGray"], 3: DEVICE["DeviceRGB"], 4: DEVICE["DeviceCMYK"]}.get(n)
        if alt is None:
            return None
        return ColorSpace("ICCBased", alt.n, alt)
    if family in ("Indexed", "I"):
        if len(obj) < 4:
            return None
        base = load_colorspace(doc, obj[1], None, depth + 1)
        hival = r(obj[2])
        lookup = r(obj[3])
        if isinstance(lookup, Stream):
            lookup = doc.stream_data(lookup)
        if base is None or not isinstance(hival, int) or not isinstance(lookup, (bytes, bytearray)):
            return None
        return ColorSpace("Indexed", 1, base, (hival, bytes(lookup)))
    if family == "Separation":
        if len(obj) < 4:
            return None
        base = load_colorspace(doc, obj[2], None, depth + 1)
        return ColorSpace("Separation", 1, base, _function(doc, obj[3]))
    if family == "DeviceN":
        if len(obj) < 4 or not isinstance(r(obj[1]), list):
            return None
        base = load_colorspace(doc, obj[2], None, depth + 1)
        return ColorSpace("DeviceN", len(r(obj[1])), base, _function(doc, obj[3]))
    if family == "Pattern":
        base = load_colorspace(doc, obj[1], None, depth + 1) if len(obj) > 1 else None
        return ColorSpace("Pattern", 0, base)
    return None


def _defaults(doc, resources, name: str, depth: int) -> ColorSpace | None:
    """DefaultGray / DefaultRGB / DefaultCMYK from the resources replace a device space."""
    r = doc.resolve
    spaces = r(resources.get("ColorSpace")) if isinstance(resources, dict) else None
    key = {"DeviceGray": "DefaultGray", "DeviceRGB": "DefaultRGB", "DeviceCMYK": "DefaultCMYK"}[name]
    if not isinstance(spaces, dict) or key not in spaces:
        return None
    cs = load_colorspace(doc, spaces[key], None, depth + 1)
    return cs if cs is not None and cs.n == DEVICE[name].n and not cs.is_pattern else None
