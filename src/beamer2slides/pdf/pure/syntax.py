"""PDF syntax: tokens, objects, content-stream operations (ISO 32000-1, 7.2-7.3, 7.8.2)."""

from __future__ import annotations

import re
import struct
import zlib
from fractions import Fraction
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
# A number is any word of digits, signs and dots (PDFCharIsNumeric): "--5" and "5..5" are numbers
# to PDFium too, read by FX_Number's rules (`_number`).
_TOKEN = re.compile(
    rb"[\x00\t\n\x0c\r ]*"                    # whitespace
    rb"(?:"
    rb"(?P<num>[0-9+\-.]+)(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
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


# Several values rounded to float32 at once: `unpack(pack(a, b, ...))` is `float32` of each, in one
# pair of C calls. `pack` raises OverflowError where `float32` gives an infinity, so a caller doing
# this keeps a `float32` path to fall back on (the hot paths below: raster, textpage, content).
F32X2, F32X4, F32X6 = struct.Struct("<2f"), struct.Struct("<4f"), struct.Struct("<6f")


_REAL = re.compile(rb"-?(?:\d+\.?\d*|\.\d+)")
_DOUBLE_BITS = struct.Struct("<Q")
_DOUBLE = struct.Struct("<d")
_FLOAT_BITS = struct.Struct("<I")


def string_to_float(token: bytes) -> float:
    """StringToFloat: leading blanks and signs skipped (the last minus kept), then fast_float's
    from_chars on the longest valid prefix, rounded once, straight to float (0 without digits)."""
    start = 0
    while start < len(token) and token[start] in b" +-":
        start += 1
    if start and token[start - 1] == 0x2D:
        start -= 1
    m = _REAL.match(token, start)
    if m is None:
        return 0.0
    text = m.group()
    d = float(text)
    f = float32(d)
    if d != f:
        # float(text) rounded once already, and a double exactly halfway between two floats can
        # then round the other way than the decimal would (double rounding): settle those exactly
        bits = _DOUBLE_BITS.unpack(_DOUBLE.pack(d))[0]
        if (bits & 0x1FFFFFFF) == 0x10000000 or abs(d) < 1.2e-38:
            f = _nearest_float32(Fraction(text.decode()))
    return f


def _nearest_float32(q: Fraction) -> float:
    """The float nearest to the rational `q`, ties to even (finite, positive or negative)."""
    a = float32(float(q))
    if Fraction(a) == q or abs(a) == float("inf"):
        return a
    # the neighbour of `a` on q's side: one unit in the last place towards q
    bits = _FLOAT_BITS.unpack(_FLOAT32.pack(a))[0]
    away = (Fraction(a) < q) == (a >= 0)
    if a == 0:
        b = _FLOAT32.unpack(_FLOAT_BITS.pack(1 if q > 0 else 0x80000001))[0]
    else:
        b = _FLOAT32.unpack(_FLOAT_BITS.pack(bits + 1 if away else bits - 1))[0]
    da, db = abs(Fraction(a) - q), abs(Fraction(b) - q)
    if da != db:
        return a if da < db else b
    return a if bits % 2 == 0 else b


def _number(token: bytes):
    """FX_Number: a real (a '.' in it) through StringToFloat; else an integer read as uint32,
    0 when that overflows, or, with a sign, 0 beyond int32."""
    if b"." in token:
        return string_to_float(token)
    i = 0
    neg = False
    if token[:1] in (b"+", b"-"):
        neg, i = token[:1] == b"-", 1
    j = i
    while j < len(token) and 0x30 <= token[j] <= 0x39:
        j += 1
    value = int(token[i:j]) if j > i else 0
    if value > 0xFFFFFFFF:
        value = 0
    if i:
        if value > (0x80000000 if neg else 0x7FFFFFFF):
            value = 0
        return -value if neg else value
    return value


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
    exact: bool = True    # False: a codec whose end PDFium finds differently may have cut the data


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


# CPDF_StreamParser::GetNextWord: whitespace and comments, then a name, '<<' or '>>', one delimiter,
# or a word running to the next delimiter or whitespace.
_WORD = re.compile(
    rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*)*"
    rb"(/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*|<<|>>|[()<>\[\]{}]|[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)?")
