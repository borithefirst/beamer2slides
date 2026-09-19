"""A Type 1 program as FreeType's type1 driver loads it (t1parse.c, t1load.c), multiple masters included.

fontTools reads the embedded Type 1 fonts well enough, but not PDFium's generic faces FoxitSansMM and
FoxitSerifMM: they are multiple master fonts whose PostScript builds the blended font at run time
(`makeblendedfont`), which fontTools' interpreter cannot execute. FreeType never executes it either:
its parser is a keyword scanner over the cleartext and the eexec part. This module is that scanner,
for the keywords the glyph loader and the face need:

- the PFB segments (t1parse's `read_pfb_tag`) or a raw file, and eexec decryption (key 55665, the
  four random bytes dropped);
- /lenIV (default 4), /Subrs (`dup i n RD <n bytes> NP`) and /CharStrings (`/name n RD <n bytes> ND`),
  each charstring decrypted with key 4330 and its first lenIV bytes cut (`t1_decrypt`); the binary
  data starts one byte after the RD token (`read_binary_data`);
- the glyph order of /CharStrings, with /.notdef swapped into index 0 (`parse_charstrings`: a swap,
  not a move);
- the /Encoding array (`dup <code> /<name> put`) or StandardEncoding;
- the blend: /BlendDesignPositions gives num_designs, /WeightVector the weights (`T1_ToFixed`, which
  is `PS_Conv_ToFixed(.., 0)`), /BuildCharArray its length; /FontBBox goes to every design's bbox,
  and the /Blend dictionary's /FontBBox (an array of four arrays: xMins, yMins, xMaxs, yMaxs) replaces
  them, design 0's being the face's (`T1_FIELD_TYPE_MM_BBOX`, `FT_RoundFix` on each value).

The face bbox is then what T1_Face_Init makes of font_bbox: mins `>> 16`, maxes `(v + 0xFFFF) >> 16`;
ascender = yMax, descender = yMin, units per em 1000 (the FontMatrix these faces carry).
"""
from __future__ import annotations

import re

EEXEC_KEY = 55665
CHARSTRING_KEY = 4330

_SPACE = b" \t\r\n\x0c\x00"


def decrypt(data: bytes, key: int) -> bytes:
    """t1_decrypt / the eexec cipher."""
    if len(data) >= _VECTOR_MIN:
        return decrypt_many([data], key)[0]
    out = bytearray(len(data))
    r = key
    for i, c in enumerate(data):
        out[i] = c ^ (r >> 8)
        r = ((c + r) * 52845 + 22719) & 0xFFFF
    return bytes(out)


# The cipher's key runs r' = A·r + (A·c + B) mod 2^16, an affine recurrence in which every c is known
# up front, so it has a closed form: with s_i = A^-i·r_i, s_(i+1) = s_i + A^-(i+1)·(A·c_i + B), so
# s is a running sum and r_i = A^i·s_i. A is odd, hence invertible mod 2^16, and numpy's uint64
# products and sums wrap mod 2^64, of which 2^16 is a factor: every low 16 bits are exact.
_A, _B = 52845, 22719
_A_INV = pow(_A, -1, 1 << 16)
_VECTOR_MIN = 192
_powers: tuple = (0, None, None)


def _power_tables(n: int):
    """(A^i, A^-(i+1)) mod 2^16 for i < n, as uint64."""
    global _powers
    if _powers[0] < n:
        import numpy as np
        size = max(n, 2 * _powers[0], 4096)
        a = np.full(size, _A, np.uint64)
        a[0] = 1
        b = np.full(size, _A_INV, np.uint64)
        _powers = (size, np.cumprod(a) & 0xFFFF, np.cumprod(b) & 0xFFFF)
    return _powers[1], _powers[2]


def decrypt_many(chunks: list, key: int) -> list[bytes]:
    """`decrypt(chunk, key)` for every chunk, each from `key` again, in one numpy pass."""
    import numpy as np
    lengths = [len(c) for c in chunks]
    n = sum(lengths)
    if n == 0:
        return [b"" for _ in chunks]
    apow, ainv = _power_tables(n)
    c = np.frombuffer(b"".join(chunks), np.uint8).astype(np.uint64)
    t = (ainv[:n] * (c * _A + _B)) & 0xFFFF
    total = np.empty(n + 1, np.uint64)            # total[i] = sum of t[k], k < i
    total[0] = 0
    np.cumsum(t, out=total[1:])
    starts = np.cumsum([0] + lengths[:-1]).astype(np.int64)
    first = np.repeat(starts, lengths)            # each byte's chunk start
    # r_i = A^i·(A^-start·key + S_i - S_start), all mod 2^16
    r = (apow[:n] * (_inv_pow(first, ainv) * key + total[:n] - total[first])) & 0xFFFF
    plain = ((c ^ (r >> 8)) & 0xFF).astype(np.uint8).tobytes()
    out, at = [], 0
    for size in lengths:
        out.append(plain[at:at + size])
        at += size
    return out


