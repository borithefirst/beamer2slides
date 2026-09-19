"""The file layer: cross-reference tables and streams, object streams, the page tree, page labels,
named destinations, text strings, and writing a new file (ISO 32000-1, 7.5, 7.7, 12.3, 12.4.2)."""

from __future__ import annotations

import codecs
import contextlib
import re
import sys
from pathlib import Path

from .filters import decode as decode_filters
from .syntax import END, Lexer, Name, Op, PdfSyntaxError, Ref, Stream, String, _literal_string, _name, _number

_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")
_PAGE_MAX = 0xFFFFF        # CPDF_Document::kPageMaxNum
_MAX_PAGE_LEVEL = 1024     # kMaxPageLevel
_MAX_OBJECT_NUMBER = 24 * 1024 * 1024  # CPDF_Parser::kMaxObjectNumber
_MAX_XREF_SIZE = _MAX_OBJECT_NUMBER + 1  # kMaxXRefSize
_HEADER_SIZE = 9  # kPDFHeaderSize
_WHITE_OR_DELIM = frozenset(b"\x00\t\n\x0c\r ()<>[]{}/%")


class PdfFileError(Exception):
    pass


def _dict(value) -> dict | None:
    """GetDict: a dictionary, or a stream's dictionary."""
    if isinstance(value, Stream):
        return value.dict
    return value if isinstance(value, dict) else None


def _integer(value) -> int:
    """CPDF_Object::GetInteger: numbers (reals truncated) and booleans, else 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return max(-2 ** 31, min(2 ** 31 - 1, int(value)))
        except (OverflowError, ValueError):
            return 0
    return 0


_GAP = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)*")
_REGULAR = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_NUMERIC = frozenset(b"0123456789+-.")


class _Words:
    """CPDF_SyntaxParser::GetNextWord over file syntax: (word, is_number, start) or None."""

    def __init__(self, data: bytes, pos: int):
        self.data, self.pos, self.last = data, pos, b""

    def next(self):
        data = self.data
        pos = _GAP.match(data, self.pos).end()
        if pos >= len(data):
            self.pos = pos
            return None
        c = data[pos]
        if c in b"()<>[]{}/":
            if c == 0x2F:  # '/'
                end = _REGULAR.match(data, pos + 1).end()
            elif c in b"<>" and data[pos + 1:pos + 2] == bytes([c]):
                end = pos + 2
            else:
                end = pos + 1
            word, is_number = data[pos:end], False
        else:
            end = _REGULAR.match(data, pos).end()
            word = data[pos:end]
            is_number = all(ch in _NUMERIC for ch in word)
        self.pos = end
        word = word[:256]   # the word buffer holds 256 bytes
        self.last = word    # m_WordBuffer: kept when a position is restored, and at the end
        return word, is_number, end - len(word)


def _atoui(word: bytes) -> int:
    """FXSYS_atoui: an optional sign and decimal digits; past 2^32 - 1 the maximum, a minus 0."""
    i = 1 if word[:1] in (b"-", b"+") else 0
    neg = word[:1] == b"-"
    num = 0
    while i < len(word) and 0x30 <= word[i] <= 0x39:
        num = num * 10 + word[i] - 0x30
        if num > 0xFFFFFFFF:
            return 0 if neg else 0xFFFFFFFF
        i += 1
    return 0 if neg and num else num


_NOTHING = object()   # GetObjectBodyInternal's nullptr
_HEX = frozenset(b"0123456789abcdefABCDEF")


def _hex_body(data: bytes, pos: int) -> tuple[bytes, int]:
    """CPDF_SyntaxParser::ReadHexString: hex digits up to `>` (others skipped)."""
    end = data.find(b">", pos)
    end = len(data) if end < 0 else end
    digits = bytes(c for c in data[pos:end] if c in _HEX)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii")), min(end + 1, len(data))


def _body(scan: _Words, strict: bool, stream_at, depth: int = 0):
    """CPDF_SyntaxParser::GetObjectBodyInternal: one object, or _NOTHING (nullptr). Strict only
    at the top: an array must end on `]`, and a dictionary value that is no object ends it.
    `stream_at(dict, pos)` reads the stream after a dictionary: (Stream or _NOTHING, position)."""
    if depth > 64:
        return _NOTHING
    data = scan.data
    saved = scan.pos
    w = scan.next()
    if w is None:
        return _NOTHING
    word, is_number, _ = w
    if is_number:
        after = scan.pos
        w2 = scan.next()
        if w2 is not None and w2[1]:
            w3 = scan.next()
            if w3 is not None and w3[0] == b"R":
                num = _atoui(word)
                return Ref(num, _atoui(w2[0]) & 0xFFFF) if num else _NOTHING
        scan.pos = after
        return _number(word)
    if word in (b"true", b"false"):
        return word == b"true"
    if word == b"null":
        return None
    if word == b"(":
        value, scan.pos = _literal_string(data, scan.pos)
        return String(value)
    if word == b"<":
        value, scan.pos = _hex_body(data, scan.pos)
        s = String(value)
        s.hex = True
        return s
    if word == b"[":
        out = []
        while (item := _body(scan, False, stream_at, depth + 1)) is not _NOTHING:
            out.append(item)
        return out if not strict or scan.last[:1] == b"]" else _NOTHING   # the word that ended it
    if word[:1] == b"/":
        return _name(word)
    if word == b"<<":
        d: dict = {}
        while True:
            w = scan.next()
            if w is None:
                return _NOTHING
            inner = w[0]
            if inner == b">>":
                break
            if inner == b"endobj":
                scan.pos = w[2]
                break
            if inner[:1] != b"/":
                continue
            value = _body(scan, False, stream_at, depth + 1)
            if value is _NOTHING:
                if not strict:
                    continue
                nl = [p for p in (data.find(b"\n", scan.pos), data.find(b"\r", scan.pos)) if p >= 0]
                scan.pos = min(nl) + 1 if nl else len(data)   # ToNextLine
                return _NOTHING
            if len(inner) > 1:
                d[_name(inner)] = value
        after = scan.pos
        w = scan.next()
        if w is None or w[0] != b"stream":
            scan.pos = after
            return d
        stream, scan.pos = stream_at(d, scan.pos)
        return stream
    if word == b">>":
        scan.pos = saved
    return _NOTHING


@contextlib.contextmanager
def _deep_recursion():
    """The page tree may be 1024 levels deep, as deep as PDFium follows it."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 4 * _MAX_PAGE_LEVEL + 200))
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