_NUMERIC = re.compile(rb"[0-9+\-.]+\Z")
_MAX_WORD = 255      # kMaxWordLength: longer words are cut (the stream is still read to their end)
_MAX_NESTING = 512   # kMaxNestedParsingLevel
_NOTHING = object()  # ReadNextObject's nullptr (unlike a `null` object, never kept in an array)
_PARAM_SLOTS = 16    # the content parser's circular operand buffer: older operands fall out


class _StreamParser:
    """CPDF_StreamParser: the tokenizer of content streams, which is not the file syntax parser.

    Its object reader has its own rules, and they decide what junk becomes: an array inside an
    array at the top level is nothing (only its '[' is consumed, so its ']' closes the outer one);
    a keyword inside an array is skipped; a dictionary with anything but a name for a key, or a key
    without a value, is nothing, read up to that point; a stray ']', '>>', ')', '{' is an operand
    that is no object at all."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos, self.word = data, pos, b""

    def next_word(self) -> bytes:
        m = _WORD.match(self.data, self.pos)
        self.pos = m.end()
        w = m.group(1) or b""
        self.word = w[:_MAX_WORD]
        return self.word

    def element(self):
        """ParseNextElement: ("end",) | ("num", n) | ("name", Name) | ("kw", str) | ("obj", value),
        where value is _NOTHING for a delimiter that starts no object."""
        m = _WORD.match(self.data, self.pos)
        w = m.group(1)
        if not w:
            self.pos = len(self.data)
            return ("end",)
        if w[0] in b"()<>[]{}":
            self.pos = m.start(1)
            return ("obj", self.read_object(False, False, 0))
        self.pos = m.end()
        w = w[:_MAX_WORD]
        self.word = w
        if _NUMERIC.match(w):
            return ("num", _number(w))
        if w[0] == 0x2F:
            return ("name", _name(w))
        if w == b"true" or w == b"false" or w == b"null":
            return ("obj", {b"true": True, b"false": False, b"null": None}[w])
        return ("kw", w.decode("latin-1"))

    def read_object(self, allow_nested: bool, in_array: bool, level: int):
        """ReadNextObject."""
        w = self.next_word()
        if not w or level > _MAX_NESTING:
            return _NOTHING
        if _NUMERIC.match(w):
            return _number(w)
        c = w[0]
        if c == 0x2F:
            return _name(w)
        if c == 0x28:  # (
            value, self.pos = _literal_string(self.data, self.pos)
            return String(value)
        if c == 0x3C:  # <
            if len(w) == 1:
                end = self.data.find(b">", self.pos)
                end = len(self.data) if end < 0 else end
                digits = bytes(b for b in self.data[self.pos:end] if b in _HEX_DIGITS)
                self.pos = min(end + 1, len(self.data))
                s = String(bytes.fromhex((digits + b"0" if len(digits) % 2 else digits).decode()))
                s.hex = True
                return s
            d = {}
            while True:
                k = self.next_word()
                if k == b">>":
                    return d
                if not k or k[0] != 0x2F:
                    return _NOTHING
                value = self.read_object(True, in_array, level + 1)
                if value is _NOTHING:
                    return _NOTHING
                d[_name(k)] = value
        if c == 0x5B:  # [
            if not allow_nested and in_array:
                return _NOTHING
            out = []
            while True:
                value = self.read_object(allow_nested, True, level + 1)
                if value is not _NOTHING:
                    out.append(value)
                elif not self.word or self.word[0] == 0x5D:
                    return out
        if w == b"false":
            return False
        if w == b"true":
            return True
        if w == b"null":
            return None
        return _NOTHING


_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def _name(word: bytes) -> Name:
    raw = word[1:]
    if b"#" in raw:
        raw = _NAME_ESCAPE.sub(lambda e: bytes([int(e.group(1), 16)]), raw)
    return Name(raw.decode("latin-1"))


class Operands(list):
    """What an operator gets after more than 16 operands: `raw` is every operand as written."""
    raw: list


def _ring(operands: list) -> list:
    """The operands CPDF_StreamContentParser's 16-slot buffer holds after `operands`: once it is
    full, each new one advances the start *and then* goes into the new start slot, so it
    overwrites the second oldest and the oldest stays, read as the last one (GetNextParamPos)."""
    slots, start = operands[:_PARAM_SLOTS], 0
    for value in operands[_PARAM_SLOTS:]:
        start = (start + 1) % _PARAM_SLOTS
        slots[start] = value
    out = Operands(slots[start:] + slots[:start])
    out.raw = operands
    return out


def operations(data: bytes, components=None) -> Iterator[tuple[str, list]]:
    """(operator, operands) of a content stream as CPDF_StreamContentParser::Parse reads it;
    BI...ID...EI comes as ("BI", [InlineImage]). An operand that is no object is None; after more
    than 16 operands the operator gets what PDFium's buffer holds (`_ring`)."""
    parser = _StreamParser(data)
    operands: list = []
    while True:
        e = parser.element()
        kind = e[0]
        if kind == "end":
            return
        if kind != "kw":
            value = e[1]
            operands.append(None if value is _NOTHING else value)
            continue
        op = e[1]
        if op == "BI":
            image = _begin_image(parser, data, components)
            if image is not None:
                yield "BI", [image]
            operands = []
            continue
        yield op, (_ring(operands) if len(operands) > _PARAM_SLOTS else operands)
        operands = []