def _inv_pow(first, ainv):
    """A^-start for each start (A^-0 = 1)."""
    import numpy as np
    out = np.ones(len(first), np.uint64)
    nz = first > 0
    out[nz] = ainv[first[nz] - 1]
    return out


def to_fixed(token: bytes) -> int:
    """PS_Conv_ToFixed(cursor, limit, 0) on a plain decimal ([-+]digits[.digits]), 16.16."""
    s = token
    sign = s[:1] == b"-"
    if s[:1] in (b"-", b"+"):
        s = s[1:]
    whole, _, frac = s.partition(b".")
    integral = 0
    if whole:
        if not whole.isdigit():
            return 0
        integral = int(whole)
        if integral > 0x7FFF:
            return -0x7FFFFFFF if sign else 0x7FFFFFFF
        integral <<= 16
    decimal, divider = 0, 1
    for ch in frac:
        if not 0x30 <= ch <= 0x39:
            break
        if divider < 0xCCCCCCC and decimal < 0xCCCCCCC:
            decimal = decimal * 10 + ch - 0x30
            divider *= 10
    if not integral and not decimal:
        return 0
    if decimal:
        integral += ((decimal << 16) + (divider >> 1)) // divider      # FT_DivFix, both positive
    return -integral if sign else integral


def round_fix(v: int) -> int:
    """FT_RoundFix."""
    return (v + 0x8000 - (1 if v < 0 else 0)) & ~0xFFFF


class Type1Program:
    """What the loader keeps: glyph order, decrypted charstrings and subrs, encoding, blend, bbox."""

    def __init__(self):
        self.order: list[str] = []
        self.charstrings: dict[str, bytes] = {}
        self.subrs: list[bytes | None] = []
        self.encoding: list[str] | None = None
        self.weight_vector: list[int] | None = None
        self.num_designs = 0
        self.design_map: list[tuple[list[int], list[int]]] = []   # per axis: design points, blend points
        self.len_buildchar = 0
        self.font_bbox = (0, 0, 0, 0)          # 16.16, after the blend
        self.bbox = (0, 0, 0, 0)               # the face's, whole units


def _segments(data: bytes) -> tuple[bytes, bytes]:
    """The cleartext and the (still encrypted) eexec part."""
    if data[:1] == b"\x80":
        clear, binary, i = bytearray(), bytearray(), 0
        while i + 6 <= len(data) and data[i] == 0x80 and data[i + 1] in (1, 2):
            kind = data[i + 1]
            size = int.from_bytes(data[i + 2:i + 6], "little")
            chunk = data[i + 6:i + 6 + size]
            (clear if kind == 1 and not binary else binary).extend(chunk if kind == 2 or not binary else b"")
            i += 6 + size
        return bytes(clear), bytes(binary)
    at = data.find(b"eexec")
    if at < 0:
        raise ValueError("no eexec section")
    j = at + 5
    while j < len(data) and data[j] in _SPACE:
        j += 1
    body = data[j:]
    if body[:4] and all(chr(c) in "0123456789abcdefABCDEF" for c in body[:4]):
        body = bytes.fromhex(re.sub(rb"[^0-9a-fA-F]", b"", body).decode())
    return data[:at], body


def _numbers(text: bytes) -> list[bytes]:
    return re.findall(rb"[-+]?(?:\d+\.?\d*|\.\d+)", text)


def _binary_items(text: bytes, start: int, head: re.Pattern, stop: re.Pattern):
    """(match, data) for each `<head> n RD <n bytes>` from `start`, until `stop` matches first."""
    pos = start
    while True:
        m = head.search(text, pos)
        s = stop.search(text, pos)
        if m is None or (s is not None and s.start() < m.start()):
            return
        n = int(m.group("n"))
        begin = m.end() + 1                     # one byte after the RD token
        yield m, text[begin:begin + n]
        pos = begin + n


