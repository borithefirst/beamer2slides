"""PDFium's CIE-based colour spaces, value for value (cpdf_colorspace.cpp): CalRGB (gamma, matrix,
then XYZ to sRGB under the space's white point through PDFium's own 3x3 inverses), Lab (its
piecewise curve and fixed D65 matrix) and the sRGB companding both end in (RGB_Conversion: a
1024-step table, not a formula). Every operation is float32, in the order the C++ writes it; powf
is the C runtime's (`crt.py`: the one PDFium links on each platform)."""

from __future__ import annotations

from .syntax import float32 as F

_SAMPLES1 = (
    0, 3, 6, 10, 13, 15, 18, 20, 22, 23, 25, 27, 28, 30, 31,
    32, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47,
    48, 49, 49, 50, 51, 52, 53, 53, 54, 55, 56, 56, 57, 58, 58,
    59, 60, 61, 61, 62, 62, 63, 64, 64, 65, 66, 66, 67, 67, 68,
    68, 69, 70, 70, 71, 71, 72, 72, 73, 73, 74, 74, 75, 76, 76,
    77, 77, 78, 78, 79, 79, 79, 80, 80, 81, 81, 82, 82, 83, 83,
    84, 84, 85, 85, 85, 86, 86, 87, 87, 88, 88, 88, 89, 89, 90,
    90, 91, 91, 91, 92, 92, 93, 93, 93, 94, 94, 95, 95, 95, 96,
    96, 97, 97, 97, 98, 98, 98, 99, 99, 99, 100, 100, 101, 101, 101,
    102, 102, 102, 103, 103, 103, 104, 104, 104, 105, 105, 106, 106, 106, 107,
    107, 107, 108, 108, 108, 109, 109, 109, 110, 110, 110, 110, 111, 111, 111,
    112, 112, 112, 113, 113, 113, 114, 114, 114, 115, 115, 115, 115, 116, 116,
    116, 117, 117, 117, 118, 118, 118, 118, 119, 119, 119, 120)

_SAMPLES2 = (
    120, 121, 122, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135,
    136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 148, 149,
    150, 151, 152, 153, 154, 155, 155, 156, 157, 158, 159, 159, 160, 161, 162,
    163, 163, 164, 165, 166, 167, 167, 168, 169, 170, 170, 171, 172, 173, 173,
    174, 175, 175, 176, 177, 178, 178, 179, 180, 180, 181, 182, 182, 183, 184,
    185, 185, 186, 187, 187, 188, 189, 189, 190, 190, 191, 192, 192, 193, 194,
    194, 195, 196, 196, 197, 197, 198, 199, 199, 200, 200, 201, 202, 202, 203,
    203, 204, 205, 205, 206, 206, 207, 208, 208, 209, 209, 210, 210, 211, 212,
    212, 213, 213, 214, 214, 215, 215, 216, 216, 217, 218, 218, 219, 219, 220,
    220, 221, 221, 222, 222, 223, 223, 224, 224, 225, 226, 226, 227, 227, 228,
    228, 229, 229, 230, 230, 231, 231, 232, 232, 233, 233, 234, 234, 235, 235,
    236, 236, 237, 237, 238, 238, 238, 239, 239, 240, 240, 241, 241, 242, 242,
    243, 243, 244, 244, 245, 245, 246, 246, 246, 247, 247, 248, 248, 249, 249,
    250, 250, 251, 251, 251, 252, 252, 253, 253, 254, 254, 255, 255)

FLT_EPSILON = 1.1920928955078125e-07
_powf_fn = None


def powf(x: float, y: float) -> float:
    global _powf_fn
    if _powf_fn is None:
        from .crt import float_fn
        _powf_fn = float_fn("powf", 2)
    return float(_powf_fn(x, y))


def _i32(v: float) -> int:
    if v != v or v >= 2147483648.0 or v < -2147483648.0:
        return -(1 << 31)
    return int(v)


