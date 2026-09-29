"""Colour spaces as PDFium reads them for vector colours (CPDF_ColorSpace::GetRGB and
CPDF_Color::GetColorRef): what `FPDFPageObj_GetFillColor` and `FPDFText_GetFillColor` report."""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Sequence

from ...typing_compat import assert_never
from .cmyk_table import TABLE
from .syntax import F32X3, Name, PdfDict, PdfObject, Stream

if TYPE_CHECKING:
    from ...arrays import Bytes, Int32, Ints
    from .document import PdfFile

_CMYK = base64.b64decode("".join(TABLE))


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def adobe_cmyk_to_srgb(c: int, m: int, y: int, k: int) -> tuple[int, int, int]:
    """fxge::AdobeCmykToStandardRgb: 0-255 CMYK through PDFium's 9^4 table."""
    def at(ci: int, mi: int, yi: int, ki: int) -> tuple[int, int, int]:
        i = 3 * (729 * ci + 81 * mi + 9 * yi + ki)
        return _CMYK[i], _CMYK[i + 1], _CMYK[i + 2]

    def div32(v: int) -> int:  # C integer division truncates towards zero
        return -((-v) // 32) if v < 0 else v // 32

    fix = [c << 8, m << 8, y << 8, k << 8]
    idx = [(f + 4096) >> 13 for f in fix]
    start = at(idx[0], idx[1], idx[2], idx[3])
    rgb = [start[0] << 8, start[1] << 8, start[2] << 8]
    for axis in range(4):
        other = fix[axis] >> 13
        if other == idx[axis]:
            other = other - 1 if other == 8 else other + 1
        moved = list(idx)
        moved[axis] = other
        neighbour = at(moved[0], moved[1], moved[2], moved[3])
        rate = (fix[axis] - (idx[axis] << 13)) * (idx[axis] - other)
        for ch in range(3):
            rgb[ch] += div32((start[ch] - neighbour[ch]) * rate)
    return max(rgb[0], 0) >> 8, max(rgb[1], 0) >> 8, max(rgb[2], 0) >> 8


_CMYK_ARRAY: Int32 | None = None


def adobe_cmyk_to_srgb_array(q: Ints | Bytes) -> Int32:
    """`adobe_cmyk_to_srgb` over an (n, 4) integer array of 0-255 CMYK: (n, 3) int32, the same
    integer arithmetic element by element (in int32, which holds it: |rate| <= 4096, a table
    difference <= 255)."""
    import numpy as np
    global _CMYK_ARRAY
    if _CMYK_ARRAY is None:
        _CMYK_ARRAY = np.frombuffer(_CMYK, np.uint8).astype(np.int32).reshape(-1, 3)
    table = _CMYK_ARRAY
    fix = np.asarray(q).reshape(-1, 4).astype(np.int32) << 8
    idx = (fix + 4096) >> 13
    base = 729 * idx[:, 0] + 81 * idx[:, 1] + 9 * idx[:, 2] + idx[:, 3]
    start = table[base]
    rgb = start << 8
    for axis, stride in enumerate((729, 81, 9, 1)):
        f, i = fix[:, axis], idx[:, axis]
        # the neighbour along this axis: `f >> 13` is i or i - 1; when it is i, one step away
        other = f >> 13
        other = np.where(other == i, np.where(other == 8, other - 1, other + 1), other)
        step = other - i
        neighbour = table[base + stride * step]
        rate = (f - (i << 13)) * -step
        v = (start - neighbour) * rate[:, None]
        rgb += (v + ((v >> 31) & 31)) >> 5          # C division by 32, truncating towards zero
    return np.maximum(rgb, 0) >> 8


def _f32(v: float) -> float:
    import struct
    return struct.unpack("f", struct.pack("f", v))[0]


def _round255(v: float) -> int:
    """static_cast<int>(v * 255.f + 0.49999997f) in float32."""
    return int(_f32(_f32(_f32(v) * 255.0) + 0.49999997))


SRGB_PROFILE_TAG = b"sRGB IEC61966-2.1"


def icc_srgb(data: bytes, n: int) -> bool:
    """CPDF_IccProfile's `is_srgb_`: /N 3 and DetectSRGB (a profile of exactly 3144 bytes carrying
    this description at 400). Such a profile is never handed to lcms: CPDF_ICCBasedCS::GetRGB gives
    the first three components back unchanged (no clamp), TranslateImageLine only reverses the line
    and IsNormal() is true, so it is exactly a device space without the clamping."""
    return n == 3 and len(data) == 3144 and data[400:417] == SRGB_PROFILE_TAG


def icc_openable(data: bytes) -> bool:
    """Could lcms open this as a profile? Anything that might be one is refused where a profile has
    to be interpreted: only data that cannot be one falls back to the alternate as PDFium's does."""
    return len(data) >= 128 and data[36:40] == b"acsp"


Family = Literal["DeviceGray", "DeviceRGB", "DeviceCMYK", "CalGray", "CalRGB", "Lab", "ICCBased", "Indexed",
                 "Separation", "DeviceN", "Pattern"]
"""The colour space families PDFium loads (CPDF_ColorSpace::Family)."""

Tint = Callable[[Sequence[float]], list[float]]
"""A Separation or DeviceN tint transform: the components in, the base space's components out."""


@dataclass(frozen=True, kw_only=True)
class Palette:
    """An Indexed space's colours: indices 0 to `hival`, each the base's components as bytes."""
    hival: int
    lookup: bytes


@dataclass(frozen=True, kw_only=True)
class ColorSpace:
    """One colour space: `n` components, `rgb(values)` -> 0-1 floats or None. `base` is the space an
    ICCBased, Indexed, Separation, DeviceN or Pattern space reads through, `srgb` an ICC profile
    that is sRGB (the identity), `palette` an Indexed space's colours and `tint` a Separation or
    DeviceN space's transform (None: one this port does not evaluate)."""
    family: Family
    n: int
    base: ColorSpace | None
    srgb: bool
    palette: Palette | None
    tint: Tint | None

    @property
    def is_pattern(self) -> bool:
        return self.family == "Pattern"

    def initial(self) -> list[float]:
        if self.family == "DeviceCMYK":
            return [0.0, 0.0, 0.0, 1.0]
        if self.family in ("Separation", "DeviceN"):
            return [1.0] * self.n
        return [0.0] * self.n

    def rgb(self, v: Sequence[float]) -> tuple[float, float, float] | None:
        f = self.family
        match f:
            case "DeviceGray" | "CalGray":
                g = _clamp(v[0])
                return g, g, g
            case "DeviceRGB" | "CalRGB":
                return _clamp(v[0]), _clamp(v[1]), _clamp(v[2])
            case "DeviceCMYK":
                r, g, b = adobe_cmyk_to_srgb(_round255(_clamp(v[0])), _round255(_clamp(v[1])),
                                             _round255(_clamp(v[2])), _round255(_clamp(v[3])))
                return r / 255.0, g / 255.0, b / 255.0
            case "Lab":
                return _lab_rgb(v[0], v[1], v[2])
            case "ICCBased":  # an sRGB profile is the identity; any other is read through its alternate
                if self.srgb:
                    return v[0], v[1], v[2]
                return self.base.rgb(v) if self.base else None
            case "Indexed":
                base, palette = self.base, self.palette
                if palette is None:
                    return None
                i = int(v[0])
                if base is None or not 0 <= i <= palette.hival:
                    return None
                comps = palette.lookup[i * base.n:(i + 1) * base.n]
                if len(comps) < base.n:
                    return None
                return base.rgb([c / 255.0 for c in comps])
            case "Separation" | "DeviceN":
                fn = self.tint
                if self.base is None or fn is None:
                    return None
                out = fn(v[:self.n])
                return self.base.rgb(out) if len(out) >= self.base.n else None
            case "Pattern":
                return None
            case _:
                assert_never(f)

    def colorref(self, v: Sequence[float]) -> int | None:
        """CPDF_Color::GetColorRef as 0xRRGGBB (roundf of each channel x 255)."""
        rgb = self.rgb(v) if len(v) >= self.n else None
        if rgb is None:
            return None
        a, b, c = rgb   # clamped to 0-1 first, so float32 cannot overflow
        a, b, c = F32X3.unpack(F32X3.pack(min(1.0, max(0.0, a)), min(1.0, max(0.0, b)), min(1.0, max(0.0, c))))
        a, b, c = F32X3.unpack(F32X3.pack(a * 255.0, b * 255.0, c * 255.0))
        return (math.floor(a + 0.5) << 16) | (math.floor(b + 0.5) << 8) | math.floor(c + 0.5)


def _plain(family: Family, n: int) -> ColorSpace:
    """A space that reads through nothing (a device or CIE space, or a Pattern with no base)."""
    return ColorSpace(family=family, n=n, base=None, srgb=False, palette=None, tint=None)


def _through(family: Family, n: int, base: ColorSpace | None, tint: Tint | None) -> ColorSpace:
    """A space that reads its colours through `base` (ICCBased, Separation, DeviceN, Pattern)."""
    return ColorSpace(family=family, n=n, base=base, srgb=False, palette=None, tint=tint)


GRAY, RGB, CMYK = _plain("DeviceGray", 1), _plain("DeviceRGB", 3), _plain("DeviceCMYK", 4)
DEVICE: dict[str, ColorSpace] = {"DeviceGray": GRAY, "DeviceRGB": RGB, "DeviceCMYK": CMYK}
PATTERN = _plain("Pattern", 0)
_ABBREV = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}