def parse(data: bytes) -> Type1Program:
    clear, binary = _segments(data)
    private = decrypt(binary, EEXEC_KEY)[4:]
    p = Type1Program()

    # the blend
    m = re.search(rb"/BlendDesignPositions\s*\[(.*?)\]\s*def", clear, re.S)
    if m:
        p.num_designs = m.group(1).count(b"[")
    m = re.search(rb"/WeightVector\s*\[([^\]]*)\]", clear)
    if m and p.num_designs:
        weights = [to_fixed(t) for t in _numbers(m.group(1))]
        if len(weights) == p.num_designs:
            p.weight_vector = weights
    # parse_blend_design_map: per axis [[design blend] ...], designs T1_ToInt, blends T1_ToFixed
    m = re.search(rb"/BlendDesignMap\s*\[(.*?\]\s*\])\s*\]\s*def", clear, re.S)
    if m:
        axes = re.findall(rb"\[((?:\s*\[[^\[\]]*\])+)\s*\]", m.group(1))
        dmap = []
        for axis in axes:
            pts = [_numbers(pt) for pt in re.findall(rb"\[([^\[\]]*)\]", axis)]
            dmap.append(([int(float(p[0])) for p in pts], [to_fixed(p[1]) for p in pts]))
        p.design_map = dmap
    m = re.search(rb"/BuildCharArray\s+(\d+)\s+array", clear + private)
    if m:
        p.len_buildchar = int(m.group(1))

    # FontBBox: the font's own, then the /Blend dictionary's per design
    m = re.search(rb"/FontBBox\s*[\[{]([^\]{}]*)[\]}]", clear)
    if m:
        v = [round_fix(to_fixed(t)) for t in _numbers(m.group(1))[:4]]
        if len(v) == 4:
            p.font_bbox = tuple(v)
    if p.weight_vector is not None:
        m = re.search(rb"/FontBBox\s*\{\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\}", clear)
        if m:
            cols = [[round_fix(to_fixed(t)) for t in _numbers(m.group(i + 1))] for i in range(4)]
            if all(len(c) >= p.num_designs for c in cols):
                p.font_bbox = (cols[0][0], cols[1][0], cols[2][0], cols[3][0])
    x0, y0, x1, y1 = p.font_bbox
    p.bbox = (x0 >> 16, y0 >> 16, (x1 + 0xFFFF) >> 16, (y1 + 0xFFFF) >> 16)

    # the encoding
    m = re.search(rb"/Encoding\s+StandardEncoding", clear)
    if m is None:
        m = re.search(rb"/Encoding\s+\d+\s+array", clear)
        if m:
            enc = [".notdef"] * 256
            end = clear.find(b"readonly def", m.end())
            end = clear.find(b" def", m.end()) if end < 0 else end
            for code, name in re.findall(rb"dup\s+(\d+)\s*/([^\s/\[\]{}()<>%]+)\s+put", clear[m.end():end]):
                if int(code) < 256:
                    enc[int(code)] = name.decode("latin-1")
            p.encoding = enc

    # the private dictionary
    m = re.search(rb"/lenIV\s+(-?\d+)", private)
    len_iv = int(m.group(1)) if m else 4

    def charstring(raw: bytes) -> bytes:
        if len_iv < 0:
            return raw
        if len(raw) <= len_iv:
            raise ValueError("charstring shorter than lenIV")
        return decrypt(raw, CHARSTRING_KEY)[len_iv:]

    m = re.search(rb"/Subrs\s+(\d+)\s+array", private)
    if m:
        p.subrs = [None] * int(m.group(1))
        head = re.compile(rb"dup\s+(?P<i>\d+)\s+(?P<n>\d+)\s+(?:RD|-\|)(?=\s)")
        stop = re.compile(rb"/CharStrings|\bND\b|\|-|\bdef\b")
        for hm, raw in _binary_items(private, m.end(), head, stop):
            i = int(hm.group("i"))
            if i < len(p.subrs):
                p.subrs[i] = charstring(raw)
    m = re.search(rb"/CharStrings\s+\d+\s+dict\s+dup\s+begin", private)
    if m is None:
        raise ValueError("no CharStrings")
    head = re.compile(rb"/(?P<name>[^\s/\[\]{}()<>%]+)\s+(?P<n>\d+)\s+(?:RD|-\|)(?=\s)")
    stop = re.compile(rb"(?<![\w.])end(?![\w.])")
    for hm, raw in _binary_items(private, m.end(), head, stop):
        name = hm.group("name").decode("latin-1")
        if name in p.charstrings:
            continue
        p.order.append(name)
        p.charstrings[name] = charstring(raw)
    if ".notdef" in p.charstrings and p.order[0] != ".notdef":
        k = p.order.index(".notdef")
        p.order[0], p.order[k] = p.order[k], p.order[0]
    return p