def rgb_conversion(c: float) -> float:
    """RGB_Conversion: clamp, then the sRGB table at c * 1023."""
    c = 0.0 if c < 0.0 else 1.0 if 1.0 < c else c
    scale = max(_i32(F(c * 1023.0)), 0)
    if scale < 192:
        return F(_SAMPLES1[scale] / 255.0)
    return F(_SAMPLES2[scale // 4 - 48] / 255.0)


def xyz_to_srgb(x: float, y: float, z: float) -> tuple:
    r = F(F(F(F(3.2410) * x) -F(F(1.5374) * y)) - F(F(0.4986) * z))
    g = F(F(F(F(-0.9692) * x) + F(F(1.8760) * y)) + F(F(0.0416) * z))
    b = F(F(F(F(0.0556) * x) - F(F(0.2040) * y)) + F(F(1.0570) * z))
    return rgb_conversion(r), rgb_conversion(g), rgb_conversion(b)


def _inverse(m: tuple) -> tuple:
    """Matrix_3by3::Inverse (all zeros when |det| < FLT_EPSILON)."""
    a, b, c, d, e, f, g, h, i = m
    det = F(F(F(a * F(F(e * i) - F(f * h))) - F(b * F(F(i * d) - F(f * g)))) + F(c * F(F(d * h) - F(e * g))))
    if abs(det) < FLT_EPSILON:
        return (0.0,) * 9
    return (F(F(F(e * i) - F(f * h)) / det), F(-F(F(b * i) - F(c * h)) / det), F(F(F(b * f) - F(c * e)) / det),
            F(-F(F(d * i) - F(f * g)) / det), F(F(F(a * i) - F(c * g)) / det), F(-F(F(a * f) - F(c * d)) / det),
            F(F(F(d * h) - F(e * g)) / det), F(-F(F(a * h) - F(b * g)) / det), F(F(F(a * e) - F(b * d)) / det))


def _dot3(p, q, r, u, v, w) -> float:
    return F(F(F(p * u) + F(q * v)) + F(r * w))


def _transform(m: tuple, v: tuple) -> tuple:
    a, b, c, d, e, f, g, h, i = m
    return _dot3(a, b, c, *v), _dot3(d, e, f, *v), _dot3(g, h, i, *v)


def _multiply(m: tuple, n: tuple) -> tuple:
    a, b, c, d, e, f, g, h, i = m
    na, nb, nc, nd, ne, nf, ng, nh, ni = n
    return (_dot3(a, b, c, na, nd, ng), _dot3(a, b, c, nb, ne, nh), _dot3(a, b, c, nc, nf, ni),
            _dot3(d, e, f, na, nd, ng), _dot3(d, e, f, nb, ne, nh), _dot3(d, e, f, nc, nf, ni),
            _dot3(g, h, i, na, nd, ng), _dot3(g, h, i, nb, ne, nh), _dot3(g, h, i, nc, nf, ni))


_RX, _RY, _GX, _GY, _BX, _BY = F(0.64), F(0.33), F(0.30), F(0.60), F(0.15), F(0.06)
_RGB_XYZ = (_RX, _GX, _BX, _RY, _GY, _BY,
            F(F(1.0 - _RX) - _RY), F(F(1.0 - _GX) - _GY), F(F(1.0 - _BX) - _BY))


def xyz_to_srgb_whitepoint(x, y, z, xw, yw, zw) -> tuple:
    s = _transform(_inverse(_RGB_XYZ), (xw, yw, zw))
    m = _multiply(_RGB_XYZ, (s[0], 0.0, 0.0, 0.0, s[1], 0.0, 0.0, 0.0, s[2]))
    r, g, b = _transform(_inverse(m), (x, y, z))
    return rgb_conversion(r), rgb_conversion(g), rgb_conversion(b)


def calrgb(white: tuple, gamma: tuple | None, matrix: tuple | None, buf) -> tuple:
    """CPDF_CalRGB::GetRGB."""
    a, b, c = buf[0], buf[1], buf[2]
    if gamma is not None:
        a, b, c = powf(a, gamma[0]), powf(b, gamma[1]), powf(c, gamma[2])
    if matrix is not None:
        m = matrix
        x, y, z = _dot3(m[0], m[3], m[6], a, b, c), _dot3(m[1], m[4], m[7], a, b, c), _dot3(m[2], m[5], m[8], a, b, c)
    else:
        x, y, z = a, b, c
    return xyz_to_srgb_whitepoint(x, y, z, *white)


_C0957, _C12842, _C1379, _C2069, _C10889 = F(0.957), F(0.12842), F(0.1379), F(0.2069), F(1.0889)


def lab(buf) -> tuple:
    """CPDF_LabCS::GetRGB."""
    ls, a_, b_ = buf[0], buf[1], buf[2]
    m = F(F(ls + 16.0) / 116.0)
    l = F(m + F(a_ / 500.0))
    n = F(m - F(b_ / 200.0))
    if l < _C2069:
        x = F(F(_C0957 * _C12842) * F(l - _C1379))
    else:
        x = F(F(F(_C0957 * l) * l) * l)
    if m < _C2069:
        y = F(_C12842 * F(m - _C1379))
    else:
        y = F(F(m * m) * m)
    if n < _C2069:
        z = F(F(_C10889 * _C12842) * F(n - _C1379))
    else:
        z = F(F(F(_C10889 * n) * n) * n)
    return xyz_to_srgb(x, y, z)


def lab_default(ranges: tuple, i: int) -> tuple:
    """CPDF_LabCS::GetDefaultValue: (value, min, max) of component i."""
    if i > 0:
        lo, hi = ranges[i * 2 - 2], ranges[i * 2 - 1]
        if lo <= hi:
            v = lo if 0.0 < lo else hi if hi < 0.0 else 0.0
            return v, lo, hi
    return 0.0, 0.0, 100.0


def white_point(get_array) -> tuple | None:
    """GetWhitePoint: three numbers, X > 0, Y == 1, Z > 0; `get_array()` gives them as floats."""
    w = get_array()
    if w is None or len(w) != 3:
        return None
    return tuple(w) if w[0] > 0.0 and w[1] == 1.0 and w[2] > 0.0 else None
