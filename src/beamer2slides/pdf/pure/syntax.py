"""PDF syntax: tokens, objects, content-stream operations (ISO 32000-1, 7.2-7.3, 7.8.2)."""

from __future__ import annotations

import re
import struct
from typing import Iterator, NamedTuple

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"


class PdfSyntaxError(Exception):
    pass


class Name(str):
    """/Name (decoded: #xx escapes resolved)."""
    __slots__ = ()

    def __repr__(self):
        return "/" + str.__str__(self)


class Op(str):
    """A bare keyword: a content-stream operator, or true/false/null/R/obj... in a file."""
    __slots__ = ()

    def __repr__(self):
        return "Op(" + str.__str__(self) + ")"


class Ref(NamedTuple):
    num: int
    gen: int

    def __repr__(self):
        return f"{self.num} {self.gen} R"


class String(bytes):
    """A string object (bytes); `hex` tells how it was written."""
    hex: bool = False


class Stream:
    """A stream object: its dictionary and its bytes as stored (still encoded)."""
    __slots__ = ("dict", "raw", "_decoded")

    def __init__(self, d: dict, raw: bytes):
        self.dict, self.raw, self._decoded = d, raw, None

    def get(self, key, default=None):
        return self.dict.get(key, default)

    def __repr__(self):
        return f"Stream({self.dict!r}, {len(self.raw)} bytes)"


# One token: a number, a name, a keyword, or a delimiter. Strings and comments are scanned by hand.
_TOKEN = re.compile(
    rb"[\x00\t\n\x0c\r ]*"                    # whitespace
    rb"(?:"
    rb"(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
    rb"|/(?P<name>[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"
    rb"|(?P<dict><<|>>)"
    rb"|(?P<delim>[\[\]{}])"
    rb"|(?P<hex><[0-9A-Fa-f\x00\t\n\x0c\r ]*>)"
    rb"|(?P<str>\()"
    rb"|(?P<comment>%[^\r\n]*)"
    rb"|(?P<word>[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
    rb"|(?P<junk>[)<>])"
    rb")")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_STRING_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
                   ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}

END = object()        # end of data
ARRAY_END, DICT_END = Op("]"), Op(">>")


def _literal_string(data: bytes, pos: int) -> tuple[bytes, int]:
    """The string starting after '(' at pos; returns (bytes, position after ')')."""
    out = bytearray()
    depth = 1
    n = len(data)
    i = pos
    while i < n:
        j = i
        # copy a run of ordinary bytes at once
        while j < n and data[j] not in b"()\\\r":
            j += 1
        out += data[i:j]
        if j >= n:
            break
        c = data[j]
        if c == 0x28:  # (
            depth += 1
            out.append(c)
            i = j + 1
        elif c == 0x29:  # )
            depth -= 1
            if depth == 0:
                return bytes(out), j + 1
            out.append(c)
            i = j + 1
        elif c == 0x0D:  # an end of line is \n whatever it was
            out.append(0x0A)
            i = j + 2 if data[j + 1:j + 2] == b"\n" else j + 1
        else:  # backslash
            e = data[j + 1] if j + 1 < n else None
            if e is None:
                i = j + 1
            elif e in _STRING_ESCAPES:
                out += _STRING_ESCAPES[e]
                i = j + 2
            elif 0x30 <= e <= 0x37:
                k = j + 1
                while k < n and k < j + 4 and 0x30 <= data[k] <= 0x37:
                    k += 1
                out.append(int(data[j + 1:k], 8) & 0xFF)
                i = k
            elif e == 0x0D:  # line continuation
                i = j + 3 if data[j + 2:j + 3] == b"\n" else j + 2
            elif e == 0x0A:
                i = j + 2
            else:
                out.append(e)
                i = j + 2
    return bytes(out), n


def _hex_string(token: bytes) -> bytes:
    digits = bytes(c for c in token[1:-1] if c not in WHITESPACE)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


_FLOAT32 = struct.Struct("<f")


def float32(v: float) -> float:
    """`v` as PDFium holds it: a C float (CPDF_Number, CFX_Matrix, CFX_PointF are all float)."""
    try:
        return _FLOAT32.unpack(_FLOAT32.pack(v))[0]
    except OverflowError:
        return float("inf") if v > 0 else float("-inf")


def _number(token: bytes):
    if b"." in token:
        return float32(float(token))  # FX_Number: StringToFloat, a float
    return int(token)


