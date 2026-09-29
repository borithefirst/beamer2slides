"""The file layer: cross-reference tables and streams, object streams, the page tree, text strings,
and writing a new file (ISO 32000-1, 7.5, 7.7). Links, destinations, page labels and the document
information are in navigation.py."""

from __future__ import annotations

import contextlib
import enum
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Literal, Mapping, Union

from . import navigation
from .filters import decode as decode_filters
from .navigation import NOTHING, Nothing, array_for, dict_at, integer_for, name_for, pdf_decode_text, text
from .syntax import (END, Name, PdfArray, PdfDict, PdfObject, PdfSyntaxError, Ref, Stream, String, _literal_string,
                     _name, _number)

_WHITE_OR_DELIM = frozenset(b"\x00\t\n\x0c\r ()<>[]{}/%")
_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")
_PAGE_MAX = 0xFFFFF        # CPDF_Document::kPageMaxNum
_MAX_PAGE_LEVEL = 1024     # kMaxPageLevel
_MAX_OBJECT_NUMBER = 24 * 1024 * 1024  # CPDF_Parser::kMaxObjectNumber

Box = tuple[float, float, float, float]


class PdfFileError(Exception):
    pass


def _dict(value: PdfObject) -> PdfDict | None:
    """GetDict: a dictionary, or a stream's dictionary."""
    if isinstance(value, Stream):
        return value.dict
    return value if isinstance(value, dict) else None


def _integer(value: PdfObject) -> int:
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
# the gap (1), then one word: a name (2), a delimiter (3), a regular word of number characters
# only (4) or any other regular word (5). The gap is atomic, so no word comes out of a comment:
# `(?=(gap))\1` rather than `(?>gap)`, which Python has only since 3.11 (and the package is
# imported on 3.10 by Google's monorepo; the same tokens, 5% slower on the tokenizer alone)
_WORD = re.compile(rb"(?=((?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)*))\1(?:"
                   rb"(/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)|(<<|>>|[()<>\[\]{}])"
                   rb"|([0-9+.\-]+)(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
                   rb"|([^\x00\t\n\x0c\r ()<>\[\]{}/%]+))")


Word = tuple[bytes, bool, int]   # (word, is_number, start)
TableEntry = tuple[int, int, Union[int, None]]   # (number, generation, offset or None when free)


def _gap_end(data: bytes, pos: int) -> int:
    """Where the whitespace and comments from `pos` end (_GAP matches everywhere, maybe nothing)."""
    m = _GAP.match(data, pos)
    return pos if m is None else m.end()


class _Words:
    """CPDF_SyntaxParser::GetNextWord over file syntax: (word, is_number, start) or None."""

    def __init__(self, data: bytes, pos: int) -> None:
        self.data, self.pos, self.last = data, pos, b""

    def next(self) -> Word | None:
        m = _WORD.match(self.data, self.pos)
        if m is None:   # only a gap left
            self.pos = _gap_end(self.data, self.pos)
            return None
        g = m.lastindex
        if g is None:   # a match is always one of the words
            raise PdfSyntaxError("no word")
        word = m.group(g)
        end = self.pos = m.end()
        if len(word) > 256:
            word = word[:256]   # the word buffer holds 256 bytes
        self.last = word    # m_WordBuffer: kept when a position is restored, and at the end
        return word, g == 4, end - len(word)


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


class _Nullptr(enum.Enum):
    """GetObjectBodyInternal's nullptr: no object read."""
    NOTHING = "nothing"


_NOTHING = _Nullptr.NOTHING
_HEX = frozenset(b"0123456789abcdefABCDEF")
_HEADER_SIZE = 9            # kPDFHeaderSize
_MAX_XREF_SIZE = _MAX_OBJECT_NUMBER + 1    # kMaxXRefSize
_ENTRY_SIZE = 20            # a cross-reference table entry, read blind

Body = Union[PdfObject, _Nullptr]
# ReadStream after a dictionary: (the stream, or nothing, and where reading stopped)
StreamAt = Callable[[PdfDict, int], "tuple[Stream | _Nullptr, int]"]


def _str_to_int(data: bytes | bytearray, at: int, bits: int, signed: bool) -> int:
    """FXSYS_StrToInt over a NUL-terminated buffer from `at`: a sign, then digits up to the NUL or
    anything else; past the type's range its maximum (its minimum when negative and signed), a
    negative unsigned value wrapped."""
    top = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    n = len(data)
    neg = at < n and data[at] == 0x2D
    if at < n and data[at] in (0x2B, 0x2D):
        at += 1
    num = 0
    while at < n and 0x30 <= data[at] <= 0x39:
        val = data[at] - 0x30
        if num > (top - val) // 10:
            return -top - 1 if neg and signed else top
        num = num * 10 + val
        at += 1
    if not neg:
        return num
    return -num if signed else (-num) & top


def _string_to_int(view: bytes | bytearray) -> int:
    """StringToInt (int32): leading spaces, plus and minus signs skipped but the minus right in
    front of the digits, then FXSYS_StrToInt's rule over the whole view."""
    start = 0
    while start < len(view) and view[start] in (0x20, 0x2B, 0x2D):
        start += 1
    if start and view[start - 1] == 0x2D:
        start -= 1
    return _str_to_int(bytes(view[start:]), 0, 32, True)


def _var_int(field: bytes) -> int:
    """GetVarInt: big-endian, in a uint32 (longer fields wrap)."""
    num = 0
    for b in field:
        num = ((num << 8) | b) & 0xFFFFFFFF
    return num