def _begin_image(parser: _StreamParser, data: bytes, components=None):
    """Handle_BeginImage: /Key value pairs up to ID (any other keyword abandons the image and
    parsing goes on after BI), the data (ReadInlineStream), then every element up to the keyword EI;
    no EI before the end of the stream, or data that does not read, and there is no image.
    `components(colour space object)` is the component count GetColorSpace gives for the image's
    /ColorSpace (the name looked up in the resources first, as FindResourceObj does)."""
    save = parser.pos
    d = {}
    while True:
        e = parser.element()
        if e[0] == "kw" and e[1] != "ID":
            parser.pos = save
            return None
        if e[0] != "name":
            break
        value = parser.read_object(False, False, 0)
        if value is not _NOTHING:
            d[Name(_INLINE_ABBREV.get(e[1], e[1]))] = _inline_value(value)
    parser.exact = True
    got = _read_inline_stream(parser, data, d, components)
    while True:
        e = parser.element()
        if e[0] == "end":
            return None
        if e[0] == "kw" and e[1] == "EI":
            break
    if got is None:
        return None
    return InlineImage(d, got, parser.exact)


_WHITESPACE = b"\x00\t\n\x0c\r "


def _int_value(v) -> int:
    """CPDF_Object::GetInteger of a direct value."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if isinstance(v, float):
        return int(v) if -2147483648.0 < v < 2147483648.0 else 0
    return v


def _device_components(cs) -> int | None:
    """Without resources: the device spaces, nothing for another name, 3 for anything else."""
    if isinstance(cs, Name):
        return {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}.get(str(cs))
    if isinstance(cs, list) and cs and cs[0] == "Indexed":
        return 1
    return 3


def _read_inline_stream(parser: _StreamParser, data: bytes, d: dict, components) -> bytes | None:
    """CPDF_StreamParser::ReadInlineStream: the data bytes, the parser left after them."""
    pos = parser.pos
    if pos >= len(data):
        return None
    if data[pos] in _WHITESPACE:
        pos += 1
        if pos >= len(data):
            parser.pos = pos
            return None
    parser.pos = pos
    decoder, params = "", None
    filt = d.get("Filter")
    if isinstance(filt, list):
        decoder = str(filt[0]) if filt and isinstance(filt[0], (Name, String)) else ""
        dp = d.get("DecodeParms")
        params = dp[0] if isinstance(dp, list) and dp and isinstance(dp[0], dict) else None
    elif filt is not None:
        decoder = str(filt) if isinstance(filt, (Name, String)) else ""
        dp = d.get("DecodeParms")
        params = dp if isinstance(dp, dict) else None
    width = _int_value(d.get("Width")) & 0xFFFFFFFF
    height = _int_value(d.get("Height")) & 0xFFFFFFFF
    bpc, comps = 1, 1
    if "ColorSpace" in d:
        n = (components or _device_components)(d["ColorSpace"])
        if n is not None:
            comps = n
            bpc = _int_value(d.get("BitsPerComponent")) & 0xFFFFFFFF
    bits = bpc * comps * width
    if bits + 7 > 0xFFFFFFFF or bpc * comps > 0xFFFFFFFF:
        return None
    size = (bits + 7) // 8 * height
    if size > 0xFFFFFFFF:
        return None
    if not decoder:
        size = min(size, len(data) - pos)
        parser.pos = pos + size
        return data[pos:pos + size]
    try:
        used = _inline_consumed(data[pos:], decoder, params)
    except _Inexact as e:
        used, parser.exact = e.used, False
    if used is None or used > 0x7FFFFFFF:
        return None
    # AutoRestorer: the elements after the codec's end, up to EI, belong to the data
    parser.pos = pos + used
    while True:
        before = parser.pos
        e = parser.element()
        if e[0] == "end":
            parser.pos = pos
            return None
        if e[0] == "kw" and e[1] == "EI":
            break
        used += parser.pos - before
    parser.pos = pos + used
    return data[pos:pos + used]


def _inline_consumed(src: bytes, decoder: str, params) -> int | None:
    """DecodeInlineStream: the bytes the first decoder reads (None = FX_INVALID_OFFSET)."""
    if decoder == "FlateDecode":
        z = zlib.decompressobj()
        try:
            z.decompress(src)
        except zlib.error:
            return _flate_error_consumed(src)
        return len(src) - len(z.unused_data) if z.eof else len(src)
    if decoder == "ASCII85Decode":
        return _a85_consumed(src)
    if decoder == "ASCIIHexDecode":
        end = src.find(b">")
        return end + 1 if end >= 0 else len(src)
    if decoder == "RunLengthDecode":
        i = 0
        while i < len(src):
            if src[i] == 128:
                break
            i += src[i] + 2 if src[i] < 128 else 2
        return min(i + 1, len(src))
    if decoder == "LZWDecode":
        early = params.get("EarlyChange", 1) if isinstance(params, dict) else 1
        return _lzw_consumed(src, 1 if _int_value(early) else 0)
    if decoder in ("DCTDecode", "CCITTFaxDecode"):
        # where libjpeg / the fax decoder stop is not ported: the end of the JPEG, and the
        # image is marked inexact (`InlineImage.exact`), so that it is never drawn
        end = src.find(b"\xff\xd9") if decoder == "DCTDecode" else -1
        raise _Inexact(end + 2 if end >= 0 else 0)
    return None


class _Inexact(Exception):
    def __init__(self, used: int):
        self.used = used


def _flate_error_consumed(src: bytes) -> int:
    """zlib's total_in when inflate stops on damaged data: the byte it failed on counts."""
    z = zlib.decompressobj()
    for k in range(len(src)):
        try:
            z.decompress(src[k:k + 1])
        except zlib.error:
            return k + 1
        if z.eof:
            return k + 1
    return len(src)