class Lexer:
    """Tokens from `data` starting at `pos`: numbers, Name, String, Op, and the delimiters as Op."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def next(self):
        data = self.data
        while True:
            m = _TOKEN.match(data, self.pos)
            if m is None or m.end() == self.pos and m.lastgroup is None:
                self.pos = len(data)
                return END
            self.pos = m.end()
            kind = m.lastgroup
            if kind is None:  # only whitespace left
                return END
            if kind == "num":
                return _number(m.group("num"))
            if kind == "name":
                raw = m.group("name")
                if b"#" in raw:
                    raw = _NAME_ESCAPE.sub(lambda e: bytes([int(e.group(1), 16)]), raw)
                return Name(raw.decode("latin-1"))
            if kind == "str":
                value, self.pos = _literal_string(data, self.pos)
                return String(value)
            if kind == "hex":
                s = String(_hex_string(m.group("hex")))
                s.hex = True
                return s
            if kind == "comment" or kind == "junk":
                continue
            if kind == "dict":
                return Op(m.group("dict").decode())
            if kind == "delim":
                return Op(m.group("delim").decode())
            return Op(m.group("word").decode("latin-1"))


def parse_object(lexer: Lexer, token=None):
    """One object, with `R` references resolved into Ref (file syntax). Returns END at the end."""
    if token is None:
        token = lexer.next()
    if isinstance(token, int) and not isinstance(token, bool):
        # an indirect reference "n g R" needs two tokens of lookahead
        save = lexer.pos
        t2 = lexer.next()
        if isinstance(t2, int):
            save2 = lexer.pos
            t3 = lexer.next()
            if t3 == "R" and type(t3) is Op:
                return Ref(token, t2)
            lexer.pos = save2
        lexer.pos = save
        return token
    if type(token) is Op:
        if token == "[":
            out = []
            while True:
                t = lexer.next()
                if t is END or (type(t) is Op and t == "]"):
                    return out
                out.append(parse_object(lexer, t))
        if token == "<<":
            d = {}
            while True:
                t = lexer.next()
                if t is END or (type(t) is Op and t == ">>"):
                    return d
                if not isinstance(t, Name):
                    continue  # junk where a key should be: skip it
                value = parse_object(lexer)
                if type(value) is Op and value == ">>":
                    d[t] = None
                    return d
                d[t] = value
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "null":
            return None
    return token


# ---------------------------------------------------------------------- content streams


class InlineImage(NamedTuple):
    dict: dict
    data: bytes


_INLINE_ABBREV = {"BPC": "BitsPerComponent", "CS": "ColorSpace", "D": "Decode", "DP": "DecodeParms",
                  "F": "Filter", "H": "Height", "IM": "ImageMask", "I": "Interpolate", "W": "Width",
                  "L": "Length"}
_INLINE_VALUES = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed",
                  "AHx": "ASCIIHexDecode", "A85": "ASCII85Decode", "LZW": "LZWDecode", "Fl": "FlateDecode",
                  "RL": "RunLengthDecode", "CCF": "CCITTFaxDecode", "DCT": "DCTDecode"}


def _inline_value(v):
    if isinstance(v, Name) and v in _INLINE_VALUES:
        return Name(_INLINE_VALUES[v])
    if isinstance(v, list):
        return [_inline_value(x) for x in v]
    return v


def operations(data: bytes) -> Iterator[tuple[str, list]]:
    """(operator, operands) of a content stream; BI...ID...EI comes as ("BI", [InlineImage])."""
    lexer = Lexer(data)
    operands: list = []
    while True:
        token = lexer.next()
        if token is END:
            return
        if type(token) is Op:
            if token in ("[", "<<"):
                operands.append(parse_content_object(lexer, token))
                continue
            if token in ("true", "false", "null"):
                operands.append({"true": True, "false": False, "null": None}[token])
                continue
            if token == "BI":
                d = {}
                while True:
                    key = lexer.next()
                    if key is END or (type(key) is Op and key == "ID"):
                        break
                    value = parse_content_object(lexer)
                    if isinstance(key, Name):
                        d[Name(_INLINE_ABBREV.get(key, key))] = _inline_value(value)
                start = lexer.pos + 1  # one whitespace byte after ID
                end = _inline_end(data, start, d)
                lexer.pos = end + 2
                yield "BI", [InlineImage(d, data[start:end])]
                operands = []
                continue
            yield str(token), operands
            operands = []
        else:
            operands.append(token)


def parse_content_object(lexer: Lexer, token=None):
    """Like parse_object, without references (content streams have none)."""
    if token is None:
        token = lexer.next()
    if type(token) is Op:
        if token == "[":
            out = []
            while True:
                t = lexer.next()
                if t is END or (type(t) is Op and t == "]"):
                    return out
                out.append(parse_content_object(lexer, t))
        if token == "<<":
            d = {}
            while True:
                t = lexer.next()
                if t is END or (type(t) is Op and t == ">>"):
                    return d
                if isinstance(t, Name):
                    d[t] = parse_content_object(lexer)
        if token in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[token]
    return token


_EI = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|$)")


def _inline_end(data: bytes, start: int, d: dict) -> int:
    """Where the inline image's data ends (the whitespace before EI)."""
    if d.get("Filter") is None and not d.get("DecodeParms"):
        # unfiltered: the size is known
        w, h = d.get("Width", 0), d.get("Height", 0)
        bpc = 1 if d.get("ImageMask") else d.get("BitsPerComponent", 8)
        cs = d.get("ColorSpace", "DeviceGray")
        comps = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}.get(cs, 1) if not d.get("ImageMask") else 1
        if isinstance(cs, list) and cs and cs[0] == "Indexed":
            comps = 1
        size = h * ((w * comps * bpc + 7) // 8)
        m = _EI.search(data, start + size) if size else _EI.search(data, start)
        if m and m.start() - (start + size) <= 2:
            return m.start()
    m = _EI.search(data, start)
    return m.start() if m else len(data)