def _device_for(n: PdfObject) -> ColorSpace | None:
    """The device space of `n` components (1, 3 or 4), as `{1: gray, 3: rgb, 4: cmyk}.get(n)`
    looks it up (a real equal to one of them counts)."""
    if isinstance(n, (int, float)) and n in (1, 3, 4):
        return {1: GRAY, 3: RGB, 4: CMYK}[int(n)]
    return None


def _lab_rgb(l: float, a: float, b: float) -> tuple[float, float, float]:
    """CPDF_LabCS::GetRGB (D65 white point assumed, sRGB matrix, no gamma)."""
    m = (l + 16) / 116
    x_, y_, z_ = m + a / 500, m, m - b / 200

    def f(t: float) -> float:
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)

    x, y, z = 0.9505 * f(x_), f(y_), 1.089 * f(z_)
    r = 3.240479 * x - 1.53715 * y - 0.498535 * z
    g = -0.969256 * x + 1.875992 * y + 0.041556 * z
    bb = 0.055648 * x - 0.204043 * y + 1.057311 * z
    return _clamp(r), _clamp(g), _clamp(bb)


def _to_float(x: PdfObject) -> float:
    """`float(x)` as the functions below have always taken it: a number, or a name or string that
    spells one (anything else, or one that spells none, raises)."""
    if isinstance(x, (int, float, str, bytes)):
        return float(x)
    raise TypeError(f"not a number: {type(x).__name__}")