def _lzw_consumed(src: bytes, early: int) -> int | None:
    """CLZWDecoder::Decode's GetSrcSize, following only the code lengths (None: it fails)."""
    nbits = len(src) * 8
    pos, code_len, current, old, out = 0, 9, 0, None, 0
    val = int.from_bytes(src, "big") if src else 0

    def add():
        nonlocal current, code_len
        if current + early == 4094:
            return
        current += 1
        if current + early == 512 - 258:
            code_len = 10
        elif current + early == 1024 - 258:
            code_len = 11
        elif current + early == 2048 - 258:
            code_len = 12

    while pos + code_len <= nbits:
        code = (val >> (nbits - pos - code_len)) & ((1 << code_len) - 1)
        pos += code_len
        if code < 256:
            out += 1
            if old is not None:
                add()
            old = code
            continue
        if code == 256:
            code_len, current, old = 9, 0, None
            continue
        if code == 257:
            break
        if old is None:
            return None
        out += 1
        if old >= 258 and old - 258 >= current:
            break
        add()
        old = code
    return (pos + 7) // 8 if out else None


def _a85_consumed(src: bytes) -> int:
    if not src:
        return 0
    pos = 0
    while pos < len(src):
        ch = src[pos]
        if ch != 0x7A and (ch < 0x21 or ch > 0x75) and ch not in b"\r\n \t":
            break
        pos += 1
    if pos == 0:
        return 0
    pos = 0
    while pos < len(src):
        ch = src[pos]
        pos += 1
        if ch in b"\r\n \t" or ch == 0x7A:
            continue
        if ch < 0x21 or ch > 0x75:
            break
    if pos < len(src) and src[pos] == 0x3E:
        pos += 1
    return pos
