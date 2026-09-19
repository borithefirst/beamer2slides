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
    out = bytearray(len(data))
    r = key
    for i, c in enumerate(data):
        out[i] = c ^ (r >> 8)
        r = ((c + r) * 52845 + 22719) & 0xFFFF
    return bytes(out)


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