# ---------------------------------------------------------------- fontTools' reading, faster and shared
#
# The embedded Type 1 programs are read by fontTools (`t1Lib.T1Font.parse`: its PostScript interpreter,
# then every charstring and subroutine decrypted). Most of that time is fontTools' eexec cipher, one
# Python call per byte; `fonttools_font` runs the same interpreter with the cipher above and gives
# the same dictionary. The parse is kept per program bytes (the extraction and the glyph loader read
# the same program, and a talk's fonts come back in every PDF of the process); each call hands out
# fresh charstring objects, since fontTools' drawing replaces a charstring's bytecode.

_T1_CACHE: dict = {}
_T1_CACHE_SIZE = 64


def _interpreter_class():
    from fontTools.misc import psLib

    class Interpreter(psLib.PSInterpreter):
        def ps_eexec(self):
            f = self.pop("filetype").value
            # PSTokenizer.starteexec with the fast cipher
            f.pos = f.pos + 1
            f.dirtybuf = f.buf[f.pos:]
            f.buf = decrypt(f.dirtybuf, EEXEC_KEY)
            f.len = len(f.buf)
            f.pos = 4

    return Interpreter


_Interpreter = None


def _suckfont(data: bytes, encoding: str = "ascii"):
    """psLib.suckfont over the fast cipher."""
    global _Interpreter
    from fontTools.misc import psLib
    if _Interpreter is None:
        _Interpreter = _interpreter_class()
    m = re.search(rb"/FontName\s+/([^ \t\n\r]+)\s+def", data)
    font_name = m.group(1).decode() if m else None
    interpreter = _Interpreter(encoding=encoding)
    interpreter.interpret(b"/Helvetica 4 dict dup /Encoding StandardEncoding put definefont pop")
    interpreter.interpret(data)
    fontdir = interpreter.dictstack[0]["FontDirectory"].value
    if font_name in fontdir:
        rawfont = fontdir[font_name]
    else:
        names = list(fontdir.keys())
        if len(names) > 1:
            names.remove("Helvetica")
        names.sort()
        rawfont = fontdir[names[0]]
    interpreter.close()
    return psLib.unpack_item(rawfont)


def _as_bytes(v) -> bytes:
    return v if isinstance(v, bytes) else v.encode("latin-1")


def _parsed(data: bytes):
    """(font dict without CharStrings/Subrs, names, charstring bytes, subr bytes) as T1Font.parse
    leaves them, from the cache."""
    hit = _T1_CACHE.get(data)
    if hit is not None:
        return hit
    font = _suckfont(data)
    charstrings = font["CharStrings"]
    len_iv = font["Private"].get("lenIV", 4)
    assert len_iv >= 0
    subrs = font["Private"]["Subrs"]
    names = list(charstrings.keys())
    plain = decrypt_many([_as_bytes(charstrings[n]) for n in names] + [_as_bytes(s) for s in subrs],
                         CHARSTRING_KEY)
    glyph_codes = [p[len_iv:] for p in plain[:len(names)]]
    subr_codes = [p[len_iv:] for p in plain[len(names):]]
    hit = (font, names, glyph_codes, subr_codes)
    if len(_T1_CACHE) >= _T1_CACHE_SIZE:
        _T1_CACHE.pop(next(iter(_T1_CACHE)))
    _T1_CACHE[data] = hit
    return hit


def fonttools_font(data: bytes) -> dict:
    """`T1Font.parse`'s `font` dictionary for a program: CharStrings and Private/Subrs hold fresh
    T1CharString objects, the rest is shared and must not be changed."""
    from fontTools.misc import psCharStrings
    font, names, glyph_codes, subr_codes = _parsed(data)
    subrs: list = []
    subrs.extend(psCharStrings.T1CharString(code, subrs=subrs) for code in subr_codes)
    out = dict(font)
    out["Private"] = dict(font["Private"], Subrs=subrs)
    out["CharStrings"] = {n: psCharStrings.T1CharString(code, subrs=subrs) for n, code in zip(names, glyph_codes)}
    return out


def fonttools_codes(data: bytes):
    """(font dict, {glyph name: charstring bytecode}, [subr bytecode]) without charstring objects."""
    font, names, glyph_codes, subr_codes = _parsed(data)
    return font, dict(zip(names, glyph_codes)), list(subr_codes)