def _direct_integer(d: PdfDict | None, key: str) -> int:
    """GetDirectIntegerFor: a number written in the dictionary itself, else 0."""
    v = d.get(key) if d is not None else None
    return _integer(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


# CPDF_CrossRefTable

EntryKind = Literal["n", "c", "free"]   # normal, compressed (in an object stream), free


@dataclass(slots=True, kw_only=True)
class ObjectInfo:
    """CPDF_CrossRefTable::ObjectInfo, changed in place as tables are read. A compressed object's
    position holds its object stream's number in the low 32 bits and its index above."""
    kind: EntryKind
    gen: int
    pos: int
    is_objstm: bool


@dataclass(frozen=True, kw_only=True)
class CrossRef:
    """CPDF_CrossRefTable: the objects by number, the trailer (None before one is read) and the
    trailer's own object number (a cross-reference stream's)."""
    objects: dict[int, ObjectInfo]
    trailer: PdfDict | None
    number: int


def _no_table() -> CrossRef:
    return CrossRef(objects={}, trailer=None, number=0)


def _info(objects: dict[int, ObjectInfo], num: int) -> ObjectInfo:
    info = objects.get(num)
    if info is None:
        info = objects[num] = ObjectInfo(kind="free", gen=0, pos=0, is_objstm=False)
    return info


def _add_normal(objects: dict[int, ObjectInfo], num: int, gen: int, is_objstm: bool, pos: int) -> None:
    info = _info(objects, num)
    if info.gen > gen:
        return
    info.kind, info.gen, info.pos = "n", gen, pos
    info.is_objstm = info.is_objstm or is_objstm


def _add_compressed(objects: dict[int, ObjectInfo], num: int, archive: int, index: int) -> None:
    info = _info(objects, num)
    if info.gen > 0 or info.is_objstm:   # a newer generation, or a known object stream
        return
    info.kind, info.gen, info.pos = "c", 0, archive | index << 32
    _info(objects, archive).is_objstm = True


def _set_free(objects: dict[int, ObjectInfo], num: int, gen: int) -> None:
    info = _info(objects, num)
    info.kind, info.gen, info.pos = "free", gen, 0


def _set_size(objects: dict[int, ObjectInfo], size: int) -> None:
    """SetObjectMapSize: the numbers from `size` on dropped, `size - 1` always present."""
    if size == 0:
        objects.clear()
        return
    for num in [n for n in objects if n >= size]:
        del objects[num]
    _info(objects, size - 1)


def _merge_up(current: CrossRef, top: CrossRef) -> CrossRef:
    """CPDF_CrossRefTable::MergeUp: `top`'s entries over `current`'s (an object stream stays
    one), `top`'s trailer keys over `current`'s but for /XRefStm and /Prev, which stay
    `current`'s; the trailer's object number stays `current`'s."""
    objects = dict(current.objects)
    for num, info in top.objects.items():
        old = objects.get(num)
        objects[num] = ObjectInfo(kind=info.kind, gen=info.gen, pos=info.pos,
                                  is_objstm=info.is_objstm or (old is not None and info.kind == "n"
                                                               and old.kind == "n" and old.is_objstm))
    trailer = current.trailer
    if top.trailer is not None:
        if trailer is None:
            trailer = top.trailer
        else:
            trailer = dict(trailer)
            keep = {k: trailer.pop(k) for k in ("XRefStm", "Prev") if k in trailer}
            trailer.update({k: v for k, v in top.trailer.items() if k not in ("XRefStm", "Prev")})
            trailer.update(keep)
    return CrossRef(objects=dict(sorted(objects.items())), trailer=trailer, number=current.number)


@dataclass(frozen=True, kw_only=True)
class ObjectStream:
    """CPDF_ObjectStream: its members (number, offset from /First), its decoded data, /First."""
    members: list[tuple[int, int]]
    data: bytes
    first: int


@dataclass(slots=True, kw_only=True)
class _Level:
    """One level of the page tree traversal kept between calls: the node's object number, the
    node, the next kid to look at."""
    num: int | None
    node: PdfDict
    next: int


@dataclass(slots=True, kw_only=True)
class _Search:
    """FindPageIndex's state: pages still to skip, the index so far, and the pages written
    directly in /Kids that were loaded (by id: they have a number now, not 0)."""
    skip: int
    index: int
    loaded: set[int]


@dataclass(slots=True, kw_only=True)
class _Countdown:
    """TraversePDFPages' pages to go (shared by every level of one call)."""
    left: int


# (object number, page dictionary); the number is None for a page written directly in /Kids
KnownPage = tuple[Union[int, None], PdfDict]


def _hex_body(data: bytes, pos: int) -> tuple[bytes, int]:
    """CPDF_SyntaxParser::ReadHexString: hex digits up to `>` (others skipped)."""
    end = data.find(b">", pos)
    end = len(data) if end < 0 else end
    digits = bytes(c for c in data[pos:end] if c in _HEX)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii")), min(end + 1, len(data))


def _body(scan: _Words, strict: bool, stream_at: StreamAt, depth: int) -> Body:
    """CPDF_SyntaxParser::GetObjectBodyInternal: one object, or _NOTHING (nullptr). Strict only
    at the top: an array must end on `]`, and a dictionary value that is no object ends it.
    `stream_at(dict, pos)` reads the stream after a dictionary: (Stream or _NOTHING, position).
    `depth` is 0 at the top."""
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
        return String(value)
    if word == b"[":
        out: PdfArray = []
        while (item := _body(scan, False, stream_at, depth + 1)) is not _NOTHING:
            out.append(item)
        return out if not strict or scan.last[:1] == b"]" else _NOTHING   # the word that ended it
    if word[:1] == b"/":
        return _name(word)
    if word == b"<<":
        d: PdfDict = {}
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
def _deep_recursion() -> Generator[None, None, None]:
    """The page tree may be 1024 levels deep, as deep as PDFium follows it."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 4 * _MAX_PAGE_LEVEL + 200))
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


def text_string(value: PdfObject) -> str:
    """A PDF text string (or a name) as PDF_DecodeText reads it: UTF-16 with either byte order
    mark, UTF-8 with its mark, else PDFDocEncoding; a lone surrogate becomes U+FFFD."""
    if isinstance(value, str):
        return text(pdf_decode_text(str.__str__(value).encode("latin-1", "replace")))
    if not isinstance(value, bytes):
        return ""
    return text(pdf_decode_text(value))


class PdfFile:
    def __init__(self, data: bytes) -> None:
        # GetHeaderOffset: "%PDF" at most 1024 bytes in; every position counts from there
        header = data.find(b"%PDF", 0, 1028)
        if header < 0 or len(data) < header + _HEADER_SIZE:
            raise PdfFileError("not a PDF file")
        self.data = data[header:]
        self._table: CrossRef = _no_table()
        self._cache: dict[int, PdfObject] = {}
        self._objstm: dict[int, ObjectStream | None] = {}   # CPDF_Parser::object_stream_map_
        self._page_list: list[KnownPage | None] | None = None
        self._traversal: list[_Level] = []   # the page tree traversal's levels, kept between calls
        self._next_page = 0
        self._max_level = False
        self._parsing: set[int] = set()
        self._nulls: set[int] = set()          # objects whose body is `null` (a CPDF_Null)
        self._objnums: dict[int, int] = {}     # id(indirect dict/array/stream) -> its number
        # CPDF_Parser::StartParseInternal: the tables the file says it has, else a table rebuilt
        # from the objects themselves on top of what was read; then a catalog and a page, else
        # (once) a rebuild, after which a catalog is all it takes
        start = self._start_xref()
        rebuilt = start < _HEADER_SIZE or not self._load_all(start)
        if rebuilt:
            self._rebuild()
        if self.trailer is None:
            raise PdfFileError("no trailer")
        if not self._root_ok():
            if rebuilt:
                raise PdfFileError("no document catalog or no page")
            self._rebuild()
            self._page_list = None
            if not self.catalog:
                raise PdfFileError("no document catalog")
        trailer = self.trailer
        if trailer is not None and trailer.get("Encrypt") is not None:   # (a rebuild left one)
            raise PdfFileError("encrypted PDFs are not supported")
        self.page_count  # noqa: B018 - counted now, as CPDF_Document::LoadPages does

    @property
    def offsets(self) -> dict[int, ObjectInfo]:
        return self._table.objects

    @property
    def trailer(self) -> PdfDict | None:
        """The trailer; None only while the tables are read (an open file has one)."""
        return self._table.trailer

    # ------------------------------------------------------------------ cross-reference
    # CPDF_Parser's LoadAllCrossRefTablesAndStreams and CPDF_CrossRefTable, as they are: entries
    # are 20 bytes read blind (an offset is FXSYS_atoi64 of whatever the entry starts with, the
    # generation StringToInt of what follows byte 11), only the first placed object is checked,
    # an older table is loaded first and a newer entry replaces it unless its generation is
    # lower, a free entry in a table changes nothing, a stream never replaces what is known, and
    # a trailer's /Size cuts off the numbers past it.

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
        offset = _str_to_int(word[0], 0, 64, True)
        return offset if offset < len(data) else 0

    def _load_all(self, xref_offset: int) -> bool:
        """LoadAllCrossRefTablesAndStreams."""
        ok, _, end = self._parse_table(xref_offset, False)
        is_stream = not ok
        if is_stream:
            if self._load_xref_stream(xref_offset, True) is None:
                return False
        else:
            trailer = self._load_trailer(end)
            if trailer is None:
                return False
            self._table = CrossRef(objects=self._table.objects, trailer=trailer, number=0)
            size = _direct_integer(trailer, "Size")
            if 0 < size <= _MAX_XREF_SIZE:
                _set_size(self._table.objects, size)
        if is_stream:
            xrefs, streams = [0], [xref_offset]
        else:
            xrefs, streams = [xref_offset], [_direct_integer(self.trailer, "XRefStm")]
        if not self._find_all(xref_offset, xrefs, streams):
            return False
        if xrefs[0] > 0:
            if not self._load_table(xrefs[0]) or not self._verify_xref():
                return False
        for xref, stream in zip(xrefs[1:], streams[1:]):
            if stream > 0 and self._load_xref_stream(stream, False) is None:
                return False
            if xref > 0 and not self._load_table(xref):
                return False
        if is_stream:
            self._objstm.clear()
        return True

    def _find_all(self, main: int, xrefs: list[int], streams: list[int]) -> bool:
        """FindAllCrossReferenceTablesAndStream: the /Prev chain, oldest first."""
        seen = {main}
        pos = _direct_integer(self.trailer, "Prev")
        while pos > 0:
            if pos in seen:
                return False
            seen.add(pos)
            prev = self._load_xref_stream(pos, False)
            if prev is not None:
                xrefs.insert(0, 0)
                streams.insert(0, pos)
                pos = prev
                continue
            end = self._parse_table(pos, False)[2]
            trailer = self._load_trailer(end)
            if trailer is None:
                return False
            xrefs.insert(0, pos)
            streams.insert(0, navigation.get_integer(self, trailer["XRefStm"]) if "XRefStm" in trailer else 0)
            pos = _direct_integer(trailer, "Prev")
            self._table = _merge_up(CrossRef(objects={}, trailer=trailer, number=0), self._table)
        return True

    def _parse_table(self, pos: int, collect: bool) -> tuple[bool, list[TableEntry], int]:
        """ParseCrossRefTable: (parsed, [(number, generation, offset or None when free)] when
        `collect`, where reading stopped)."""
        data = self.data
        scan = _Words(data, min(max(pos, 0), len(data)))
        w = scan.next()
        if w is None or w[0] != b"xref":
            return False, [], scan.pos
        entries: list[TableEntry] = []
        buf = bytearray(1024 * _ENTRY_SIZE + 1)
        while True:
            saved = scan.pos
            w = scan.next()
            if w is None:
                return False, [], scan.pos
            if not w[1]:
                scan.pos = saved
                break
            start = _str_to_int(w[0] + b"\0", 0, 32, False)
            if start > _MAX_OBJECT_NUMBER:
                return False, [], scan.pos
            w = scan.next()   # GetDirectNum
            count = _str_to_int(w[0] + b"\0", 0, 32, False) if w is not None and w[1] else 0
            scan.pos = _gap_end(data, min(scan.pos, len(data)))   # ToNextWord
            if not count:
                continue
            if not collect:
                scan.pos += count * _ENTRY_SIZE
                continue
            if len(entries) + count > min(_MAX_XREF_SIZE, len(data) // _ENTRY_SIZE):
                return False, [], scan.pos
            left = count
            while left:
                block = min(left, 1024)
                size = block * _ENTRY_SIZE
                if scan.pos + size > len(data):   # ReadBlock
                    return False, [], scan.pos
                buf[:size] = data[scan.pos:scan.pos + size]
                scan.pos += size
                for i in range(block):
                    at = i * _ENTRY_SIZE
                    num = start + count - left + i
                    if buf[at + 17] == 0x66:   # 'f'
                        entries.append((num, 0, None))
                        continue
                    offset = _str_to_int(buf, at, 64, True)
                    if offset == 0 and not all(0x30 <= c <= 0x39 for c in buf[at:at + 10]):
                        return False, [], scan.pos
                    entries.append((num, _string_to_int(buf[at + 11:]) & 0xFFFF, offset))
                left -= block
        return True, entries, scan.pos

    def _load_table(self, pos: int) -> bool:
        """LoadCrossRefTable and MergeCrossRefObjectsData (a free entry's generation is never
        read, so it is 0 and changes nothing)."""
        ok, entries, _ = self._parse_table(pos, True)
        if not ok:
            return False
        objects = self._table.objects
        for num, gen, offset in entries:
            if offset is not None and num <= _MAX_OBJECT_NUMBER:
                _add_normal(objects, num, gen, False, offset)
        return True

    def _load_trailer(self, pos: int) -> PdfDict | None:
        """LoadTrailer: `trailer` and a dictionary."""
        scan = _Words(self.data, min(max(pos, 0), len(self.data)))
        w = scan.next()
        if w is None or w[0] != b"trailer":
            return None
        with _deep_recursion():
            trailer = _body(scan, False, self._held_stream(self.data), 0)
        return trailer if isinstance(trailer, dict) else None

    def _load_xref_stream(self, pos: int, main: bool) -> int | None:
        """LoadCrossRefStream: the stream's /Prev, or None when it is none."""
        if not 0 <= pos < len(self.data):
            return None
        with _deep_recursion():
            stream, num = self._indirect_at(pos)
        if not isinstance(stream, Stream) or not num:
            return None
        d = stream.dict
        prev = navigation.integer_for(self, d, "Prev")
        size = navigation.integer_for(self, d, "Size")
        if prev < 0 or size < 0 or size > _MAX_XREF_SIZE:
            return None
        table = CrossRef(objects={}, trailer=dict(d), number=num)
        if main:
            self._table = table
            _set_size(table.objects, size)
        else:
            self._table = _merge_up(table, self._table)
        indices: list[tuple[int, int]] = []
        index: PdfArray = navigation.array_for(self, d, "Index") or []
        for i in range(len(index) // 2):
            first_obj, count_obj = index[2 * i], index[2 * i + 1]
            if not navigation.is_number(first_obj) or not navigation.is_number(count_obj):
                continue
            first, count = navigation.get_integer(self, first_obj), navigation.get_integer(self, count_obj)
            if first >= 0 and count > 0:
                indices.append((first, count))
        if not indices:
            indices = [(0, size)]
        w: PdfArray = navigation.array_for(self, d, "W") or []
        widths = [navigation.get_integer(self, x) & 0xFFFFFFFF for x in w]
        total = sum(widths)
        if len(widths) < 3 or total > 0xFFFFFFFF:
            return None
        data = self.stream_data(stream)   # LoadAllDataFiltered
        seg = 0
        for first, count in indices:
            if seg + count > 0xFFFFFFFF or (seg + count) * total > min(0xFFFFFFFF, len(data)):
                continue
            if first + count > 0xFFFFFFFF:
                continue
            objects = self._table.objects
            if min(first + count, _MAX_XREF_SIZE) > (self._last_number() + 1 if objects else 0):
                _set_size(objects, min(first + count, _MAX_XREF_SIZE))
            base = seg * total
            last = self._last_number()   # the entries below never add a higher number
            for i in range(count):
                if first + i > _MAX_OBJECT_NUMBER:
                    break
                at = base + i * total
                self._xref_stream_entry(data[at:at + total], widths, first + i, last)
            seg += count
        return prev

    def _xref_stream_entry(self, entry: bytes, widths: list[int], num: int, last: int) -> None:
        """ProcessCrossRefStreamEntry."""
        objects = self._table.objects
        w0, w1, w2 = widths[:3]
        kind = _var_int(entry[:w0]) if w0 else 1
        second, third = _var_int(entry[w0:w0 + w1]), _var_int(entry[w0 + w1:w0 + w1 + w2])
        if kind == 0:
            if third <= 0xFFFF:
                _set_free(objects, num, third)
        elif kind == 1:
            if third <= 0xFFFF:
                _add_normal(objects, num, third, False, second)
        elif kind == 2:
            if second <= last:   # IsValidObjectNumber
                _add_compressed(objects, num, second, third)

    def _root_ok(self) -> bool:
        """CPDF_Document::TryInit: a catalog, and at least one page counted."""
        try:
            self._page_list = None
            return bool(self.catalog) and self.page_count > 0
        except Exception:  # noqa: BLE001
            return False

    def _verify_xref(self) -> bool:
        """CPDF_Parser::VerifyCrossRefTable: the first object the table places (lowest number
        with a position, whatever its type) must start with its own number there."""
        for num in sorted(self.offsets):
            pos = self.offsets[num].pos
            if pos <= 0:
                continue
            word = _Words(self.data, min(pos, len(self.data))).next()
            return word is not None and word[1] and _str_to_int(word[0] + b"\0", 0, 32, False) == num
        return True

    def _rebuild(self) -> None:
        """CPDF_Parser::RebuildCrossRef: the file read word by word from the start (strings and
        hex strings skipped whole, an unbalanced `(` swallowing the rest), every `n g obj` read
        as an object and stepped over, stream data included (so objects inside a stream that
        parsed are not seen, and those after a broken header's stream data may be lost); each
        added as a table would add it (a lower generation than one known is ignored), an object
        stream's members after it; each `trailer` dictionary and cross-reference stream merged
        on in file order. The table built goes over what was read before, which it does not
        replace. The catalog is only what a trailer's /Root refers to: PDFium does not go
        looking for one."""
        data = self.data
        table = _no_table()
        scan = _Words(data, 0)
        numbers: list[tuple[int, int]] = []
        while (w := scan.next()) is not None:
            word, is_number, start = w
            if is_number:
                numbers.append((_str_to_int(word + b"\0", 0, 32, False), start))
                if len(numbers) > 2:
                    numbers.pop(0)
                continue
            if word == b"(":
                scan.pos = _literal_string(data, scan.pos)[1]
            elif word == b"<":
                scan.pos = _hex_body(data, scan.pos)[1]
            elif word == b"trailer":
                t = _body(scan, False, self._rebuild_stream, 0)
                t = t.dict if isinstance(t, Stream) else t
                if t is not _NOTHING and t is not None:
                    table = _merge_up(table, CrossRef(objects={}, trailer=t if isinstance(t, dict) else None,
                                                      number=0))
            elif word == b"obj" and len(numbers) == 2:
                (num, pos), (gen, _) = numbers
                obj, scan.pos = self._object_at(pos)
                stream = obj if isinstance(obj, Stream) else None
                if stream is not None and name_for(stream.dict, "Type") == b"XRef":
                    table = _merge_up(table, CrossRef(objects={}, trailer=dict(stream.dict), number=num))
                if num <= _MAX_OBJECT_NUMBER:
                    _add_normal(table.objects, num, gen & 0xFFFF, False, pos)
                    members = self._object_stream_from(stream)
                    for i, (n, _) in enumerate(members.members if members is not None else ()):
                        if n <= _MAX_OBJECT_NUMBER:
                            _add_compressed(table.objects, n, num, i)
            numbers.clear()
        self._table = _merge_up(self._table, table)
        if self.trailer is None or not self.offsets:
            raise PdfFileError("no trailer to rebuild the cross-reference table with")

    def _object_at(self, pos: int) -> tuple[PdfObject, int]:
        """CPDF_SyntaxParser::GetIndirectObject as the rebuild calls it (no object holder, so
        an indirect /Length is unknown): the object and where reading stopped."""
        scan = _Words(self.data, pos)
        for _ in range(3):
            scan.next()
        obj = _body(scan, True, self._rebuild_stream, 0)
        return (None if obj is _NOTHING else obj), scan.pos

    def _rebuild_stream(self, d: PdfDict, pos: int) -> tuple[Stream | _Nullptr, int]:
        """CPDF_SyntaxParser::ReadStream with no object holder: (Stream or _NOTHING, position)."""
        return self._read_stream(self.data, d, pos, False)

    def _held_stream(self, data: bytes) -> StreamAt:
        """ReadStream as an object read through the parser calls it: an indirect /Length counts."""
        def stream_at(d: PdfDict, pos: int) -> tuple[Stream | _Nullptr, int]:
            return self._read_stream(data, d, pos, True)
        return stream_at

    def _read_stream(self, data: bytes, d: PdfDict, pos: int, held: bool) -> tuple[Stream | _Nullptr, int]:
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
        written = obj.get("Length")
        if isinstance(written, Ref):
            written = self.get(written.num) if held and written.num not in self._parsing else None
        length = _integer(written) if isinstance(written, (int, float)) and not isinstance(written, bool) else -1
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

    def _indirect_at(self, pos: int) -> tuple[Body, int | None]:
        """CPDF_SyntaxParser::GetIndirectObject (loose, with the document as holder): the
        object after `n g obj` at pos with its number, or (_NOTHING, None)."""
        scan = _Words(self.data, pos)
        num, gen, keyword = scan.next(), scan.next(), scan.next()
        if num is None or gen is None or keyword is None or not num[1] or not gen[1] or keyword[0] != b"obj":
            return _NOTHING, None
        return _body(scan, False, self._held_stream(self.data), 0), _atoui(num[0])

    def get(self, num: int) -> PdfObject:
        """The indirect object `num`, None when there is none (or it is `null`: `indirect` tells
        them apart)."""
        if num in self._cache:
            return self._cache[num]
        if num in self._parsing:
            return None
        # CPDF_Parser::ParseIndirectObject; the holder keeps what parsed, never a failure, so an
        # object asked for while the table was being read is looked for again once it is read
        entry = self.offsets.get(num) if num > 0 else None   # (a known number is a valid one)
        obj: Body = _NOTHING
        self._parsing.add(num)
        try:
            with _deep_recursion():
                if entry is not None and entry.kind == "n" and entry.pos > 0:
                    obj = self._parse_at(entry.pos, num)
                elif entry is not None and entry.kind == "c":
                    objstm = self._object_stream(entry.pos & 0xFFFFFFFF)
                    index = entry.pos >> 32
                    if objstm is not None and index < len(objstm.members) and objstm.members[index][0] == num:
                        at = objstm.first + objstm.members[index][1]   # ParseObjectAtOffset
                        if at < len(objstm.data):
                            obj = _body(_Words(objstm.data, at), False, self._held_stream(objstm.data), 0)
        finally:
            self._parsing.discard(num)
        if obj is _NOTHING:
            return None
        if obj is None:
            self._nulls.add(num)
        elif isinstance(obj, (dict, list, Stream)):
            self._objnums[id(obj)] = num
        self._cache[num] = obj
        return obj

    def _last_number(self) -> int:
        """GetLastObjNum: the table's highest number (IsValidObjectNumber: at most that)."""
        return max(self.offsets, default=0)

    def _parse_at(self, pos: int, num: int) -> Body:
        """ParseIndirectObjectAt: the object at pos if it is object `num`, else nothing."""
        obj, found = self._indirect_at(min(pos, len(self.data)))
        return obj if found == num else _NOTHING

    def _object_stream(self, num: int) -> ObjectStream | None:
        """CPDF_Parser::GetObjectStream: the object stream the table knows as one, None when it
        is none; kept once parsed, even as none."""
        if num in self._parsing:
            return None
        if num in self._objstm:
            return self._objstm[num]
        info = self.offsets.get(num)
        if info is None or not info.is_objstm or info.pos <= 0:
            return None
        self._parsing.add(num)
        try:
            obj = self._parse_at(info.pos, num)
        finally:
            self._parsing.discard(num)
        if obj is _NOTHING:
            return None
        self._objstm[num] = self._object_stream_from(obj if isinstance(obj, Stream) else None)
        return self._objstm[num]

    def _object_stream_from(self, stream: Stream | None) -> ObjectStream | None:
        """CPDF_ObjectStream::Create and Init: a direct /Type /ObjStm, an integer /N in
        [0, kMaxObjectNumber] and an integer /First >= 0; then /N pairs read as GetDirectNum
        does (anything but a number reads 0), stopping at the end of the data, a pair with
        number 0 left out."""
        if stream is None:
            return None
        d = stream.dict
        n, first = d.get("N"), d.get("First")
        if name_for(d, "Type") != b"ObjStm":
            return None
        if type(n) is not int or not 0 <= n <= _MAX_OBJECT_NUMBER or type(first) is not int or first < 0:
            return None
        data = self.stream_data(stream)
        scan = _Words(data, 0)
        members: list[tuple[int, int]] = []
        for _ in range(n):
            if scan.pos >= len(data):
                break
            number, offset = self._direct_num(scan), self._direct_num(scan)
            if number:
                members.append((number, offset))
        return ObjectStream(members=members, data=data, first=first)

    @staticmethod
    def _direct_num(scan: _Words) -> int:
        """GetDirectNum: the next word as a number, 0 when it is none."""
        w = scan.next()
        return _str_to_int(w[0] + b"\0", 0, 32, False) if w is not None and w[1] else 0

    def indirect(self, num: int) -> PdfObject | Nothing:
        """GetOrParseIndirectObject: the object, None for a `null` one, navigation.NOTHING (nullptr)
        when there is none."""
        obj = self.get(num)
        return NOTHING if obj is None and num not in self._nulls else obj

    def objnum(self, obj: PdfObject) -> int:
        """CPDF_Object::GetObjNum: an indirect object's number; 0 for anything held directly (an
        array item, a dictionary value, a stream's dictionary)."""
        num = self._objnums.get(id(obj))
        return num if num is not None and self._cache.get(num) is obj else 0

    def resolve(self, value: PdfObject) -> PdfObject:
        """The object a value stands for: references followed, 32 at most."""
        depth = 0
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
    def catalog(self) -> PdfDict:
        """GetRoot: what the trailer's /Root refers to (a direct dictionary does not count)."""
        trailer = self.trailer
        root = trailer.get("Root") if trailer is not None else None
        return (_dict(self.resolve(root)) or {}) if isinstance(root, Ref) else {}

    # The page tree as CPDF_Document reads it. The page count is the root's /Count when it is
    # sane (RetrievePageCount, CountPages), whatever the kids say; a page is found by a traversal
    # that keeps its place between calls (GetMutablePageDictionary, TraversePDFPages), where a kid
    # that is no dictionary uses up a page, a node with no /Kids array is a page, and a leaf
    # found remembers its object number (m_PageList). So a /Count larger than the tree gives
    # pages that don't load, one smaller hides the rest, and which pages load can depend on the
    # order they are asked for. Inherited attributes come from the /Parent chain (GetPageAttr).

    def _pages_node(self) -> tuple[int, PdfDict | None]:
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
    def _is_branch(node: PdfDict) -> bool:
        """GetNodeType: /Type /Pages or /Page decides; anything else (a reference included, since
        GetNameFor reads only a direct name) is guessed from /Kids and written in, as PDFium
        fixes its in-memory copy."""
        kind = node.get("Type")
        if isinstance(kind, Name) and kind in ("Pages", "Page"):
            return kind == "Pages"
        branch = "Kids" in node
        node[Name("Type")] = Name("Pages" if branch else "Page")
        return branch

    def _count_node(self, node: PdfDict, visited: list[PdfDict]) -> int | None:
        """CountPages: None when the tree holds too many pages."""
        count = _integer(self.resolve(node.get("Count")))
        if 0 < count < _PAGE_MAX:
            return count
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            return 0
        count = 0
        for item in kids:
            kid = _dict(self.resolve(item))
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
        return len(self._pages())

    def _pages(self) -> list[KnownPage | None]:
        """m_PageList: the pages found so far, counted when first asked for."""
        if self._page_list is None:
            self._page_list = [None] * self._count_pages()
            self._traversal = []
            self._next_page = 0
            self._max_level = False
        return self._page_list

    def page_dict(self, index: int) -> KnownPage | None:
        """(object number, page dictionary) of page `index`, or None where PDFium finds none.
        The number is None for a page written directly in /Kids, 0 when it has none."""
        page_list = self._pages()
        if not 0 <= index < len(page_list):
            return None
        known = page_list[index]
        if known is not None:
            known_num = known[0]
            if known_num is None:
                return known
            if known_num != 0:
                obj = self.get(known_num)
                if isinstance(obj, dict):
                    return known_num, obj
        num, pages = self._pages_node()
        if pages is None:
            return None
        if not self._traversal:
            self._traversal = [_Level(num=num, node=pages, next=0)]
            self._next_page = 0
            self._max_level = False
        togo = _Countdown(left=index - self._next_page + 1)
        with _deep_recursion():
            page = self._traverse(page_list, index, togo, 0)
        self._next_page = index + 1
        return page

    def _kid(self, value: PdfObject) -> tuple[int | None, PdfDict | None]:
        """CPDF_Array::ConvertToIndirectObjectAt, then GetMutableDictAt."""
        if isinstance(value, Ref):
            obj = self.get(value.num)
            if isinstance(obj, Stream):
                return 0, obj.dict   # the stream's dictionary has no object number of its own
            return value.num, obj if isinstance(obj, dict) else None
        return None, _dict(value)

    def _traverse(self, page_list: list[KnownPage | None], index: int, togo: _Countdown,
                  level: int) -> KnownPage | None:
        if togo.left < 0 or self._max_level:
            return None
        entry = self._traversal[level]
        node = entry.node
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            self._traversal.pop()
            if togo.left != 1 or self._is_branch(node):
                return None
            page_list[index] = (entry.num, node)
            return entry.num, node
        if level >= _MAX_PAGE_LEVEL:
            self._traversal.pop()
            self._max_level = True
            return None
        page: KnownPage | None = None
        for k in range(entry.next, len(kids)):
            if togo.left == 0:
                break
            num, kid = self._kid(kids[k])
            if kid is None:
                togo.left -= 1
                entry.next += 1
                continue
            if kid is node:
                entry.next += 1
                continue
            if "Kids" not in kid:
                page_list[index - togo.left + 1] = (num, kid)
                togo.left -= 1
                entry.next += 1
                if togo.left == 0:
                    page = (num, kid)
                    break
            else:
                if len(self._traversal) == level + 1:
                    self._traversal.append(_Level(num=num, node=kid, next=0))
                found = self._traverse(page_list, index, togo, level + 1)
                if len(self._traversal) == level + 1:
                    entry.next += 1
                if len(self._traversal) != level + 1 or togo.left == 0 or self._max_level:
                    page = found
                    break
        if entry.next == len(kids):
            self._traversal.pop()
        return page

    def page_attr(self, node: PdfDict, key: str) -> PdfObject:
        """CPDF_Page::GetPageAttr: the page's own value, else its parents'."""
        seen: list[PdfDict] = []
        at: PdfDict | None = node
        while at is not None and not any(at is v for v in seen):
            if key in at:
                value = self.resolve(at[key])
                if value is not None:
                    return value
            seen.append(at)
            at = _dict(self.resolve(at.get("Parent")))
        return None

    def inherited_page(self, index: int) -> tuple[Ref | None, PdfDict] | None:
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

    def page_index(self, objnum: int) -> int:
        """CPDF_Document::GetPageIndex: a page known by its object number, else found in the
        tree (skipping the pages already known) and remembered when it is a /Page. Object
        number 0 (a dictionary written in place) is the first page not loaded yet; a page
        written directly in /Kids has a number of its own in PDFium, which nothing refers to."""
        page_list = self._pages()
        count = len(page_list)
        skip, skipped = 0, False
        for i, known in enumerate(page_list):
            num = known[0] if known is not None else 0
            if num == objnum:
                return i
            if not skipped and num == 0:
                skip, skipped = i, True
        num, pages = self._pages_node()
        if pages is None:
            return -1
        state = _Search(skip=skip, index=0,
                        loaded={id(k[1]) for k in page_list if k is not None and k[0] is None})
        with _deep_recursion():
            found = self._find_page_index(num, pages, state, objnum, 0)
        if not 0 <= found < count:
            return -1
        obj = self.indirect(objnum) if objnum else NOTHING
        if isinstance(obj, dict) and name_for(obj, "Type") == b"Page":   # IsValidPageObject
            page_list[found] = (objnum, obj)
        return found

    def _find_page_index(self, num: int, node: PdfDict, state: _Search, objnum: int, level: int) -> int:
        """CPDF_Document::FindPageIndex."""
        if "Kids" not in node:
            if objnum == num:
                return state.index
            if state.skip:
                state.skip -= 1
            state.index += 1
            return -1
        kids = array_for(self, node, "Kids")
        if kids is None or level >= _MAX_PAGE_LEVEL:
            return -1
        count = integer_for(self, node, "Count") & 0xFFFFFFFFFFFFFFFF  # an int stored in a size_t
        if count <= state.skip:
            state.skip -= count
            state.index += count
            return -1
        if count and count == len(kids):
            for i, item in enumerate(kids):
                if isinstance(item, Ref) and item.num == objnum:
                    return state.index + i
        for i in range(len(kids)):
            kid = dict_at(self, kids, i)
            if kid is None or kid is node:
                continue
            kid_num = -1 if id(kid) in state.loaded else self.objnum(kid)
            found = self._find_page_index(kid_num, kid, state, objnum, level + 1)
            if found >= 0:
                return found
        return -1


# ---------------------------------------------------------------------- writing


def _write_name(n: str) -> bytes:
    out = bytearray(b"/")
    for b in n.encode("utf-8"):
        if b < 0x21 or b > 0x7E or b in b"()<>[]{}/%#":
            out += b"#%02X" % b
        else:
            out.append(b)
    return bytes(out)


def _write_number(v: bool | int | float) -> bytes:
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return str(v).encode()
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-", "-0") else "0").encode()


def serialize(v: PdfObject, keep: Callable[[Ref], bool]) -> bytes:
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


def write_file(pdf: PdfFile, keep_pages: list[int], boxes: Mapping[int, Box]) -> bytes:
    """A new file with the pages `keep_pages` (in document order), media and crop box replaced
    for those in `boxes` (PDF user space rectangles). Every other object keeps its number; a
    reference to a page that was left out becomes null."""
    pages = [pdf.inherited_page(i) for i in range(pdf.page_count)]
    kept = [(i, found) for i in sorted(set(keep_pages)) if (found := pages[i]) is not None]
    page_refs = {p[0].num for p in pages if p is not None and p[0] is not None}
    kept_refs = {ref.num for _, (ref, _) in kept if ref is not None}
    pages_num = max(pdf.offsets or {0: None}) + 1
    objects: dict[int, PdfObject] = {}

    def keep(ref: Ref) -> bool:
        return ref.num == pages_num or ref.num not in page_refs or ref.num in kept_refs

    # the new page dictionaries: inherited attributes written out, one parent
    new_pages: dict[int, PdfDict] = {}
    extra = pages_num + 1
    kids: PdfArray = []
    for i, (ref, page) in kept:
        d = {k: v for k, v in page.items() if k != "Parent"}
        d["Parent"] = Ref(pages_num, 0)
        if i in boxes:
            d["MediaBox"] = [*boxes[i]]
            d["CropBox"] = [*boxes[i]]
        if ref is None:
            ref = Ref(extra, 0)
            extra += 1
        new_pages[ref.num] = d
        kids.append(ref)
    objects[pages_num] = {Name("Type"): Name("Pages"), Name("Kids"): kids, Name("Count"): len(kids)}
    catalog = dict(pdf.catalog)
    catalog[Name("Pages")] = Ref(pages_num, 0)
    trailer: PdfDict = pdf.trailer or {}
    root_ref = trailer.get("Root")
    root_num = root_ref.num if isinstance(root_ref, Ref) else extra
    if not isinstance(root_ref, Ref):
        extra += 1
    objects[root_num] = catalog
    objects.update(new_pages)

    # everything reachable from the catalog and the info dictionary
    todo: list[PdfObject] = [catalog, trailer.get("Info")]
    todo += new_pages.values()
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
    offsets: dict[int, int] = {}
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
    new_trailer: PdfDict = {Name("Size"): size, Name("Root"): Ref(root_num, 0)}
    info = trailer.get("Info")
    if isinstance(info, Ref) and info.num in objects:
        new_trailer[Name("Info")] = info
    out += b"trailer\n" + serialize(new_trailer, _every_ref) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _every_ref(ref: Ref) -> bool:
    return True


def read(source: str | Path | bytes | bytearray) -> PdfFile:
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    try:
        return PdfFile(bytes(data))
    except (PdfFileError, PdfSyntaxError):
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfFileError(f"unreadable PDF: {e}") from e


__all__ = ["PdfFile", "PdfFileError", "read", "text_string", "write_file", "serialize", "END"]