def _function(doc: PdfFile, obj: PdfObject) -> Tint | None:
    """A PDF function (types 2 and 4 partly, 0 not) as a callable, or None."""
    obj = doc.resolve(obj)
    d = obj.dict if isinstance(obj, Stream) else obj
    if isinstance(obj, list):
        loaded = [_function(doc, f) for f in obj]
        fns = [fn for fn in loaded if fn is not None]
        if len(fns) != len(loaded):
            return None

        def each(v: Sequence[float]) -> list[float]:
            return [f(v)[0] for f in fns]
        return each
    if not isinstance(d, dict):
        return None
    ftype = doc.resolve(d.get("FunctionType"))
    if ftype == 2:
        c0 = [_to_float(doc.resolve(x)) for x in _floats_raw(doc.resolve(d.get("C0")), [0.0])]
        c1 = [_to_float(doc.resolve(x)) for x in _floats_raw(doc.resolve(d.get("C1")), [1.0])]
        n = _to_float(doc.resolve(d.get("N")) or 1.0)

        def exponential(v: Sequence[float]) -> list[float]:
            return [a + (v[0] ** n if v[0] > 0 else 0.0) * (b - a) for a, b in zip(c0, c1)]
        return exponential
    return None


def _floats_raw(value: PdfObject, missing: list[float]) -> Sequence[PdfObject]:
    """`value or missing` over a function's array entry: its elements when it is a non-empty array
    (still to be resolved), `missing` when it is falsy. Anything else raises: iterating it did too,
    except a name, string or dictionary, whose letters, bytes or keys it read as numbers (PDFium
    reads none of these: a /C0 or /C1 that is no array is its default)."""
    if not value:
        return missing
    if isinstance(value, list):
        return value
    raise TypeError(f"not an array: {type(value).__name__}")