def _direct_int(value) -> int:
    """GetDirectIntegerFor: a number stored right there (no reference, no boolean), else 0."""
    return 0 if isinstance(value, bool) or not isinstance(value, (int, float)) else _integer(value)


def _str_to_int(data: bytes, pos: int, end: int, bits: int, signed: bool) -> int:
    """FXSYS_StrToInt / StringToIntImpl over data[pos:end]: a sign, then decimal digits; out of
    range is the type's maximum (its minimum for a signed negative); unsigned negatives wrap."""
    neg = pos < end and data[pos] == 0x2D
    if pos < end and data[pos] in b"+-":
        pos += 1
    top = (1 << (bits - 1 if signed else bits)) - 1
    num = 0
    while pos < end and 0x30 <= data[pos] <= 0x39:
        val = data[pos] - 0x30
        if num > (top - val) // 10:
            return -top - 1 if neg and signed else top
        num = num * 10 + val
        pos += 1
    if not neg:
        return num
    return -num if signed else (-num) & ((1 << bits) - 1)


class _XRef:
    """CPDF_CrossRefTable: object number -> [kind, gen, pos, object-stream flag] and a trailer.
    `pos` is a union as in PDFium: a compressed object keeps its archive number in the low 32
    bits and its index in the high ones (VerifyCrossRefTable reads it as a position)."""

    def __init__(self, trailer: dict | None = None):
        self.info: dict[int, list] = {}
        self.trailer = trailer

    def _slot(self, num: int) -> list:
        entry = self.info.get(num)
        if entry is None:
            entry = self.info[num] = ["free", 0, 0, False]
        return entry

    def add_normal(self, num: int, gen: int, pos: int, objstm: bool = False) -> None:
        entry = self._slot(num)
        if entry[1] > gen:
            return
        entry[0], entry[1], entry[2], entry[3] = "normal", gen, pos, entry[3] or objstm

    def add_compressed(self, num: int, archive: int, index: int) -> None:
        entry = self._slot(num)
        if entry[1] > 0 or entry[3]:
            return
        packed = (archive & 0xFFFFFFFF) | (index & 0xFFFFFFFF) << 32
        entry[0], entry[1], entry[2] = "compressed", 0, packed - (1 << 64) if packed >> 63 else packed
        self._slot(archive)[3] = True

    def set_free(self, num: int, gen: int) -> None:
        entry = self._slot(num)
        entry[0], entry[1], entry[2] = "free", gen, 0

    def set_size(self, size: int) -> None:
        """SetObjectMapSize: numbers from `size` on are dropped, and size - 1 exists."""
        if size == 0:
            self.info.clear()
            return
        for num in [n for n in self.info if n >= size]:
            del self.info[num]
        self._slot(size - 1)

    def last(self) -> int:
        return max(self.info) if self.info else 0

    def update(self, top: _XRef) -> None:
        """Update: the entries and trailer keys of `top` win, except that a trailer keeps its own
        /XRefStm and /Prev (or loses them when it had none)."""
        if top.info:
            if not self.info:
                self.info = top.info
            else:
                merged = top.info
                for num, entry in self.info.items():
                    new = merged.get(num)
                    if new is None:
                        merged[num] = entry
                    elif new[0] == "normal" and entry[0] == "normal" and entry[3]:
                        new[3] = True
                self.info = dict(sorted(merged.items()))
        if top.trailer is None:
            return
        if self.trailer is None:
            self.trailer = top.trailer
            return
        new = dict(top.trailer)
        for key in ("XRefStm", "Prev"):
            value = self.trailer.pop(key, None)
            new.pop(key, None)
            if value is not None:
                new[Name(key)] = value
        self.trailer.update(new)


def _merge_up(current: _XRef | None, top: _XRef | None) -> _XRef | None:
    """CPDF_CrossRefTable::MergeUp."""
    if current is None:
        return top
    if top is None:
        return current
    current.update(top)
    return current