def load_colorspace(doc: PdfFile, obj: PdfObject, resources: PdfDict | None, depth: int) -> ColorSpace | None:
    """CPDF_DocPageData::GetColorSpace for a colour space object (a name or an array); `depth`
    counts the names and arrays followed to get here (0 for a colour space the content names)."""
    r = doc.resolve
    obj = r(obj)
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, Name):
        name = _ABBREV.get(str(obj), str(obj))
        device = DEVICE.get(name)
        if device is not None:
            default = _defaults(doc, resources, name, depth)
            return default or device
        if name == "Pattern":
            return PATTERN
        spaces = r(resources.get("ColorSpace")) if resources is not None else None
        if isinstance(spaces, dict) and name in spaces:
            return load_colorspace(doc, spaces[name], None, depth + 1)
        return None
    if not isinstance(obj, list) or not obj:
        return None
    family = str(r(obj[0]))
    family = _ABBREV.get(family, family)
    device = DEVICE.get(family)
    if device is not None and len(obj) == 1:
        return device
    if family == "CalGray":
        return _plain("CalGray", 1)
    if family == "CalRGB":
        return _plain("CalRGB", 3)
    if family == "Lab":
        return _plain("Lab", 3)
    if family == "ICCBased":
        stream = r(obj[1]) if len(obj) > 1 else None
        if not isinstance(stream, Stream):
            return None
        n = r(stream.get("N"))
        if n == 3 and icc_srgb(doc.stream_data(stream), 3):
            return ColorSpace(family="ICCBased", n=3, base=RGB, srgb=True, palette=None, tint=None)
        alt = load_colorspace(doc, stream.get("Alternate"), None, depth + 1)
        if alt is None or (isinstance(n, int) and alt.n != n):
            alt = _device_for(n)
        if alt is None:
            return None
        return _through("ICCBased", alt.n, alt, None)
    if family in ("Indexed", "I"):
        if len(obj) < 4:
            return None
        base = load_colorspace(doc, obj[1], None, depth + 1)
        hival = r(obj[2])
        lookup = r(obj[3])
        table = doc.stream_data(lookup) if isinstance(lookup, Stream) else lookup
        if base is None or not isinstance(hival, int) or not isinstance(table, bytes):
            return None
        return ColorSpace(family="Indexed", n=1, base=base, srgb=False,
                          palette=Palette(hival=hival, lookup=bytes(table)), tint=None)
    if family == "Separation":
        if len(obj) < 4:
            return None
        base = load_colorspace(doc, obj[2], None, depth + 1)
        return _through("Separation", 1, base, _function(doc, obj[3]))
    if family == "DeviceN":
        names = r(obj[1]) if len(obj) >= 4 else None
        if not isinstance(names, list):
            return None
        base = load_colorspace(doc, obj[2], None, depth + 1)
        return _through("DeviceN", len(names), base, _function(doc, obj[3]))
    if family == "Pattern":
        base = load_colorspace(doc, obj[1], None, depth + 1) if len(obj) > 1 else None
        return _through("Pattern", 0, base, None)
    return None


def _defaults(doc: PdfFile, resources: PdfDict | None, name: str, depth: int) -> ColorSpace | None:
    """DefaultGray / DefaultRGB / DefaultCMYK from the resources replace a device space."""
    r = doc.resolve
    spaces = r(resources.get("ColorSpace")) if resources is not None else None
    key = {"DeviceGray": "DefaultGray", "DeviceRGB": "DefaultRGB", "DeviceCMYK": "DefaultCMYK"}[name]
    if not isinstance(spaces, dict) or key not in spaces:
        return None
    cs = load_colorspace(doc, spaces[key], None, depth + 1)
    return cs if cs is not None and cs.n == DEVICE[name].n and not cs.is_pattern else None