# PDFDocEncoding differs from Latin-1 in 0x18-0x1F and 0x80-0x9F
_PDFDOC = {0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙", 0x1C: "˝", 0x1D: "˛",
           0x1E: "˚", 0x1F: "˜", 0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…",
           0x84: "—", 0x85: "–", 0x86: "ƒ", 0x87: "⁄", 0x88: "‹", 0x89: "›",
           0x8A: "−", 0x8B: "‰", 0x8C: "„", 0x8D: "“", 0x8E: "”", 0x8F: "‘",
           0x90: "’", 0x91: "‚", 0x92: "™", 0x93: "ﬁ", 0x94: "ﬂ", 0x95: "Ł",
           0x96: "Œ", 0x97: "Š", 0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı", 0x9B: "ł",
           0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€"}


def text_string(value) -> str:
    """A PDF text string: UTF-16BE with a BOM, UTF-8 with a BOM, else PDFDocEncoding."""
    if isinstance(value, str):
        return str(value)
    if not isinstance(value, bytes):
        return ""
    if value[:2] == codecs.BOM_UTF16_BE:
        return value[2:].decode("utf-16-be", "replace")
    if value[:2] == codecs.BOM_UTF16_LE:
        return value[2:].decode("utf-16-le", "replace")
    if value[:3] == codecs.BOM_UTF8:
        return value[3:].decode("utf-8", "replace")
    return "".join(_PDFDOC.get(b, chr(b)) for b in value)


class PdfFile:
    def __init__(self, data: bytes):
        # GetHeaderOffset: "%PDF" at most 1024 bytes in; every position counts from there
        header = data.find(b"%PDF", 0, 1028)
        if header < 0 or len(data) < header + _HEADER_SIZE:
            raise PdfFileError("not a PDF file")
        self.data = data[header:]
        self.xref = _XRef()
        self._pos = 0     # the syntax parser's position between LoadCrossRefTable and LoadTrailer
        self._cache: dict[int, object] = {}
        self._objstm: dict[int, tuple | None] = {}
        self._page_list: list | None = None
        self._parsing: set[int] = set()
        # CPDF_Parser::StartParseInternal
        rebuilt = False
        xref_offset = self._start_xref()
        loaded = False
        if xref_offset >= _HEADER_SIZE:
            with _deep_recursion():
                loaded = self._load_all(xref_offset)
        if not loaded:
            if not self._rebuild():
                raise PdfFileError("no trailer to rebuild the cross-reference table with")
            rebuilt = True
        if self.xref.trailer is None:
            raise PdfFileError("no trailer")
        if self.trailer.get("Encrypt") is not None:
            raise PdfFileError("encrypted PDFs are not supported")
        if not self.catalog or not self._root_ok():
            if rebuilt:
                raise PdfFileError("no document catalog")
            if not self._rebuild():
                raise PdfFileError("no trailer to rebuild the cross-reference table with")
            rebuilt = True
            self._page_list = None
            if not self.catalog:
                raise PdfFileError("no document catalog")
        if not isinstance(self.trailer.get("Root"), Ref) or not self.trailer["Root"].num:
            if not self._rebuild() or not isinstance(self.trailer.get("Root"), Ref):
                raise PdfFileError("no document catalog")
            self._page_list = None
        self.page_count  # noqa: B018 - counted now, as CPDF_Document::LoadPages does

    @property
    def trailer(self) -> dict:
        return self.xref.trailer if self.xref.trailer is not None else {}

    # ------------------------------------------------------------------ cross-reference

    def _start_xref(self) -> int:
        """ParseStartXRef: the number after the last whole word `startxref` ending by the file's
        9th last byte and starting less than 4096 bytes before that (BackwardsSearchToWord); the
        byte after it is only checked when it ends within the file's first 4096 bytes, since
        IsWholeWord compares a position with the search limit. Else 0."""
        data = self.data
        last = len(data) - 9     # the last byte the search reads
        low = max(0, last - 4096 + 1)
        end = last + 1
        while True:
            at = data.rfind(b"startxref", low, end)
            if at < 0:
                return 0
            right = at + 9 <= 4096 and at + 9 < len(data) and data[at + 9] not in _WHITE_OR_DELIM
            left = at > 0 and data[at - 1] not in _WHITE_OR_DELIM
            if not left and not right:
                break
            end = at + 8
        word = _Words(data, at + 9).next()
        if word is None or not word[1] or not word[0]:
            return 0
        offset = _str_to_int(word[0], 0, len(word[0]), 64, True)
        return offset if offset < len(data) else 0

    def _load_all(self, xref_offset: int) -> bool:
        """LoadAllCrossRefTablesAndStreams."""
        is_stream = not self._load_table(xref_offset, skip=True)
        if is_stream:
            if not self._load_stream(xref_offset, main=True)[0]:
                return False
            xref_list, stream_list = [0], [xref_offset]
        else:
            trailer = self._load_trailer()
            if trailer is None:
                return False
            self.xref.trailer = trailer
            size = _direct_int(trailer.get("Size"))
            if 0 < size <= _MAX_XREF_SIZE:
                self.xref.set_size(size)
            xref_list, stream_list = [xref_offset], [_direct_int(trailer.get("XRefStm"))]
        if not self._find_all(xref_offset, xref_list, stream_list):
            return False
        if xref_list[0] > 0:
            if not self._load_table(xref_list[0], skip=False) or not self._verify_xref():
                return False
        for table, stream in zip(xref_list[1:], stream_list[1:]):
            if stream > 0 and not self._load_stream(stream, main=False)[0]:
                return False
            if table > 0 and not self._load_table(table, skip=False):
                return False
        if is_stream:
            self._objstm.clear()
        return True

    def _find_all(self, xref_offset: int, xref_list: list, stream_list: list) -> bool:
        """FindAllCrossReferenceTablesAndStream: the /Prev chain, oldest first."""
        seen = {xref_offset}
        offset = _direct_int(self.trailer.get("Prev"))
        while offset > 0:
            if offset in seen:
                return False
            seen.add(offset)
            ok, prev = self._load_stream(offset, main=False)
            if ok:
                xref_list.insert(0, 0)
                stream_list.insert(0, offset)
                offset = prev
                continue
            self._load_table(offset, skip=True)
            trailer = self._load_trailer()
            if trailer is None:
                return False
            xref_list.insert(0, offset)
            stream_list.insert(0, self._int_for(trailer.get("XRefStm")))
            offset = _direct_int(trailer.get("Prev"))
            self.xref = _merge_up(_XRef(trailer), self.xref)
        return True

    def _int_for(self, value) -> int:
        """GetIntegerFor: a reference is followed, a boolean counts."""
        if isinstance(value, Ref):
            value = self.get(value.num)
        return _integer(value)

    def _load_trailer(self) -> dict | None:
        """LoadTrailer, at the parser's position: the keyword `trailer` and a dictionary."""
        scan = _Words(self.data, self._pos)
        word = scan.next()
        if word is None or word[0] != b"trailer":
            return None
        with _deep_recursion():
            trailer = _body(scan, False, self._held_stream(self.data))
        return trailer if isinstance(trailer, dict) else None

    def _load_table(self, pos: int, skip: bool) -> bool:
        """LoadCrossRefTable / ParseCrossRefTable: `xref`, then subsections of 20-byte entries
        (with `skip`, only stepped over). The parser is left where the table ends."""
        scan = _Words(self.data, min(pos, len(self.data)))
        objects: list[tuple[int, int, int]] = []
        try:
            if not self._parse_table(scan, skip, objects):
                return False
        finally:
            self._pos = scan.pos
        for num, gen, offset in objects:
            self.xref.add_normal(num, gen, offset)
        return True

    def _parse_table(self, scan: _Words, skip: bool, objects: list) -> bool:
        data = self.data
        word = scan.next()
        if word is None or word[0] != b"xref":
            return False
        count_so_far = 0
        while True:
            saved = scan.pos
            word = scan.next()
            if word is None or not word[0]:
                return False
            if not word[1]:
                scan.pos = saved
                break
            start = _atoui(word[0])
            if start > _MAX_OBJECT_NUMBER:
                return False
            word = scan.next()
            count = _atoui(word[0]) if word is not None and word[1] else 0
            at = _GAP.match(data, scan.pos).end()    # ToNextWord
            if not count:
                scan.pos = at
                continue
            if skip:
                scan.pos = min(at + count * 20, len(data))
                continue
            count_so_far += count
            if count_so_far > _MAX_XREF_SIZE or count_so_far > len(data) // 20:
                return False
            for block in range(0, count, 1024):
                n = min(1024, count - block)
                end = at + n * 20
                if end > len(data):    # ReadBlock
                    return False
                for i in range(n):
                    e = at + i * 20
                    if data[e + 17] == 0x66:     # 'f': free, generation 0, which merges as nothing
                        continue
                    offset = _str_to_int(data, e, end, 64, True)
                    if offset == 0 and not all(0x30 <= c <= 0x39 for c in data[e:e + 10]):
                        return False
                    g = e + 11     # StringToInt's ParseLeadingChars: spaces and signs, but a '-' stays
                    while g < end and data[g] in b" +-":
                        g += 1
                    if g > e + 11 and data[g - 1] == 0x2D:
                        g -= 1
                    gen = _str_to_int(data, g, end, 32, True)
                    objects.append((start + block + i, gen & 0xFFFF, offset))
                at = end
                scan.pos = at
        return True

    def _load_stream(self, pos: int, main: bool) -> tuple[bool, int]:
        """LoadCrossRefStream: (loaded, the stream's /Prev)."""
        with _deep_recursion():
            stream, num = self._indirect_at(pos)
        if not isinstance(stream, Stream) or not num:
            return False, pos
        d = stream.dict
        prev = self._int_for(d.get("Prev"))
        if prev < 0:
            return False, pos
        size = self._int_for(d.get("Size"))
        if size < 0 or size > _MAX_XREF_SIZE:
            return False, pos
        table = _XRef(dict(d))
        if main:
            self.xref = table
            table.set_size(size)
        else:
            self.xref = _merge_up(table, self.xref)
        indices = []
        index = self.resolve(d.get("Index"))
        if isinstance(index, list):
            for k in range(len(index) // 2):
                first, count = self.resolve(index[2 * k]), self.resolve(index[2 * k + 1])
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (first, count)):
                    continue
                first, count = _integer(first), _integer(count)
                if first >= 0 and count > 0:
                    indices.append((first, count))
        if not indices:
            indices.append((0, size))
        w = self.resolve(d.get("W"))
        widths = [self._int_for(v) & 0xFFFFFFFF for v in w] if isinstance(w, list) else []
        if len(widths) < 3:
            return False, prev
        total = sum(widths)
        if total > 0xFFFFFFFF:
            return False, prev
        data, _ = decode_filters(stream.raw, d, self.resolve)
        segment = 0
        for first, count in indices:
            seg_end = (segment + count) * total
            if seg_end > 0xFFFFFFFF or seg_end > len(data):
                continue
            if first + count > 0xFFFFFFFF:
                continue
            current = self.xref.last() + 1 if self.xref.info else 0
            new_size = min(first + count, _MAX_XREF_SIZE)
            if new_size > current:
                self.xref.set_size(new_size)
            base = segment * total
            for i in range(count):
                num = first + i
                if num > _MAX_OBJECT_NUMBER:
                    break
                self._stream_entry(data[base + i * total:base + (i + 1) * total], widths, num)
            segment += count
        return True, prev

    def _stream_entry(self, row: bytes, widths: list[int], num: int) -> None:
        """ProcessCrossRefStreamEntry."""
        def var(a: int, b: int) -> int:
            v = 0
            for c in row[a:a + b]:
                v = (v * 256 + c) & 0xFFFFFFFF
            return v
        kind = var(0, widths[0]) if widths[0] else 1
        second = var(widths[0], widths[1])
        third = var(widths[0] + widths[1], widths[2])
        if kind == 0:
            if third <= 0xFFFF:
                self.xref.set_free(num, third)
        elif kind == 1:
            if third <= 0xFFFF:
                self.xref.add_normal(num, third, second)
        elif kind == 2:
            if second <= self.xref.last():
                self.xref.add_compressed(num, second, third)

    def _root_ok(self) -> bool:
        """CPDF_Document::TryInit: a catalog, and at least one page counted."""
        try:
            return bool(self.catalog) and self.page_count > 0
        except Exception:  # noqa: BLE001
            return False

    def _verify_xref(self) -> bool:
        """CPDF_Parser::VerifyCrossRefTable: the first object with a position must start with its
        own number there."""
        for num, entry in self.xref.info.items():
            if entry[2] <= 0:
                continue
            word = _Words(self.data, min(entry[2], len(self.data))).next()
            return word is not None and word[1] and bool(word[0]) and _atoui(word[0]) == num
        return True

    def _rebuild(self) -> bool:
        """CPDF_Parser::RebuildCrossRef: the file read word by word from the start (strings and
        hex strings skipped whole, an unbalanced `(` swallowing the rest), every `n g obj` read
        as an object and stepped over, stream data included (so objects inside a stream that
        parsed are not seen, and those after a broken header's stream data may be lost); each
        `trailer` dictionary and cross-reference stream merged into the trailer in file order.
        The result is merged over what the tables gave (its entries win), and objects already
        parsed stay what they were. The catalog is only what the trailer's /Root refers to:
        PDFium does not go looking for one."""
        data = self.data
        table = _XRef()
        scan = _Words(data, 0)
        numbers: list[tuple[int, int]] = []
        while (w := scan.next()) is not None:
            word, is_number, start = w
            if is_number:
                numbers.append((_atoui(word), start))
                if len(numbers) > 2:
                    numbers.pop(0)
                continue
            if word == b"(":
                scan.pos = _literal_string(data, scan.pos)[1]
            elif word == b"<":
                scan.pos = _hex_body(data, scan.pos)[1]
            elif word == b"trailer":
                t = _body(scan, False, self._rebuild_stream)
                t = t.dict if isinstance(t, Stream) else t
                if isinstance(t, dict):
                    table = _merge_up(table, _XRef(t))
            elif word == b"obj" and len(numbers) == 2:
                (num, pos), (gen, _) = numbers
                obj, scan.pos = self._object_at(pos)
                if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
                    table = _merge_up(table, _XRef(dict(obj.dict)))
                if num <= _MAX_OBJECT_NUMBER:
                    table.add_normal(num, gen & 0xFFFF, pos)
                    members = self._objstm_from(obj, lambda v: v) if isinstance(obj, Stream) else None
                    for i, n in enumerate(members[0] if members else []):
                        if n <= _MAX_OBJECT_NUMBER:
                            table.add_compressed(n, num, i)
            numbers.clear()
        self.xref = _merge_up(self.xref, table)
        return self.xref.trailer is not None and bool(self.xref.info)

    def _object_at(self, pos: int):
        """CPDF_SyntaxParser::GetIndirectObject as the rebuild calls it (no object holder, so
        an indirect /Length is unknown): the object and where reading stopped."""
        scan = _Words(self.data, pos)
        for _ in range(3):
            scan.next()
        obj = _body(scan, True, self._rebuild_stream)
        return (None if obj is _NOTHING else obj), scan.pos

    def _rebuild_stream(self, d: dict, pos: int):
        """CPDF_SyntaxParser::ReadStream with no object holder: (Stream or _NOTHING, position)."""
        return self._read_stream(self.data, d, pos, False)

    def _held_stream(self, data: bytes):
        """ReadStream as an object read through the parser calls it: an indirect /Length counts."""
        return lambda d, pos: self._read_stream(data, d, pos, True)

    def _read_stream(self, data: bytes, d: dict, pos: int, held: bool):
        """CPDF_SyntaxParser::ReadStream: (Stream or _NOTHING, position). /Length must be a
        number, directly or (`held`) through one reference; an object whose /Length refers to
        an object being parsed gets none (CPDF_Parser's m_ParsingObjNums)."""
        obj = d
        start = pos   # ToNextLine
        while start < len(data) and data[start] not in b"\r\n":
            start += 1
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif start < len(data):
            start += 1
        length = obj.get("Length")
        if isinstance(length, Ref):
            length = self.get(length.num) if held and length.num not in self._parsing else None
        length = _integer(length) if isinstance(length, (int, float)) and not isinstance(length, bool) else -1
        if length > 0 and start + length >= len(data):
            length = -1
        if length >= 0:
            # a zero length is checked too; the word only has to begin with endstream (a memcmp)
            after = _Words(data, start + length).next()
            if after is None or after[0][:9] != b"endstream":
                length = -1
        if length >= 0:
            end = start + length
        else:
            ends = [e for e in (data.find(b"endstream", start), data.find(b"endobj", start)) if e >= 0]
            if not ends:
                return _NOTHING, start
            end = min(ends)
            if data[end - 2:end] == b"\r\n" and end - 2 >= start:
                end -= 2
            elif data[end - 1:end] in (b"\r", b"\n") and end - 1 >= start:
                end -= 1
        after = _Words(data, end)
        word = after.next()
        stop = end if word is None or word[0] == b"endobj" else after.pos
        return Stream(obj, data[start:end]), stop

    # ------------------------------------------------------------------ objects

    def _indirect_at(self, pos: int):
        """CPDF_SyntaxParser::GetIndirectObject (loose, with the document as holder): the
        object after `n g obj` at pos with its number, or (None, None)."""
        scan = _Words(self.data, pos)
        words = [scan.next() for _ in range(3)]
        if None in words or not words[0][1] or not words[1][1] or words[2][0] != b"obj":
            return None, None
        obj = _body(scan, False, self._held_stream(self.data))
        return (None if obj is _NOTHING else obj), _atoui(words[0][0])

    def get(self, num: int):
        if num in self._cache:
            return self._cache[num]
        if num in self._parsing:
            return None
        # CPDF_Parser::ParseIndirectObject
        entry = self.xref.info.get(num)
        obj = None
        self._parsing.add(num)
        try:
            with _deep_recursion():
                if entry and entry[0] == "normal" and entry[2] > 0:
                    obj, found = self._indirect_at(entry[2])
                    if found != num:   # CPDF_Parser::ParseIndirectObjectAt: another object is none
                        obj = None
                elif entry and entry[0] == "compressed":
                    archive, index = entry[2] & 0xFFFFFFFF, (entry[2] >> 32) & 0xFFFFFFFF
                    objstm = self._object_stream(archive)
                    if objstm is not None and index < len(objstm[0]) and objstm[0][index] == num:
                        nums, data, first, offsets = objstm
                        at = first + offsets[index]
                        if at < len(data):
                            obj = _body(_Words(data, at), False, self._held_stream(data))
                            obj = None if obj is _NOTHING else obj
        finally:
            self._parsing.discard(num)
        if obj is not None:   # the holder keeps what parsed; a miss is asked again next time
            self._cache[num] = obj
        return obj

    def _object_stream(self, num: int):
        """CPDF_Parser::GetObjectStream: the archive must be known as one, and is parsed at its
        position (a union: whatever the entry holds), once, a failure included."""
        if num in self._parsing:
            return None
        if num in self._objstm:
            return self._objstm[num]
        entry = self.xref.info.get(num)
        if entry is None or not entry[3] or entry[2] <= 0:
            return None
        self._parsing.add(num)
        try:
            stream, found = self._indirect_at(entry[2])
            if found != num:
                stream = None
            self._objstm[num] = self._objstm_from(stream, self.resolve) if isinstance(stream, Stream) else None
        finally:
            self._parsing.discard(num)
        return self._objstm[num]

    def _objstm_from(self, stream: Stream, resolve) -> tuple[list, bytes, int, list] | None:
        """CPDF_ObjectStream::Create: (numbers, decoded data, /First, offsets), or None when the
        dictionary doesn't say /Type /ObjStm with integers /N (0 to the maximum object number)
        and /First (not negative). Up to N pairs are read with GetDirectNum (a word that is no
        number reads as 0), a pair numbered 0 is skipped, and reading stops at the data's end."""
        d = stream.dict
        n, first = resolve(d.get("N")), resolve(d.get("First"))
        if (d.get("Type") != "ObjStm" or not isinstance(d.get("Type"), Name)
                or not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= _MAX_OBJECT_NUMBER
                or not isinstance(first, int) or isinstance(first, bool) or first < 0):
            return None
        data, _ = decode_filters(stream.raw, d, resolve)
        scan = _Words(data, 0)

        def direct_num() -> int:
            w = scan.next()
            return _atoui(w[0]) if w is not None and w[1] else 0

        nums, offsets = [], []
        for _ in range(n):
            if scan.pos >= len(data):
                break
            a, b = direct_num(), direct_num()
            if a:
                nums.append(a)
                offsets.append(b)
        return nums, data, first, offsets

    def resolve(self, value, depth: int = 0):
        while isinstance(value, Ref) and depth < 32:
            value = self.get(value.num)
            depth += 1
        return value

    def stream_data(self, stream: Stream) -> bytes:
        """A stream's bytes decoded up to an image codec (whose data stays encoded)."""
        if stream._decoded is None:
            try:
                stream._decoded = decode_filters(stream.raw, {k: self.resolve(v) for k, v in stream.dict.items()},
                                                 self.resolve)[0]
            except Exception:  # noqa: BLE001 - an unknown or broken filter: no data
                stream._decoded = b""
        return stream._decoded

    # ------------------------------------------------------------------ document structure

    @property
    def catalog(self) -> dict:
        """GetRoot: what the trailer's /Root refers to (a direct dictionary does not count)."""
        root = self.trailer.get("Root")
        return (_dict(self.resolve(root)) or {}) if isinstance(root, Ref) else {}

    # The page tree as CPDF_Document reads it. The page count is the root's /Count when it is
    # sane (RetrievePageCount, CountPages), whatever the kids say; a page is found by a traversal
    # that keeps its place between calls (GetMutablePageDictionary, TraversePDFPages), where a kid
    # that is no dictionary uses up a page, a node with no /Kids array is a page, and a leaf
    # found remembers its object number (m_PageList). So a /Count larger than the tree gives
    # pages that don't load, one smaller hides the rest, and which pages load can depend on the
    # order they are asked for. Inherited attributes come from the /Parent chain (GetPageAttr).

    def _pages_node(self) -> tuple[int, dict | None]:
        pages = self.catalog.get("Pages")
        return (pages.num if isinstance(pages, Ref) else 0), _dict(self.resolve(pages))

    def _count_pages(self) -> int:
        _, pages = self._pages_node()
        if pages is None:
            return 0
        if "Kids" not in pages:
            return 1
        with _deep_recursion():
            count = self._count_node(pages, [pages])
        return 0 if count is None else count

    @staticmethod
    def _is_branch(node: dict) -> bool:
        """GetNodeType: /Type /Pages or /Page decides; anything else (a reference included, since
        GetNameFor reads only a direct name) is guessed from /Kids and written in, as PDFium
        fixes its in-memory copy."""
        kind = node.get("Type")
        if isinstance(kind, Name) and kind in ("Pages", "Page"):
            return kind == "Pages"
        branch = "Kids" in node
        node[Name("Type")] = Name("Pages" if branch else "Page")
        return branch

    def _count_node(self, node: dict, visited: list) -> int | None:
        """CountPages: None when the tree holds too many pages."""
        count = _integer(self.resolve(node.get("Count")))
        if 0 < count < _PAGE_MAX:
            return count
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            return 0
        count = 0
        for kid in kids:
            kid = _dict(self.resolve(kid))
            if kid is None or any(kid is v for v in visited):
                continue
            if self._is_branch(kid):
                visited.append(kid)
                inner = self._count_node(kid, visited)
                visited.pop()
                if inner is None:
                    return None
                count += inner
            else:
                count += 1
            if count >= _PAGE_MAX:
                return None
        node[Name("Count")] = count  # CountPages writes it back
        return count

    @property
    def page_count(self) -> int:
        if self._page_list is None:
            self._page_list = [None] * self._count_pages()
            self._traversal: list[list] = []   # [object number, node, next kid] per level
            self._next_page = 0
            self._max_level = False
        return len(self._page_list)

    def page_dict(self, index: int) -> tuple[int | None, dict] | None:
        """(object number, page dictionary) of page `index`, or None where PDFium finds none.
        The number is None for a page written directly in /Kids, 0 when it has none."""
        if not 0 <= index < self.page_count:
            return None
        known = self._page_list[index]
        if known is not None and known[0] != 0:
            if known[0] is None:
                return known
            obj = self.get(known[0])
            if isinstance(obj, dict):
                return known[0], obj
        num, pages = self._pages_node()
        if pages is None:
            return None
        if not self._traversal:
            self._traversal = [[num, pages, 0]]
            self._next_page = 0
            self._max_level = False
        togo = [index - self._next_page + 1]
        with _deep_recursion():
            page = self._traverse(index, togo, 0)
        self._next_page = index + 1
        return page

    def _kid(self, value) -> tuple[int | None, dict | None]:
        """CPDF_Array::ConvertToIndirectObjectAt, then GetMutableDictAt."""
        if isinstance(value, Ref):
            obj = self.get(value.num)
            if isinstance(obj, Stream):
                return 0, obj.dict   # the stream's dictionary has no object number of its own
            return value.num, obj if isinstance(obj, dict) else None
        return None, _dict(value)

    def _traverse(self, index: int, togo: list, level: int):
        if togo[0] < 0 or self._max_level:
            return None
        entry = self._traversal[level]
        node = entry[1]
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            self._traversal.pop()
            if togo[0] != 1 or self._is_branch(node):
                return None
            self._page_list[index] = (entry[0], node)
            return entry[0], node
        if level >= _MAX_PAGE_LEVEL:
            self._traversal.pop()
            self._max_level = True
            return None
        page = None
        for k in range(entry[2], len(kids)):
            if togo[0] == 0:
                break
            num, kid = self._kid(kids[k])
            if kid is None:
                togo[0] -= 1
                entry[2] += 1
                continue
            if kid is node:
                entry[2] += 1
                continue
            if "Kids" not in kid:
                self._page_list[index - togo[0] + 1] = (num, kid)
                togo[0] -= 1
                entry[2] += 1
                if togo[0] == 0:
                    page = (num, kid)
                    break
            else:
                if len(self._traversal) == level + 1:
                    self._traversal.append([num, kid, 0])
                found = self._traverse(index, togo, level + 1)
                if len(self._traversal) == level + 1:
                    entry[2] += 1
                if len(self._traversal) != level + 1 or togo[0] == 0 or self._max_level:
                    page = found
                    break
        if entry[2] == len(kids):
            self._traversal.pop()
        return page

    def page_attr(self, node: dict, key: str):
        """CPDF_Page::GetPageAttr: the page's own value, else its parents'."""
        seen: list = []
        while node is not None and not any(node is v for v in seen):
            if key in node:
                value = self.resolve(node[key])
                if value is not None:
                    return value
            seen.append(node)
            node = _dict(self.resolve(node.get("Parent")))
        return None

    def inherited_page(self, index: int) -> tuple[Ref | None, dict] | None:
        """(reference, page dictionary with the inherited attributes written in), or None."""
        found = self.page_dict(index)
        if found is None:
            return None
        num, node = found
        page = dict(node)
        for k in _INHERITED:
            value = self.page_attr(node, k)
            if value is None:
                page.pop(k, None)
            else:
                page[Name(k)] = value
        return (Ref(num, 0) if num else None), page

    def page_index(self, ref) -> int:
        """CPDF_Document::GetPageIndex for a reference; a number is the page number."""
        if isinstance(ref, int) and not isinstance(ref, bool):  # a page number (remote-style destinations)
            return ref if 0 <= ref < self.page_count else -1
        if not isinstance(ref, Ref):
            return -1
        objnum = ref.num
        count = self.page_count
        skip, skipped = 0, False
        for i, known in enumerate(self._page_list):
            num = known[0] if known is not None else 0
            if num == objnum:
                return i
            if not skipped and not num:
                skip, skipped = i, True
        num, pages = self._pages_node()
        if pages is None:
            return -1
        state = [skip, 0]
        with _deep_recursion():
            found = self._find_page_index(num, pages, state, objnum, 0)
        if not 0 <= found < count:
            return -1
        obj = self.get(objnum)
        if isinstance(obj, dict) and self.resolve(obj.get("Type")) == "Page":
            self._page_list[found] = (objnum, obj)
        return found

    def _find_page_index(self, num: int, node: dict, state: list, objnum: int, level: int) -> int:
        """CPDF_Document::FindPageIndex; state = [pages still to skip, index so far]."""
        if "Kids" not in node:
            if objnum == num:
                return state[1]
            if state[0]:
                state[0] -= 1
            state[1] += 1
            return -1
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list) or level >= _MAX_PAGE_LEVEL:
            return -1
        count = _integer(self.resolve(node.get("Count"))) & 0xFFFFFFFFFFFFFFFF  # a size_t
        if count <= state[0]:
            state[0] -= count
            state[1] += count
            return -1
        if count and count == len(kids):
            for i, kid in enumerate(kids):
                if isinstance(kid, Ref) and kid.num == objnum:
                    return state[1] + i
        for kid in kids:
            kid_num, kid = self._kid(kid) if isinstance(kid, Ref) else (0, _dict(kid))
            if kid is None or kid is node:
                continue
            found = self._find_page_index(kid_num, kid, state, objnum, level + 1)
            if found >= 0:
                return found
        return -1

    def info(self) -> dict:
        info = self.resolve(self.trailer.get("Info"))
        return info if isinstance(info, dict) else {}

    def _number_tree(self, node, out: list, depth: int = 0) -> None:
        node = self.resolve(node)
        if not isinstance(node, dict) or depth > 32:
            return
        nums = self.resolve(node.get("Nums"))
        if isinstance(nums, list):
            for k in range(0, len(nums) - 1, 2):
                if isinstance(nums[k], int):
                    out.append((nums[k], self.resolve(nums[k + 1])))
        for kid in self.resolve(node.get("Kids")) or []:
            self._number_tree(kid, out, depth + 1)

    def name_tree(self, node, out: list, depth: int = 0) -> None:
        node = self.resolve(node)
        if not isinstance(node, dict) or depth > 32:
            return
        names = self.resolve(node.get("Names"))
        if isinstance(names, list):
            for k in range(0, len(names) - 1, 2):
                out.append((names[k], names[k + 1]))
        for kid in self.resolve(node.get("Kids")) or []:
            self.name_tree(kid, out, depth + 1)

    def page_labels(self) -> list[str]:
        entries: list = []
        self._number_tree(self.catalog.get("PageLabels"), entries)
        if not entries:
            return [""] * self.page_count
        entries.sort(key=lambda e: e[0])
        out = []
        for i in range(self.page_count):
            start, spec = None, None
            for s, d in entries:
                if s <= i:
                    start, spec = s, d
            if spec is None or not isinstance(spec, dict):
                out.append("")
                continue
            n = i - start + (spec.get("St", 1) if isinstance(spec.get("St"), int) else 1)
            style = spec.get("S")
            prefix = text_string(self.resolve(spec.get("P"))) if spec.get("P") is not None else ""
            out.append(prefix + _label_number(n, style))
        return out

    def dest_page(self, dest, depth: int = 0) -> int:
        dest = self.resolve(dest)
        if isinstance(dest, dict):
            dest = self.resolve(dest.get("D"))
        if isinstance(dest, (String, Name, bytes, str)) and depth < 4:
            return self.dest_page(self.named_dest(dest), depth + 1)
        if isinstance(dest, list) and dest:
            return self.page_index(dest[0])
        return -1

    def named_dest(self, name):
        if not hasattr(self, "_dest_map"):
            self._dest_map = {}
            for key, value in self.named_dests_raw():
                self._dest_map.setdefault(bytes(key) if isinstance(key, bytes) else str(key).encode("latin-1"), value)
        key = bytes(name) if isinstance(name, bytes) else str(name).encode("latin-1", "replace")
        return self._dest_map.get(key)

    def named_dests_raw(self) -> list[tuple]:
        """(name, destination) from the Dests name tree, then from the catalog's Dests dictionary."""
        out: list = []
        names = self.resolve(self.catalog.get("Names"))
        if isinstance(names, dict):
            self.name_tree(names.get("Dests"), out)
        dests = self.resolve(self.catalog.get("Dests"))
        if isinstance(dests, dict):
            out += [(Name(k), v) for k, v in dests.items()]
        return out


_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def _label_number(n: int, style) -> str:
    if style == "D":
        return str(n)
    if style in ("R", "r"):
        out = ""
        for v, s in _ROMAN:
            while n >= v:
                out += s
                n -= v
        return out.upper() if style == "R" else out
    if style in ("A", "a"):
        if n < 1:
            return ""
        letter = chr(ord("A") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return letter if style == "A" else letter.lower()
    return ""


# ---------------------------------------------------------------------- writing


def _write_name(n: str) -> bytes:
    out = bytearray(b"/")
    for b in n.encode("utf-8"):
        if b < 0x21 or b > 0x7E or b in b"()<>[]{}/%#":
            out += b"#%02X" % b
        else:
            out.append(b)
    return bytes(out)


def _write_number(v) -> bytes:
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return str(v).encode()
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-", "-0") else "0").encode()


def serialize(v, keep) -> bytes:
    """An object in file syntax; references pass through `keep` (None drops them to null)."""
    if v is None:
        return b"null"
    if isinstance(v, (bool, int, float)):
        return _write_number(v)
    if isinstance(v, Name):
        return _write_name(v)
    if isinstance(v, Ref):
        return b"%d %d R" % (v.num, v.gen) if keep(v) else b"null"
    if isinstance(v, bytes):
        return b"<" + bytes(v).hex().encode() + b">"
    if isinstance(v, str):
        return _write_name(v)
    if isinstance(v, list):
        return b"[" + b" ".join(serialize(x, keep) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b"".join(_write_name(k) + b" " + serialize(x, keep) + b"\n" for k, x in v.items()) + b">>"
    return b"null"


def write_file(pdf: PdfFile, keep_pages: list[int], boxes: dict[int, tuple]) -> bytes:
    """A new file with the pages `keep_pages` (in document order), media and crop box replaced
    for those in `boxes` (PDF user space rectangles). Every other object keeps its number; a
    reference to a page that was left out becomes null."""
    pages = [pdf.inherited_page(i) for i in range(pdf.page_count)]
    kept = sorted(i for i in set(keep_pages) if pages[i] is not None)
    page_refs = {p[0].num for p in pages if p is not None and p[0] is not None}
    kept_refs = {pages[i][0].num for i in kept if pages[i][0] is not None}
    pages_num = max(pdf.xref.info or {0: None}) + 1
    objects: dict[int, object] = {}

    def keep(ref: Ref) -> bool:
        return ref.num == pages_num or ref.num not in page_refs or ref.num in kept_refs

    # the new page dictionaries: inherited attributes written out, one parent
    new_pages: dict[int, dict] = {}
    extra = pages_num + 1
    kids = []
    for i in kept:
        ref, page = pages[i]
        d = {k: v for k, v in page.items() if k != "Parent"}
        d["Parent"] = Ref(pages_num, 0)
        if i in boxes:
            d["MediaBox"] = list(boxes[i])
            d["CropBox"] = list(boxes[i])
        if ref is None:
            ref = Ref(extra, 0)
            extra += 1
        new_pages[ref.num] = d
        kids.append(ref)
    objects[pages_num] = {Name("Type"): Name("Pages"), Name("Kids"): kids, Name("Count"): len(kids)}
    catalog = dict(pdf.catalog)
    catalog[Name("Pages")] = Ref(pages_num, 0)
    root_ref = pdf.trailer.get("Root")
    root_num = root_ref.num if isinstance(root_ref, Ref) else extra
    if not isinstance(root_ref, Ref):
        extra += 1
    objects[root_num] = catalog
    objects.update(new_pages)

    # everything reachable from the catalog and the info dictionary
    todo = [catalog, pdf.trailer.get("Info")] + list(new_pages.values())
    while todo:
        v = todo.pop()
        if isinstance(v, Ref):
            if v.num in objects or not keep(v):
                continue
            obj = pdf.get(v.num)
            if v.num in page_refs:
                continue
            objects[v.num] = obj
            todo.append(obj.dict if isinstance(obj, Stream) else obj)
        elif isinstance(v, dict):
            todo.extend(x for k, x in v.items() if not (k == "Parent" and v.get("Type") == "Page"))
        elif isinstance(v, list):
            todo.extend(v)
        elif isinstance(v, Stream):
            todo.append(v.dict)

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        obj = objects[num]
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num
        if isinstance(obj, Stream):
            d = dict(obj.dict)
            d[Name("Length")] = len(obj.raw)
            out += serialize(d, keep) + b"\nstream\n" + obj.raw + b"\nendstream"
        else:
            out += serialize(obj, keep)
        out += b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[num]) if num in offsets else b"0000000000 65535 f \n"
    trailer = {Name("Size"): size, Name("Root"): Ref(root_num, 0)}
    if isinstance(pdf.trailer.get("Info"), Ref) and pdf.trailer["Info"].num in objects:
        trailer[Name("Info")] = pdf.trailer["Info"]
    out += b"trailer\n" + serialize(trailer, lambda r: True) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def read(source) -> PdfFile:
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    try:
        return PdfFile(bytes(data))
    except (PdfFileError, PdfSyntaxError):
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfFileError(f"unreadable PDF: {e}") from e


__all__ = ["PdfFile", "PdfFileError", "read", "text_string", "write_file", "serialize", "END"]
